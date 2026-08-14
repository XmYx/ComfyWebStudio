"""Running a stage.

A registry keyed by stage kind, in the same shape as the LLM providers and the ComfyUI backends: adding a
kind is adding a module, not editing a conditional.

`run_stage` is the whole public surface. It fans out over frames itself when the stage is per-frame, so
nothing above it loops, and it writes a :class:`StageRun` for every execution whether that execution
worked or not — a stage that failed is exactly the one someone needs the transcript for.

One asymmetry is deliberate and worth knowing about: an ``llm`` stage finishes inside this call, while a
``comfy`` stage only *starts* something and comes back with a run id. Waiting for that is the caller's
job, because the two callers want different things — a request wants to return, and a whole-pipeline run
wants to block until the pictures exist.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..core.errors import ValidationFailed
from ..core.pipeline import ModelRole, Stage, StageRun, WriteRecord
from ..core.prompting import json_schema, render
from ..core.storyboard import Storyboard, StoryboardFrame
from ..llm.provider import LlmError, LlmProvider, create_provider
from ..llm.storywriter import as_text, parse_json_object
from . import sinks
from .context import build_context
from .transcript import record

logger = logging.getLogger(__name__)


@dataclass
class StageContext:
    """Everything a stage needs, and the bits of state that outlive one stage."""

    state: Any
    project: Any
    board: Storyboard
    #: What earlier stages returned, keyed by stage id, reachable from a template as {stage.<id>.<key>}.
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The stage that ran immediately before, whose outputs are also {prev.<key>}.
    previous: str | None = None
    pipeline_run_id: str | None = None
    #: False for a stage that proposes rather than decides — what "find the characters" has always been.
    apply: bool = True
    #: One client per role for as long as the context lives, rather than one per frame. A twenty-frame
    #: board used to mean twenty HTTP clients opened and closed.
    _providers: dict[str, tuple[LlmProvider, str]] = field(default_factory=dict)

    def provider(self, role: ModelRole, model: str = "") -> tuple[LlmProvider, str]:
        if role not in self._providers:
            self._providers[role] = provider_for(self.state.settings, role)
        provider, configured = self._providers[role]
        return provider, (model or configured)

    async def close(self) -> None:
        for provider, _ in self._providers.values():
            try:
                await provider.close()
            except Exception as exc:  # noqa: BLE001 - closing must not mask the real result
                logger.debug("Could not close a language-model client: %s", exc)
        self._providers.clear()


@dataclass(slots=True)
class Outcome:
    """What one execution of a handler produced, beyond its transcript entry."""

    runs: list[StageRun] = field(default_factory=list)
    #: What a propose-only stage came back with, for the caller to offer to the user.
    proposed: list[Any] = field(default_factory=list)
    #: `comfy` only: the queued run, which finishes later.
    run_id: str | None = None
    step_ids: dict[str, str] = field(default_factory=dict)
    #: Facts a route wants to pass on — `seeded`, `shot_id`, `asset_id`.
    detail: dict[str, Any] = field(default_factory=dict)

    def merge(self, other: Outcome) -> None:
        self.runs += other.runs
        self.proposed += other.proposed
        self.run_id = other.run_id or self.run_id
        self.step_ids.update(other.step_ids)
        self.detail.update(other.detail)


@dataclass(slots=True)
class StageResult:
    stage_id: str
    status: str = "success"
    runs: list[StageRun] = field(default_factory=list)
    run_id: str | None = None
    step_ids: dict[str, str] = field(default_factory=dict)
    proposed: list[Any] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


StageHandler = Callable[["StageContext", Stage, StoryboardFrame | None, dict], Awaitable[Outcome]]

#: kind -> handler. Populated by :func:`handles` at import.
HANDLERS: dict[str, StageHandler] = {}


def handles(kind: str):
    def decorate(fn: StageHandler) -> StageHandler:
        HANDLERS[kind] = fn
        return fn
    return decorate


def provider_for(settings, role: ModelRole) -> tuple[LlmProvider, str]:
    """The configured provider and model for one of the two jobs, or a reason it cannot be done."""
    story = settings.story
    vision = role == "vision"
    provider_id = (story.vision_provider_id if vision else story.provider_id) or story.provider_id
    model = story.vision_model if vision else story.write_model

    config = next((p for p in settings.llm_providers if p.id == provider_id and p.enabled), None)
    if config is None:
        raise ValidationFailed(
            "No language model is configured. Add one on the Settings page — Ollama on this machine "
            "is the usual choice."
        )
    if not model:
        raise ValidationFailed(
            f"No {'vision' if vision else 'writing'} model chosen. Pick one on the Settings page."
        )
    return create_provider(config), model


def temperature_for(ctx: StageContext, stage: Stage) -> float:
    """The stage's own temperature, or the setting it defers to."""
    if stage.model.temperature is not None:
        return stage.model.temperature
    return ctx.state.settings.story.temperature


# -- the llm stage ---------------------------------------------------------------------------------------


@handles("llm")
async def run_llm(
    ctx: StageContext, stage: Stage, frame: StoryboardFrame | None, options: dict
) -> Outcome:
    """Ask a model, structured, and put the answer where the output fields say it goes."""
    entry = begin_run(ctx, stage, frame)
    started = time.monotonic()
    outcome = Outcome(runs=[entry])

    context = build_context(
        ctx.project, ctx.board, frame,
        count=options.get("count"), outputs=ctx.outputs, previous=ctx.previous,
    )
    entry.system, system_unknown = render(stage.system, context)
    entry.prompt, prompt_unknown = render(stage.prompt, context)
    entry.unknown_tokens = list(dict.fromkeys(system_unknown + prompt_unknown))

    provider, model = ctx.provider(stage.model.role, stage.model.model)
    entry.model = model
    entry.provider_id = provider.config.id
    entry.temperature = temperature_for(ctx, stage)

    schema = json_schema(stage.outputs) if stage.outputs else None
    entry.schema_sent = schema

    try:
        images = _frame_image(ctx, frame) if stage.model.attach_image else []
        entry.image_count = len(images)

        reply = await provider.complete(
            entry.prompt,
            model=model,
            system=entry.system,
            images=images or None,
            json_object=bool(stage.outputs),
            schema=schema,
            temperature=entry.temperature,
        )
        entry.reply = reply.text
        payload = parse_json_object(reply.text) if stage.outputs else {"text": reply.text}
        entry.payload = payload

        if stage.retry is not None:
            payload, retried = await _retry_blanks(ctx, stage, frame, entry, payload, images, context)
            outcome.runs += retried

        entry.writes, proposed = _apply(ctx, stage, frame, payload)
        outcome.proposed += proposed
    except (LlmError, ValidationFailed) as exc:
        fail_run(ctx, entry, str(exc), started)
        raise ValidationFailed(str(exc)) from exc
    except Exception as exc:
        fail_run(ctx, entry, str(exc), started)
        raise

    if stage.sets_status and frame is not None and ctx.apply:
        frame.status = stage.sets_status
    ctx.outputs[stage.id] = payload
    finish_run(ctx, entry, started)
    return outcome


async def _retry_blanks(
    ctx: StageContext,
    stage: Stage,
    frame: StoryboardFrame | None,
    entry: StageRun,
    payload: dict,
    images: list[bytes],
    context: dict[str, str],
) -> tuple[dict, list[StageRun]]:
    """Ask again for the fields that came back empty, one narrow question at a time.

    A schema can insist a field is present; it cannot insist it says anything. Asking the one unanswered
    question on its own is a far easier thing to answer than all of them at once — and doing it here, as
    a stage's own declared retry, means it shows up in the transcript instead of happening invisibly.
    """
    retry = stage.retry
    blank = [key for key in retry.when_empty if not str(payload.get(key) or "").strip()]
    if not blank or not retry.prompt.strip():
        return payload, []

    # The first answer is in scope for the retry, which is how it can say "you already wrote this".
    followup = dict(context)
    for key, value in payload.items():
        followup[f"prev.{key}"] = as_text(value)

    provider, model = ctx.provider(stage.model.role, stage.model.model)
    prompt, unknown = render(retry.prompt, followup)
    schema = json_schema([f for f in stage.outputs if f.key in blank])

    second = begin_run(ctx, stage, frame)
    second.retry = True
    second.system = entry.system
    second.prompt = prompt
    second.provider_id = provider.config.id
    second.model = model
    second.temperature = min(1.0, (entry.temperature or 0.0) + retry.temperature_delta)
    second.image_count = len(images)
    second.schema_sent = schema
    second.unknown_tokens = unknown

    started = time.monotonic()
    try:
        reply = await provider.complete(
            prompt,
            model=model,
            system=entry.system,
            images=images or None,
            json_object=True,
            schema=schema,
            temperature=second.temperature,
        )
        second.reply = reply.text
        answered = parse_json_object(reply.text)
        second.payload = answered
        payload = {**payload, **{k: v for k, v in answered.items() if str(v or "").strip()}}
        finish_run(ctx, second, started)
    except LlmError as exc:
        # A failed retry is not a failed stage: what came back the first time is still worth keeping.
        logger.info("The follow-up question for %s came back empty-handed: %s", stage.id, exc)
        fail_run(ctx, second, str(exc), started)

    return payload, [second]


def _apply(
    ctx: StageContext, stage: Stage, frame: StoryboardFrame | None, payload: dict
) -> tuple[list[WriteRecord], list[Any]]:
    """Route each output to its destination, recording what happened to it."""
    written: list[WriteRecord] = []
    proposed: list[Any] = []

    for output in stage.outputs:
        value = payload.get(output.key)

        if output.writes == "board.frames":
            frames = sinks.frames_from(value, ctx.board)
            written.append(
                WriteRecord(
                    target=output.writes,
                    before=str(len(ctx.board.frames)),
                    after=str(len(frames)),
                    applied=ctx.apply,
                    reason="" if ctx.apply else "proposed only",
                )
            )
            if ctx.apply:
                ctx.board.frames = frames
                ctx.board.reorder()
            else:
                proposed += frames

        elif output.writes == "board.characters":
            # Against the board as well as against itself: proposing someone who is already there is the
            # one thing this step does that a person then has to undo by hand.
            people = sinks.characters_from(value, known=ctx.board.characters)
            written.append(
                WriteRecord(
                    target=output.writes,
                    before=str(len(ctx.board.characters)),
                    after=str(len(people)),
                    applied=ctx.apply,
                    reason="" if ctx.apply else "proposed only",
                )
            )
            if ctx.apply:
                ctx.board.characters.extend(people)
            else:
                proposed += people

        else:
            record_ = sinks.write(
                ctx.project, ctx.board, frame, output.writes, value, apply=ctx.apply
            )
            written.append(record_)
            if not output.writes and record_.after:
                proposed.append(record_.after)

    return written, proposed


def _frame_image(ctx: StageContext, frame: StoryboardFrame | None) -> list[bytes]:
    """The picture this frame is showing, as bytes, for a stage that wants to look at it."""
    from .frames import current

    if frame is None:
        raise ValidationFailed("This step looks at a frame's picture, so it has to run on a frame.")
    showing = current(ctx.state.store, ctx.project, ctx.board, frame)
    path = ctx.state.store.resolve(ctx.project.id, showing.path) if showing.path else None
    if path is None or not path.is_file():
        raise ValidationFailed("That frame has no image to look at yet. Draw it first.")
    return [path.read_bytes()]


# -- bookkeeping -----------------------------------------------------------------------------------------


def begin_run(ctx: StageContext, stage: Stage, frame: StoryboardFrame | None) -> StageRun:
    """A transcript entry for one execution, opened before anything can go wrong."""
    return StageRun(
        board_id=ctx.board.id,
        pipeline_run_id=ctx.pipeline_run_id,
        stage_id=stage.id,
        stage_name=stage.name or stage.id,
        kind=stage.kind,
        scope=stage.scope,
        frame_id=frame.id if frame else None,
    )


def finish_run(ctx: StageContext, entry: StageRun, started: float, status: str = "success") -> None:
    entry.status = status
    entry.finished = datetime.now(UTC)
    entry.duration_ms = int((time.monotonic() - started) * 1000)
    record(ctx.state.store, ctx.project.id, entry)


def fail_run(ctx: StageContext, entry: StageRun, message: str, started: float) -> None:
    entry.error = message
    finish_run(ctx, entry, started, status="error")


# -- the public surface ----------------------------------------------------------------------------------


async def run_stage(
    ctx: StageContext,
    stage: Stage,
    *,
    frame_ids: list[str] | None = None,
    options: dict[str, Any] | None = None,
) -> StageResult:
    """Run one stage, over the whole board or over each of its frames."""
    # Imported for their registration side effect, and here rather than at module scope because they
    # import this module back — the same shape `create_provider` uses for the LLM adapters.
    from . import handlers  # noqa: F401

    handler = HANDLERS.get(stage.kind)
    if handler is None:
        raise ValidationFailed(
            f"There is no such kind of step as {stage.kind!r}. Known kinds: "
            f"{', '.join(sorted(HANDLERS)) or 'none'}."
        )

    options = dict(options or {})
    gathered = Outcome()

    if stage.scope == "board":
        gathered.merge(await handler(ctx, stage, None, options))
        return _result(stage, gathered)

    frames = selected(ctx.board, frame_ids)
    if stage.kind == "comfy":
        # The exception to fanning out: one run covering every selected frame, rather than a run each.
        # Progress, caching and cancellation are all per run, so one run is what the user means by
        # "draw the board".
        options["frame_ids"] = [f.id for f in frames]
        gathered.merge(await handler(ctx, stage, None, options))
        return _result(stage, gathered, status="pending")

    for frame in frames:
        gathered.merge(await handler(ctx, stage, frame, options))
    return _result(stage, gathered)


def _result(stage: Stage, outcome: Outcome, status: str = "success") -> StageResult:
    return StageResult(
        stage_id=stage.id,
        status=status,
        runs=outcome.runs,
        run_id=outcome.run_id,
        step_ids=outcome.step_ids,
        proposed=outcome.proposed,
        detail=outcome.detail,
    )


def selected(board: Storyboard, frame_ids: list[str] | None) -> list[StoryboardFrame]:
    """The frames a run applies to. Empty selection means all of them."""
    wanted = set(frame_ids or [])
    frames = [f for f in sorted(board.frames, key=lambda f: f.order) if not wanted or f.id in wanted]
    if not frames:
        raise ValidationFailed(
            "Those frames are not on this storyboard." if wanted
            else "Nothing to work on. Write the shots first, or add a frame."
        )
    return frames
