"""Storyboards: writing them, drawing them, and turning them into shots.

The flow is a sequence of steps, and each one is a separate request so the user can stop, look and edit
between them — which is the point of a storyboard.

    premise  --LLM-->  frames  --ComfyUI-->  stills  --VLM-->  better prompts  -->  shots

Every route below is a thin wrapper over one **stage** of that flow (`pipeline/`), which is where the
prompts, the models and the order live. The stages are data, so all of it is editable and none of it is
hidden in these handlers; what remains here is which stage a given button presses and what the answer
looks like coming back.

Three decisions shape the rest of this module.

**The stills are a shot.** Generating a frame's image is running one workflow with one parameter changed,
which is exactly what a step is — so a storyboard keeps a hidden shot with one step per frame. Caching,
progress, cancellation, previews and the run history all come free, and "regenerate frame 3" is just
running a step.

**Nothing is inferred at generation time.** Which workflow draws, which parameter is the prompt, which
input takes the starting image: all recorded on the storyboard's binding. A guess that happens to run is
worse than an error, because it produces a hundred frames that quietly ignored the prompt.

**A frame shows whichever of its two pictures is newer** — the still its step last drew, or the asset it
points at. Storyboarding is iterative: draw the board, reroll the three frames that came out wrong, drop a
photograph onto the one that should be a plate. Neither source wins by rule, so none of that needs undoing
first, and everything downstream — describing, animating — works from the picture on screen.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.errors import NotFound, ValidationFailed
from ..core.models import Asset, AssetSource, Shot, utcnow
from ..core.pipeline import Pipeline, Stage, StageRun
from ..core.prompting import render
from ..core.storyboard import (
    Storyboard,
    StoryboardBinding,
    StoryboardCharacter,
    StoryboardFrame,
)
from ..pipeline import sinks
from ..pipeline.builtin import PORTRAIT_PROMPT, builtin_pipeline
from ..pipeline.context import build_context
from ..pipeline.frames import (
    STILLS_PREFIX,
    ensure_drawing_step,
    ensure_step,
    slot_workflow,
    stills_shot,
    stills_view,
    workflow_for,
)
from ..pipeline.resolve import overlay_with, overlay_without, resolve, stage_is_stale
from ..pipeline.runner import StageContext, StageResult, run_stage
from ..pipeline.transcript import preview
from .deps import ProjectDep, StateDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects/{project_id}/storyboards", tags=["storyboard"])

# -- finding things ------------------------------------------------------------------------------------


def _board(project, board_id: str) -> Storyboard:
    board = next((b for b in project.storyboards if b.id == board_id), None)
    if board is None:
        raise NotFound(f"No storyboard {board_id!r}")
    return board


def _frame(board: Storyboard, frame_id: str) -> StoryboardFrame:
    frame = board.frame(frame_id)
    if frame is None:
        raise NotFound(f"No frame {frame_id!r}")
    return frame


def _pipeline(state, board: Storyboard) -> Pipeline:
    """The flow this board actually runs: the built-ins, plus the app's edits, plus its own."""
    return resolve(state.settings, board)


def _stage(state, board: Storyboard, stage_id: str) -> Stage:
    stage = _pipeline(state, board).stage(stage_id)
    if stage is None:
        raise NotFound(f"This storyboard has no {stage_id!r} step.")
    return stage


async def _run(
    state,
    project,
    board: Storyboard,
    stage: Stage,
    *,
    frame_ids: list[str] | None = None,
    options: dict | None = None,
    apply: bool = True,
) -> StageResult:
    """Run one stage for one request, closing whatever it opened."""
    # A step that runs a workflow gets the workflow ComfyUI has *now*, not the one imported last week.
    bound = slot_workflow(project, board, stage)
    if bound is not None:
        await state.sync_workflow(project, bound)

    ctx = StageContext(state=state, project=project, board=board, apply=apply)
    try:
        return await run_stage(ctx, stage, frame_ids=frame_ids, options=options)
    finally:
        await ctx.close()


# -- storyboards ---------------------------------------------------------------------------------------


class CreateBoardRequest(BaseModel):
    name: str = "Storyboard"
    premise: str = ""
    style: str = ""
    aspect: str = "16:9"


class UpdateBoardRequest(BaseModel):
    name: str | None = None
    premise: str | None = None
    style: str | None = None
    aspect: str | None = None
    binding: StoryboardBinding | None = None


@router.get("")
def list_boards(project: ProjectDep) -> list[Storyboard]:
    return project.storyboards


@router.post("", status_code=201)
def create_board(state: StateDep, project: ProjectDep, body: CreateBoardRequest) -> Storyboard:
    board = Storyboard(**body.model_dump())
    project.storyboards.append(board)
    state.store.save(project)
    return board


@router.get("/{board_id}")
def get_board(project: ProjectDep, board_id: str) -> Storyboard:
    return _board(project, board_id)


@router.patch("/{board_id}")
def update_board(
    state: StateDep, project: ProjectDep, board_id: str, body: UpdateBoardRequest
) -> Storyboard:
    board = _board(project, board_id)
    # The attributes themselves, not their dumped dicts: `model_dump` turns the binding into a plain
    # dict, and assigning that leaves the board holding something that only looks like a binding.
    for field in ("name", "premise", "style", "aspect"):
        value = getattr(body, field)
        if value is not None:
            setattr(board, field, value)

    if body.binding is not None:
        # Only the fields that were actually sent. A binding holds two independent halves — what draws a
        # frame and what animates it — and a caller changing the drawing half would otherwise silently
        # take the animating half with it, leaving a board that has forgotten how to build a shot.
        # `model_fields_set` is what tells an omitted field apart from one sent as its default.
        for name in body.binding.model_fields_set:
            setattr(board.binding, name, getattr(body.binding, name))
    board.touch()
    state.store.save(project)
    return board


@router.delete("/{board_id}", status_code=204)
def delete_board(state: StateDep, project: ProjectDep, board_id: str) -> None:
    board = _board(project, board_id)
    # The stills shot is the board's own scaffolding; the shots it produced are the user's work and stay.
    project.shots = [s for s in project.shots if s.template_edit_id != f"{STILLS_PREFIX}{board.id}"]
    project.storyboards = [b for b in project.storyboards if b.id != board_id]
    state.store.save(project)


def _model_used(result: StageResult) -> str:
    """Which model actually answered, for the event the UI reports."""
    return next((r.model for r in result.runs if r.model), "")


# -- the flow itself -------------------------------------------------------------------------------------


def _describe_stage(stage: Stage, board: Storyboard) -> dict[str, Any]:
    """One stage, plus what the editor needs to know about it that is not on the record itself."""
    return {
        **stage.model_dump(mode="json"),
        "edited": bool(board.pipeline and stage.id in board.pipeline.stages),
        "stale": stage_is_stale(stage),
        "writable": sinks.destinations(scope=stage.scope),
    }


@router.get("/{board_id}/pipeline")
def get_pipeline(state: StateDep, project: ProjectDep, board_id: str) -> dict[str, Any]:
    """The flow this board runs, with the tokens its templates may use."""
    board = _board(project, board_id)
    pipeline = resolve(state.settings, board)
    return {
        "pipeline": pipeline.model_dump(mode="json"),
        "stages": [_describe_stage(s, board) for s in pipeline.stages],
        # Rendered against a real frame where there is one, so the palette shows what a token is worth
        # rather than only what it is called.
        "tokens": build_context(
            project, board, board.frames[0] if board.frames else None
        ),
    }


@router.get("/{board_id}/pipeline/builtin")
def get_builtin_pipeline(project: ProjectDep, board_id: str) -> Pipeline:
    """The defaults, so the panel can show what a stage would go back to."""
    _board(project, board_id)
    return builtin_pipeline()


@router.put("/{board_id}/pipeline/stages/{stage_id}")
def put_stage(
    state: StateDep, project: ProjectDep, board_id: str, stage_id: str, body: Stage
) -> dict[str, Any]:
    """Change one stage for this board, leaving every other one tracking the defaults."""
    board = _board(project, board_id)
    if body.id != stage_id:
        raise ValidationFailed(f"That is step {body.id!r}, but the address says {stage_id!r}.")
    for output in body.outputs:
        sinks.check(output.writes, scope=body.scope)

    board.pipeline = overlay_with(board.pipeline, body)
    board.touch()
    state.store.save(project)
    return get_pipeline(state, project, board_id)


@router.delete("/{board_id}/pipeline/stages/{stage_id}")
def reset_stage(
    state: StateDep, project: ProjectDep, board_id: str, stage_id: str
) -> dict[str, Any]:
    """Put one stage back to the default it was edited away from."""
    board = _board(project, board_id)
    board.pipeline = overlay_without(board.pipeline, stage_id)
    board.touch()
    state.store.save(project)
    return get_pipeline(state, project, board_id)


@router.delete("/{board_id}/pipeline")
def reset_pipeline(state: StateDep, project: ProjectDep, board_id: str) -> dict[str, Any]:
    """Drop every edit this board made, back to the app's defaults."""
    board = _board(project, board_id)
    board.pipeline = None
    board.touch()
    state.store.save(project)
    return get_pipeline(state, project, board_id)


class RunStageRequest(BaseModel):
    frame_ids: list[str] | None = None
    #: Per-invocation knobs the stage itself does not own: `count`, `append`, `reroll`.
    options: dict[str, Any] = {}


@router.post("/{board_id}/pipeline/stages/{stage_id}/run")
async def run_one_stage(
    state: StateDep, project: ProjectDep, board_id: str, stage_id: str, body: RunStageRequest
) -> dict[str, Any]:
    """Run a single stage, whichever kind it is.

    A `comfy` stage comes back with a run id and finishes later; everything else has already finished by
    the time this returns. The caller knows which from the stage's kind, which it already has.
    """
    board = _board(project, board_id)
    result = await _run(
        state, project, board, _stage(state, board, stage_id),
        frame_ids=body.frame_ids, options=body.options,
    )
    board.touch()
    state.store.save(project)
    return {
        "stage_id": result.stage_id,
        "status": result.status,
        "run_id": result.run_id,
        "step_ids": result.step_ids,
        "detail": {k: v for k, v in result.detail.items() if k != "shot"},
        "runs": [r.model_dump(mode="json") for r in result.runs],
    }


class RunPipelineRequest(BaseModel):
    #: Which steps to run. Omitted means every enabled one, in order.
    stage_ids: list[str] | None = None
    #: Which frames the per-frame steps apply to. Omitted means all of them — but the UI should pass the
    #: selection, because nine frames landing and one being wrong is the loop this is actually for.
    frame_ids: list[str] | None = None


@router.post("/{board_id}/pipeline/run", status_code=202)
async def run_pipeline(
    state: StateDep, project: ProjectDep, board_id: str, body: RunPipelineRequest
) -> dict[str, Any]:
    """Run the whole flow in the background, reporting itself over the event stream.

    Returns immediately: drawing alone takes minutes, and a request held open for it is a request a
    reload will orphan.
    """
    board = _board(project, board_id)
    run = await state.pipelines.start(
        project, board, stage_ids=body.stage_ids, frame_ids=body.frame_ids
    )
    return run.model_dump(mode="json")


@router.get("/{board_id}/pipeline/run")
def active_pipeline_run(state: StateDep, project: ProjectDep, board_id: str) -> dict[str, Any] | None:
    """Whatever the flow is doing right now, for a page that has just been opened."""
    board = _board(project, board_id)
    run = state.pipelines.active(board.id)
    return run.model_dump(mode="json") if run else None


@router.post("/{board_id}/pipeline/cancel")
async def cancel_pipeline_run(
    state: StateDep, project: ProjectDep, board_id: str
) -> dict[str, Any]:
    """Stop the flow, and the drawing run it is waiting on."""
    board = _board(project, board_id)
    run = state.pipelines.active(board.id)
    if run is None:
        return {"cancelled": False}
    return {"cancelled": await state.pipelines.cancel(run.id), "pipeline_run_id": run.id}


# -- what was actually sent ------------------------------------------------------------------------------


@router.get("/{board_id}/stage-runs")
def list_stage_runs(
    state: StateDep,
    project: ProjectDep,
    board_id: str,
    stage_id: str | None = None,
    frame_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The transcript, newest first — previews only, so a long history stays cheap to list."""
    board = _board(project, board_id)
    found = state.store.list_stage_runs(
        project.id, board.id, stage_id=stage_id, frame_id=frame_id, limit=max(1, min(200, limit))
    )
    return [preview(entry) for entry in found]


@router.get("/{board_id}/stage-runs/{stage_run_id}")
def get_stage_run(
    state: StateDep, project: ProjectDep, board_id: str, stage_run_id: str
) -> StageRun:
    """One exchange in full: what was sent, what came back, and where it went."""
    board = _board(project, board_id)
    return state.store.load_stage_run(project.id, board.id, stage_run_id)


@router.delete("/{board_id}/stage-runs", status_code=204)
def clear_stage_runs(state: StateDep, project: ProjectDep, board_id: str) -> None:
    board = _board(project, board_id)
    state.store.clear_stage_runs(project.id, board.id)


# -- writing -------------------------------------------------------------------------------------------


class WriteRequest(BaseModel):
    #: How many shots to ask for. Omitted, the setting decides.
    frames: int | None = None
    #: Add to what is there rather than replacing it.
    append: bool = False


@router.post("/{board_id}/write")
async def write_board(
    state: StateDep, project: ProjectDep, board_id: str, body: WriteRequest
) -> Storyboard:
    """Break the premise into shots."""
    board = _board(project, board_id)
    count = max(1, min(60, body.frames or state.settings.story.default_frames))

    # The stage replaces the board's frames, which is what writing a storyboard means. Appending is a
    # per-press choice rather than a property of the step, so it is handled here: keep what was there,
    # let the stage write, then put the two together.
    kept = list(board.frames) if body.append else []
    result = await _run(
        state, project, board, _stage(state, board, "write"), options={"count": count}
    )

    written = list(board.frames)
    if kept:
        for index, frame in enumerate(written):
            frame.order = len(kept) + index
        board.frames = kept + written
    board.reorder()
    board.touch()
    state.store.save(project)

    state.events.emit(
        "storyboard.written",
        project_id=project.id,
        data={
            "storyboard_id": board.id,
            "frames": len(written),
            "model": _model_used(result),
        },
    )
    return board


@router.post("/{board_id}/characters/suggest")
async def suggest_board_characters(
    state: StateDep, project: ProjectDep, board_id: str
) -> list[StoryboardCharacter]:
    """Read the premise and propose the people in it, without adding them yet."""
    board = _board(project, board_id)
    # Not saved: proposing is not deciding, and a character list is short enough to accept by hand.
    result = await _run(
        state, project, board, _stage(state, board, "suggest_characters"), apply=False
    )
    return [c for c in result.proposed if isinstance(c, StoryboardCharacter)]


# -- frames --------------------------------------------------------------------------------------------


class FramePatch(BaseModel):
    #: Point the frame at an image you already have, instead of one this drew. A photograph, a plate, a
    #: frame grabbed from something else — all perfectly good starting points for a shot.
    asset_id: str | None = None
    title: str | None = None
    action: str | None = None
    camera: str | None = None
    image_prompt: str | None = None
    shot_prompt: str | None = None
    character_ids: list[str] | None = None
    notes: str | None = None
    order: int | None = None


@router.post("/{board_id}/frames", status_code=201)
def add_frame(state: StateDep, project: ProjectDep, board_id: str) -> StoryboardFrame:
    board = _board(project, board_id)
    frame = StoryboardFrame(order=len(board.frames), title=f"Shot {len(board.frames) + 1}")
    board.frames.append(frame)
    board.reorder()
    board.touch()
    state.store.save(project)
    return frame


@router.patch("/{board_id}/frames/{frame_id}")
def update_frame(
    state: StateDep, project: ProjectDep, board_id: str, frame_id: str, body: FramePatch
) -> StoryboardFrame:
    board = _board(project, board_id)
    frame = _frame(board, frame_id)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(frame, field, value)
    if body.asset_id is not None:
        if body.asset_id not in project.assets:
            raise ValidationFailed(f"No asset {body.asset_id!r} in this project.")
        # It has a picture now, however it got one.
        frame.status = "imaged" if frame.status == "draft" else frame.status
    board.reorder()
    board.touch()
    state.store.save(project)
    return frame


@router.delete("/{board_id}/frames/{frame_id}", status_code=204)
def delete_frame(state: StateDep, project: ProjectDep, board_id: str, frame_id: str) -> None:
    board = _board(project, board_id)
    board.frames = [f for f in board.frames if f.id != frame_id]
    board.reorder()
    board.touch()
    state.store.save(project)


# -- characters ----------------------------------------------------------------------------------------


class CharacterPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    appearance: str | None = None
    reference_asset_ids: list[str] | None = None


@router.post("/{board_id}/characters", status_code=201)
def add_character(
    state: StateDep, project: ProjectDep, board_id: str, body: CharacterPatch
) -> StoryboardCharacter:
    board = _board(project, board_id)
    character = StoryboardCharacter(**body.model_dump(exclude_none=True))
    board.characters.append(character)
    board.touch()
    state.store.save(project)
    return character


@router.patch("/{board_id}/characters/{character_id}")
def update_character(
    state: StateDep, project: ProjectDep, board_id: str, character_id: str, body: CharacterPatch
) -> StoryboardCharacter:
    board = _board(project, board_id)
    character = board.character(character_id)
    if character is None:
        raise NotFound(f"No character {character_id!r}")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(character, field, value)
    board.touch()
    state.store.save(project)
    return character


@router.delete("/{board_id}/characters/{character_id}", status_code=204)
def delete_character(
    state: StateDep, project: ProjectDep, board_id: str, character_id: str
) -> None:
    board = _board(project, board_id)
    board.characters = [c for c in board.characters if c.id != character_id]
    for frame in board.frames:
        frame.character_ids = [c for c in frame.character_ids if c != character_id]
    board.touch()
    state.store.save(project)


# -- drawing a character rather than dragging one in -------------------------------------------------------


def _character(board: Storyboard, character_id: str) -> StoryboardCharacter:
    character = board.character(character_id)
    if character is None:
        raise NotFound(f"No character {character_id!r}")
    return character


async def _keep_portrait(state, project_id: str, board_id: str, character_id: str, run_id: str) -> None:
    """Wait for the drawing to finish, then keep it as the character's reference.

    A reference has to be an *asset*: an asset id is what gets wired into a workflow's reference input, and
    a run's artifacts are cleared when its history is. So unlike a frame — where the picture on screen is
    useful long before anyone keeps it — a portrait is only worth anything once it is kept, and making that
    a second click would be a second click nobody would ever want to skip.
    """
    try:
        await _keep_portrait_now(state, project_id, board_id, character_id, run_id)
    except Exception as exc:  # noqa: BLE001 - nobody awaits this, so a failure would vanish otherwise
        logger.warning("Could not keep the reference drawn for %s: %s", character_id, exc)


async def _keep_portrait_now(
    state, project_id: str, board_id: str, character_id: str, run_id: str
) -> None:
    from ..pipeline.frames import find_stills_shot

    run = await state.orchestrator.wait(run_id)
    if run is None or run.status not in {"success", "cached"}:
        logger.info("The reference for %s was not drawn (%s)", character_id, run and run.status)
        return

    project = state.store.load(project_id)
    board = next((b for b in project.storyboards if b.id == board_id), None)
    character = board.character(character_id) if board else None
    shot = find_stills_shot(project, board) if board else None
    if board is None or character is None or shot is None:
        logger.warning(
            "Nothing to keep the reference on: board=%s character=%s shot=%s",
            bool(board), bool(character), bool(shot),
        )
        return

    step = next((s for s in shot.steps if s.name == character_id), None)
    latest = state.store.latest_step_runs(project_id, shot.id).get(step.id) if step else None
    artifact = next(
        (a for a in latest["step_run"].outputs if a.kind == "image"), None
    ) if latest else None
    if artifact is None:
        logger.warning(
            "The reference for %s drew nothing to keep: step=%s, a run of it=%s, images=%s",
            character_id, step and step.id, bool(latest),
            [a.kind for a in latest["step_run"].outputs] if latest else [],
        )
        return

    asset = Asset(
        name=f"{character.name or 'character'} · reference",
        kind="image",
        path=artifact.path,
        thumb=artifact.thumb,
        sha256=artifact.sha256,
        meta=dict(artifact.meta),
        created=utcnow(),
        source=AssetSource(shot_id=shot.id, step_id=step.id, port_key=artifact.port_key),
    )
    project.assets[asset.id] = asset
    character.reference_asset_ids.append(asset.id)
    board.touch()
    state.store.save(project)
    state.events.emit(
        "project.changed", project_id=project_id, data={"action": "character_reference_drawn"}
    )


@router.post("/{board_id}/characters/{character_id}/portrait", status_code=202)
async def draw_character(
    state: StateDep, project: ProjectDep, board_id: str, character_id: str
) -> dict[str, Any]:
    """Draw this character's reference picture with the board's text-to-image workflow.

    The alternative is finding a picture of an imaginary person somewhere else and dragging it in, which
    for a character who does not exist means generating it in ComfyUI and exporting it by hand — the same
    workflow, done the long way round.
    """
    board = _board(project, board_id)
    character = _character(board, character_id)
    if not character.appearance.strip():
        raise ValidationFailed(
            f"{character.name or 'This character'} has no appearance written yet, so there is nothing to "
            "draw. Describe them first, or press Find them to have one written."
        )

    stage = _stage(state, board, "draw")
    workflow, slot = workflow_for(project, board, stage.slot)
    await state.sync_workflow(project, workflow)

    shot = stills_shot(project, board)
    step = ensure_drawing_step(
        project, board, shot, workflow,
        owner_id=character.id,
        # Below the frames, so a canvas holding both reads in the order the work happens.
        order=len(board.frames) + board.characters.index(character),
        prompt=render(PORTRAIT_PROMPT, build_context(project, board, character=character))[0],
        slot=slot,
    )
    state.store.save(project)

    run = await state.orchestrator.start(project, shot, mode="step", step_ids=[step.id], force=True)
    # Keeping it is what makes it usable, so it happens by itself once the picture exists.
    state.spawn(
        _keep_portrait(state, project.id, board.id, character.id, run.id),
        name=f"portrait:{character.id}",
    )
    return {"run_id": run.id, "shot_id": shot.id, "step_id": step.id, "workflow": workflow.name}


# -- what a workflow can actually take -------------------------------------------------------------------


class WorkflowSurface(BaseModel):
    """What a workflow offers the storyboard, so the UI can bind it rather than guess."""

    workflow_id: str
    name: str
    #: Parameters that could carry a prompt: the text ones.
    text_params: list[dict[str, str]] = []
    #: Image *inputs* — ports something else can be wired into. The first is usually the starting image;
    #: any beyond that are where character references can go.
    image_ports: list[dict[str, str]] = []
    #: Image outputs, which is what makes a workflow usable as the one that draws.
    image_outputs: list[dict[str, str]] = []
    video_outputs: list[dict[str, str]] = []


def _surface(project, workflow_id: str | None) -> WorkflowSurface | None:
    workflow = project.workflow(workflow_id or "")
    if workflow is None:
        return None
    return WorkflowSurface(
        workflow_id=workflow.id,
        name=workflow.name,
        # Only what a prompt could actually go in. The set used to be every kind there is, which made
        # this every parameter of the workflow — so the prompt picker offered seeds and widths, and
        # choosing one wrote a sentence into an int for the injector to quietly discard.
        text_params=[
            {"key": p.key, "label": p.display_name}
            for p in workflow.params
            if p.kind == "string" and not p.is_seed
        ],
        image_ports=[
            {"key": p.key, "label": p.display_name}
            for p in workflow.ports
            if p.direction == "in" and p.kind in {"image", "mask"}
        ],
        image_outputs=[
            {"key": p.key, "label": p.display_name}
            for p in workflow.ports
            if p.direction == "out" and p.kind == "image"
        ],
        video_outputs=[
            {"key": p.key, "label": p.display_name}
            for p in workflow.ports
            if p.direction == "out" and p.kind == "video"
        ],
    )


@router.get("/{board_id}/surfaces")
def board_surfaces(project: ProjectDep, board_id: str) -> dict[str, Any]:
    """What the bound workflows offer, and what is missing for what the board is trying to do.

    The reference-image question is the interesting one. A workflow that takes only a starting image can
    still make the shot — it simply cannot be told what the characters look like. Rather than refusing, or
    silently dropping the references, that is reported as a warning naming the thing to fix.
    """
    board = _board(project, board_id)
    image = _surface(project, board.binding.image_workflow_id)
    video = _surface(project, board.binding.video_workflow_id)

    warnings: list[str] = []
    with_references = [c for c in board.characters if c.reference_asset_ids]

    if image is not None and not image.image_outputs:
        warnings.append(f"{image.name!r} has no image output, so it cannot draw the frames.")
    if video is not None and not video.image_ports:
        warnings.append(
            f"{video.name!r} has no image input, so there is nowhere to put the frame's still."
        )

    # "Spare" means beyond the one the starting image already occupies.
    spare_video = [
        port for port in (video.image_ports if video else [])
        if port["key"] != board.binding.video_image_port
    ]
    if with_references and image is not None and not image.image_ports:
        warnings.append(
            f"{len(with_references)} character(s) have reference images, but {image.name!r} takes no "
            "reference input. Choose a workflow with one — an IP-Adapter or reference-image input — and "
            "pick which input it is, or the references will be ignored."
        )
    if with_references and video is not None and not spare_video:
        warnings.append(
            f"{video.name!r} has no image input to spare for character references; its only one is "
            "carrying the frame's still."
        )

    # A bound workflow with a text input and nothing chosen to receive the prompt is the quiet failure:
    # it draws, or it animates, and every word written about the shot goes nowhere.
    if image is not None and image.text_params and not board.binding.image_prompt_param:
        warnings.append(
            f"No parameter of {image.name!r} is set to receive the image prompt, so the frames would be "
            "drawn from whatever the workflow already had in it. Choose one under 'Its prompt'."
        )
    if video is not None and video.text_params and not board.binding.video_prompt_param:
        warnings.append(
            f"No parameter of {video.name!r} is set to receive the motion prompt, so the shots would "
            "animate the still while ignoring what was written about how it moves. Choose one under "
            "'Its prompt'."
        )

    return {
        "image": image.model_dump() if image else None,
        "video": video.model_dump() if video else None,
        "spare_video_image_ports": spare_video,
        "characters_with_references": [c.id for c in with_references],
        "warnings": warnings,
    }


# -- drawing the frames ----------------------------------------------------------------------------------


@router.post("/{board_id}/stills")
def build_stills(state: StateDep, project: ProjectDep, board_id: str) -> dict[str, Any]:
    """Make sure every frame has a step that would draw it, and return the shot they live in."""
    board = _board(project, board_id)
    stage = _stage(state, board, "draw")
    workflow, slot = workflow_for(project, board, stage.slot)
    shot = stills_shot(project, board)

    steps = {
        frame.id: ensure_step(
            project, board, shot, workflow, frame,
            prompt=render(stage.prompt, build_context(project, board, frame))[0], slot=slot,
        ).id
        for frame in sorted(board.frames, key=lambda f: f.order)
    }

    state.store.save(project)
    return {"shot_id": shot.id, "steps": steps, "workflow": workflow.name}


class DrawRequest(BaseModel):
    #: Which frames to draw. Omitted or empty means all of them.
    frame_ids: list[str] | None = None
    #: Draw a *different* picture rather than the same one again: a new seed for each frame asked for.
    reroll: bool = False


@router.post("/{board_id}/draw", status_code=202)
async def draw_frames(
    state: StateDep, project: ProjectDep, board_id: str, body: DrawRequest
) -> dict[str, Any]:
    """Build the steps for these frames and run them, in one request.

    One endpoint for both "draw the board" and "draw this one again", because they differ only in which
    steps are selected — and going through the same path means a single frame redrawn is cached, previewed
    and reported exactly like a whole board.
    """
    board = _board(project, board_id)
    result = await _run(
        state, project, board, _stage(state, board, "draw"),
        frame_ids=body.frame_ids, options={"reroll": body.reroll},
    )
    return {
        "run_id": result.run_id,
        "shot_id": result.detail.get("shot_id"),
        "steps": result.step_ids,
        "workflow": result.detail.get("workflow"),
        "seeded": result.detail.get("seeded", False),
    }


@router.get("/{board_id}/stills")
def stills_state(state: StateDep, project: ProjectDep, board_id: str) -> dict[str, Any]:
    """Every frame's current picture and the step that draws it."""
    return stills_view(state.store, project, _board(project, board_id))


@router.post("/{board_id}/frames/{frame_id}/capture", status_code=201)
async def capture_still(
    state: StateDep, project: ProjectDep, board_id: str, frame_id: str
) -> dict[str, Any]:
    """Keep this frame's still as a project asset.

    The bytes are not copied — a run's artifacts already live in the content-addressed store — so this is
    about permanence and naming: an asset survives the run being cleared, and can be dropped onto a
    canvas or wired in as a reference like any other piece of media.
    """
    board = _board(project, board_id)
    frame = _frame(board, frame_id)
    stage = _stage(state, board, "capture")
    # Asked for explicitly, so it keeps the picture even when one is already kept — the stage's own
    # "skip if there is nothing new" is for a pipeline running past it, not for a button press.
    result = await _run(
        state, project, board, stage.model_copy(update={"only_if_missing": False}),
        frame_ids=[frame.id],
    )
    state.store.save(project)
    return {"asset_id": result.detail.get("asset_id"), "frame": frame.model_dump(mode="json")}


@router.post("/{board_id}/frames/{frame_id}/describe")
async def describe_still(
    state: StateDep, project: ProjectDep, board_id: str, frame_id: str
) -> StoryboardFrame:
    """Look at what was actually drawn and rewrite this frame's prompts from it.

    The first image prompt was written blind, from the premise. The generator then made its own choices —
    who is in shot, what they are wearing, where the light falls — and the motion prompt needs to talk
    about *that* picture, not the one that was imagined.
    """
    board = _board(project, board_id)
    frame = _frame(board, frame_id)
    # Which picture gets looked at, and the refusal when there is none, are the stage's own business —
    # it attaches whatever `current()` says the frame is showing, reroll included.
    await _run(state, project, board, _stage(state, board, "describe"), frame_ids=[frame.id])
    board.touch()
    state.store.save(project)
    return frame


@router.post("/{board_id}/frames/{frame_id}/shot", status_code=201)
async def build_shot(state: StateDep, project: ProjectDep, board_id: str, frame_id: str) -> Shot:
    """Turn this frame into a real shot: its still in, its motion prompt on, its characters wired.

    A shot rather than anything storyboard-specific, so from here on it is ordinary work — run it, edit
    it in ComfyUI, drop it on the timeline. The storyboard's job ends once the shot exists.
    """
    board = _board(project, board_id)
    frame = _frame(board, frame_id)
    result = await _run(state, project, board, _stage(state, board, "shot"), frame_ids=[frame.id])
    shot = result.detail["shot"]
    state.store.save(project)
    state.events.emit(
        "project.changed", project_id=project.id, data={"action": "storyboard_shot_created"}
    )
    return shot
