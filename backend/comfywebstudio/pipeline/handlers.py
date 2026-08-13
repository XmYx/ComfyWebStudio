"""The three stages that do not talk to a language model.

Drawing a frame, keeping the picture, and building the shot. They are stages for the same reason the
writing ones are: a flow you can see all of is a flow you can reason about, and "why did that frame never
get a picture" is a much easier question when the drawing step is a row in a list rather than something
that happens between two requests.

Each is a thin adapter over `pipeline/frames.py`, which is where the actual work lives — the same code the
plain routes call. Nothing here reimplements anything.
"""

from __future__ import annotations

import logging
import time

from ..core.prompting import render
from ..core.storyboard import StoryboardFrame
from .context import build_context
from .frames import capture, current, ensure_step, make_shot, reseed, stills_shot, workflow_for
from .runner import (
    Outcome,
    Stage,
    StageContext,
    begin_run,
    fail_run,
    finish_run,
    handles,
    selected,
)

logger = logging.getLogger(__name__)


@handles("comfy")
async def run_comfy(
    ctx: StageContext, stage: Stage, _frame: StoryboardFrame | None, options: dict
) -> Outcome:
    """Draw the selected frames: build a step each, then queue them as one run.

    One run rather than one per frame, because progress, caching and cancellation are all per run — and
    "draw the board" is one thing the user did, so it should be one thing they can watch and one thing
    they can stop.

    This returns as soon as the run is queued. Whoever wants the pictures waits for the run id.
    """
    entry = begin_run(ctx, stage, None)
    started = time.monotonic()
    outcome = Outcome(runs=[entry])

    try:
        workflow, slot = workflow_for(ctx.project, ctx.board, stage.slot)
        frames = selected(ctx.board, options.get("frame_ids"))
        shot = stills_shot(ctx.project, ctx.board)
        reroll = bool(options.get("reroll"))

        # Asked once, before the loop: whether a reroll can actually vary anything is a property of the
        # workflow, not of any one frame.
        seeded = any(p.is_seed for p in workflow.params)

        prompts: list[str] = []
        step_ids: dict[str, str] = {}
        for frame in frames:
            context = build_context(
                ctx.project, ctx.board, frame, outputs=ctx.outputs, previous=ctx.previous
            )
            prompt, unknown = render(stage.prompt, context)
            prompts.append(prompt)
            for name in unknown:
                if name not in entry.unknown_tokens:
                    entry.unknown_tokens.append(name)

            step = ensure_step(
                ctx.project, ctx.board, shot, workflow, frame, prompt=prompt, slot=slot
            )
            if reroll and stage.reroll_seed:
                reseed(step, workflow)
            step_ids[frame.id] = step.id

        # The transcript for a drawing step is the prompts it sent, which is exactly the question someone
        # asks when a frame comes back wrong.
        entry.prompt = "\n---\n".join(prompts)
        entry.step_ids = step_ids
        entry.model = workflow.name

        ctx.state.store.save(ctx.project)
        run = await ctx.state.orchestrator.start(
            ctx.project,
            shot,
            mode="step",
            step_ids=list(step_ids.values()),
            # Nothing to vary means the cache would hand back the same picture, so re-render instead of
            # pretending. `seeded` is what lets the UI say why it may look identical anyway.
            force=reroll and not seeded,
        )
    except Exception as exc:
        fail_run(ctx, entry, str(exc), started)
        raise

    entry.run_id = run.id
    outcome.run_id = run.id
    outcome.step_ids = step_ids
    outcome.detail = {"shot_id": shot.id, "workflow": workflow.name, "seeded": seeded}
    finish_run(ctx, entry, started)
    return outcome


@handles("capture")
async def run_capture(
    ctx: StageContext, stage: Stage, frame: StoryboardFrame | None, _options: dict
) -> Outcome:
    """Keep this frame's drawn still as a project asset."""
    entry = begin_run(ctx, stage, frame)
    started = time.monotonic()
    outcome = Outcome(runs=[entry])

    showing = current(ctx.state.store, ctx.project, ctx.board, frame)
    if stage.only_if_missing and showing.use_asset and showing.asset is not None:
        # The picture on screen is already an asset. Nothing to keep, and saying so is more useful than
        # making a second asset holding the same bytes.
        entry.reply = "already kept"
        finish_run(ctx, entry, started, status="skipped")
        outcome.detail = {"asset_id": showing.asset.id, "skipped": True}
        return outcome

    try:
        asset = capture(ctx.project, ctx.board, frame, showing)
    except Exception as exc:
        fail_run(ctx, entry, str(exc), started)
        raise

    if stage.sets_status:
        frame.status = stage.sets_status
    entry.reply = asset.id
    outcome.detail = {"asset_id": asset.id}
    finish_run(ctx, entry, started)
    return outcome


@handles("shot")
async def run_shot(
    ctx: StageContext, stage: Stage, frame: StoryboardFrame | None, _options: dict
) -> Outcome:
    """Turn this frame into a real shot, wired to the workflow that animates it."""
    entry = begin_run(ctx, stage, frame)
    started = time.monotonic()
    outcome = Outcome(runs=[entry])

    context = build_context(ctx.project, ctx.board, frame, outputs=ctx.outputs, previous=ctx.previous)
    entry.prompt, entry.unknown_tokens = render(stage.prompt, context)

    try:
        shot = make_shot(
            ctx.state.store,
            ctx.project,
            ctx.board,
            frame,
            prompt=entry.prompt,
            slot=ctx.board.binding.slot(stage.slot),
        )
    except Exception as exc:
        fail_run(ctx, entry, str(exc), started)
        raise

    if stage.sets_status:
        frame.status = stage.sets_status
    entry.reply = shot.id
    outcome.detail = {"shot_id": shot.id, "shot": shot}
    finish_run(ctx, entry, started)
    return outcome
