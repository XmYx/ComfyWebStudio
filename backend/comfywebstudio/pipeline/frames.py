"""What a storyboard does to a project: drawing frames, finding their pictures, keeping them, wiring shots.

Lifted out of the route handlers so it can be called by a stage as easily as by a request. Nothing here
knows about FastAPI or about ``AppState`` — a project, a board, and a store are the whole world — which is
what lets the same code serve ``POST /draw`` and a ``comfy`` stage in a pipeline run without either
becoming a special case of the other.

Two things are passed in that used to be computed here, and both are the point of the exercise:

* **the prompt**, so a stage's editable template decides what a workflow is asked for; and
* **the slot**, so which workflow does the job is a property of the binding rather than of the function.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple

from ..core.errors import ValidationFailed
from ..core.models import (
    VALUE_PORT,
    Artifact,
    Asset,
    AssetSource,
    Link,
    Shot,
    Step,
    ValueNode,
    Vec2,
    WorkflowRef,
    utcnow,
)
from ..core.storyboard import Storyboard, StoryboardFrame, WorkflowSlot

#: Marks the hidden shot that holds a storyboard's stills, so it stays out of the shot list and the
#: timeline the same way a template editing session does.
STILLS_PREFIX = "storyboard-stills:"


# -- the workflow behind a slot --------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotRules:
    """What a slot needs before it can be used, and how to say it is missing.

    The wording is per-slot because the *question* is: a drawing workflow needs to know which parameter
    is the prompt, an animating one needs to know which input takes the picture. Told properly, either is
    a thing the user can go and fix.
    """

    missing_workflow: str
    required: str
    missing_required: str


SLOT_RULES: dict[str, SlotRules] = {
    "image": SlotRules(
        "Choose the workflow that draws the frames first.",
        "prompt_param",
        "Which parameter of {name} is the prompt? Choose it before drawing.",
    ),
    "video": SlotRules(
        "Choose the workflow that turns a still into a shot first.",
        "image_port",
        "Which input of {name} takes the starting image? Choose it first.",
    ),
}

GENERIC_RULES = SlotRules(
    "This step has no workflow chosen. Pick one on the setup panel.",
    "",
    "",
)


def slot_workflow(project, board: Storyboard, stage) -> WorkflowRef | None:
    """The workflow a stage will run, if it runs one at all.

    Used to bring that workflow up to date with ComfyUI before the stage uses it, which is why it answers
    ``None`` rather than raising: a stage with nothing bound yet will refuse for itself, with a better
    message than anything this could give.
    """
    if stage.kind not in {"comfy", "shot"}:
        return None
    return project.workflow(board.binding.slot(stage.slot).workflow_id or "")


def workflow_for(project, board: Storyboard, slot_name: str) -> tuple[WorkflowRef, WorkflowSlot]:
    """The workflow bound to a slot and its wiring, or the reason the slot cannot be used."""
    slot = board.binding.slot(slot_name)
    rules = SLOT_RULES.get(slot_name, GENERIC_RULES)

    workflow = project.workflow(slot.workflow_id or "")
    if workflow is None:
        raise ValidationFailed(rules.missing_workflow)
    if rules.required and not getattr(slot, rules.required, ""):
        raise ValidationFailed(rules.missing_required.format(name=repr(workflow.name)))
    return workflow, slot


# -- the hidden shot the stills live in ------------------------------------------------------------------


def stills_shot(project, board: Storyboard) -> Shot:
    """The hidden shot holding one step per frame, created on demand.

    Marked with `template_edit_id` for the same reason a template session is: it keeps it out of the shot
    list and out of the timeline, where it would be noise rather than work.
    """
    marker = f"{STILLS_PREFIX}{board.id}"
    shot = next((s for s in project.shots if s.template_edit_id == marker), None)
    if shot is None:
        shot = Shot(name=f"{board.name} · stills", template_edit_id=marker)
        project.shots.append(shot)
    return shot


def find_stills_shot(project, board: Storyboard) -> Shot | None:
    """The stills shot if it exists, without making one — for the read-only paths."""
    marker = f"{STILLS_PREFIX}{board.id}"
    return next((s for s in project.shots if s.template_edit_id == marker), None)


def reference_nodes(
    project, board: Storyboard, frame: StoryboardFrame, shot: Shot, step: Step, ports: list[str]
) -> None:
    """Wire this frame's characters' reference images into the given inputs, in order.

    More characters than inputs is normal — a two-hander in a workflow with one reference slot — so the
    extras are simply not wired rather than being an error. The surfaces endpoint is where that gets
    said out loud.
    """
    assets: list[str] = []
    for character_id in frame.character_ids:
        character = board.character(character_id)
        if character is None:
            continue
        assets += [a for a in character.reference_asset_ids if a in project.assets]

    for index, (port_key, asset_id) in enumerate(zip(ports, assets, strict=False)):
        node = ValueNode(
            kind="media",
            name=f"ref · {project.assets[asset_id].name}",
            asset_id=asset_id,
            media_kind="image",
            ui_pos=Vec2(x=step.ui_pos.x - 320.0, y=step.ui_pos.y + 120.0 * (index + 1)),
        )
        shot.nodes.append(node)
        shot.links.append(
            Link(from_step=node.id, from_port=VALUE_PORT, to_step=step.id, to_port=port_key)
        )


def ensure_drawing_step(
    project,
    board: Storyboard,
    shot: Shot,
    workflow: WorkflowRef,
    *,
    owner_id: str,
    order: int,
    prompt: str,
    slot: WorkflowSlot,
    frame: StoryboardFrame | None = None,
) -> Step:
    """The step that draws for this owner, created if it has none, with its prompt brought up to date.

    An owner is a frame or a character: a frame's still and a character's reference picture are the same
    job — run the drawing workflow with one prompt — so they are the same step, named by whose it is.
    That naming is what ties the two together without a second mapping to keep in step.

    Idempotent, which is what makes "I edited frame 3's prompt, draw it again" a one-step run rather than
    a rebuild.

    `prompt` arrives already rendered. That is the whole difference between a drawing step whose wording
    is fixed in code and one a user can edit.
    """
    step = next((s for s in shot.steps if s.name == owner_id), None)
    if step is None:
        step = Step(
            name=owner_id,
            workflow_id=workflow.id,
            ui_pos=Vec2(x=40.0, y=40.0 + 220.0 * order),
        )
        shot.steps.append(step)
        # Only a frame has characters to feed in. A character's own portrait is what a reference *is*.
        if frame is not None:
            reference_nodes(project, board, frame, shot, step, slot.reference_params)

    if step.workflow_id != workflow.id:
        # The board was re-bound to a different workflow. Values set for the old one are meaningless
        # against the new, and left in place they would be silently ignored.
        step.workflow_id = workflow.id
        known = {p.key for p in workflow.params}
        step.param_overrides = {k: v for k, v in step.param_overrides.items() if k in known}

    step.param_overrides[slot.prompt_param] = prompt
    return step


def ensure_step(
    project,
    board: Storyboard,
    shot: Shot,
    workflow: WorkflowRef,
    frame: StoryboardFrame,
    *,
    prompt: str,
    slot: WorkflowSlot,
) -> Step:
    """The step that draws this frame. See :func:`ensure_drawing_step`."""
    return ensure_drawing_step(
        project, board, shot, workflow,
        owner_id=frame.id, order=frame.order, prompt=prompt, slot=slot, frame=frame,
    )


def reseed(step: Step, workflow: WorkflowRef) -> bool:
    """Give this step a new seed, if the workflow has one. True when it did.

    A seed override rather than merely ignoring the cache: it changes the cache key, so the new picture is
    cached like any other, and the number that produced it is recorded on the step where it can be read,
    kept or edited.
    """
    seeds = [p for p in workflow.params if p.is_seed]
    for param in seeds:
        step.param_overrides[param.key] = random.randrange(2**31 - 1)
    return bool(seeds)


# -- which picture a frame currently has -----------------------------------------------------------------


class CurrentStill(NamedTuple):
    """The two pictures a frame can have, and which of them is the current one.

    A frame's image can come from either end: the still its step last drew, or an asset — one kept from an
    earlier draw, or a photograph the user pointed the frame at instead. Both are legitimate, so neither
    wins by rule; the newer one wins. That is what makes "reroll this frame" show the reroll, and "drop a
    plate onto frame 4" show the plate, without either needing to clear the other.
    """

    shot: Shot | None
    step: Step | None
    #: The image the step last produced, if it has been run.
    artifact: Artifact | None
    #: The asset the frame points at, if any.
    asset: Asset | None
    status: str | None
    run_id: str | None
    #: True when the asset is the picture on screen; False when the freshly drawn still is.
    use_asset: bool

    @property
    def path(self) -> str | None:
        if self.use_asset and self.asset is not None:
            return self.asset.path
        if self.artifact is not None:
            return self.artifact.path
        return self.asset.path if self.asset is not None else None

    @property
    def thumb(self) -> str | None:
        if self.use_asset and self.asset is not None:
            return self.asset.thumb
        if self.artifact is not None:
            return self.artifact.thumb
        return self.asset.thumb if self.asset is not None else None


def asset_is_current(
    asset: Asset | None, artifact: Artifact | None, drawn_at: datetime | None
) -> bool:
    """Whether the frame's asset, rather than the last still it drew, is its current picture."""
    if asset is None:
        return False
    if artifact is None:
        return True
    if asset.sha256 and asset.sha256 == artifact.sha256:
        return True  # the same picture either way; prefer the named, permanent one
    kept_at = asset.generated or asset.created
    if drawn_at is None or kept_at is None:
        return False
    return kept_at >= drawn_at


def current(
    store, project, board: Storyboard, frame: StoryboardFrame, latest: dict | None = None
) -> CurrentStill:
    """What this frame's picture is right now, and where it came from.

    ``latest`` is the shot's latest step runs, passed in when several frames are being examined at once so
    the run history is read once rather than once per frame.
    """
    shot = find_stills_shot(project, board)
    step = next((s for s in shot.steps if s.name == frame.id), None) if shot else None
    asset = project.assets.get(frame.asset_id or "")

    entry = None
    if shot is not None and step is not None:
        if latest is None:
            latest = store.latest_step_runs(project.id, shot.id)
        entry = latest.get(step.id)

    artifact = drawn_at = None
    status = run_id = None
    if entry is not None:
        step_run = entry["step_run"]
        artifact = next((a for a in step_run.outputs if a.kind == "image"), None)
        status, run_id = step_run.status, entry["run_id"]
        drawn_at = step_run.finished

    return CurrentStill(
        shot=shot,
        step=step,
        artifact=artifact,
        asset=asset,
        status=status,
        run_id=run_id,
        use_asset=asset_is_current(asset, artifact, drawn_at),
    )


def stills_view(store, project, board: Storyboard) -> dict[str, Any]:
    """Every frame's current picture and the step that draws it.

    This is what lets the frame strip show results without the user first having to keep them as assets:
    a run's artifacts are already on disk, so the picture exists the moment the step finishes. The step id
    matters as much as the image — it is what a live progress event is keyed by.
    """
    shot = find_stills_shot(project, board)
    latest = store.latest_step_runs(project.id, shot.id) if shot else {}

    frames: dict[str, Any] = {}
    for frame in board.frames:
        showing = current(store, project, board, frame, latest)
        frames[frame.id] = {
            "step_id": showing.step.id if showing.step else None,
            "status": showing.status,
            "run_id": showing.run_id,
            "image": showing.path,
            "thumb": showing.thumb,
            "source": "asset" if showing.use_asset else ("still" if showing.artifact else None),
            #: Nothing new to keep: the asset already holds the picture on screen.
            "kept": showing.use_asset and showing.asset is not None,
        }

    workflow = project.workflow(board.binding.image_workflow_id or "")
    return {
        "shot_id": shot.id if shot else None,
        "workflow": workflow.name if workflow else None,
        "frames": frames,
    }


def capture(project, board: Storyboard, frame: StoryboardFrame, showing: CurrentStill) -> Asset:
    """Keep this frame's drawn still as a project asset.

    The bytes are not copied — a run's artifacts already live in the content-addressed store — so this is
    about permanence and naming: an asset survives the run being cleared, and can be dropped onto a canvas
    or wired in as a reference like any other piece of media.
    """
    if showing.artifact is None or showing.shot is None or showing.step is None:
        raise ValidationFailed("That frame has not been drawn yet. Run its step first.")

    artifact = showing.artifact
    asset = Asset(
        name=frame.title or f"Frame {frame.order + 1}",
        kind="image",
        path=artifact.path,
        thumb=artifact.thumb,
        sha256=artifact.sha256,
        meta=dict(artifact.meta),
        created=utcnow(),
        # Recorded so the asset can be refreshed from the step that drew it, exactly like any other
        # captured output — and so a later reroll can tell its own work from a picture the user chose.
        source=AssetSource(
            shot_id=showing.shot.id, step_id=showing.step.id, port_key=artifact.port_key
        ),
    )
    project.assets[asset.id] = asset
    frame.asset_id = asset.id
    frame.status = "imaged"
    board.touch()
    return asset


def make_shot(
    store, project, board: Storyboard, frame: StoryboardFrame, *, prompt: str, slot: WorkflowSlot
) -> Shot:
    """Turn this frame into a real shot: its still in, its motion prompt on, its characters wired.

    A shot rather than anything storyboard-specific, so from here on it is ordinary work — run it, edit it
    in ComfyUI, drop it on the timeline. The storyboard's job ends once the shot exists.
    """
    workflow = project.workflow(slot.workflow_id or "")
    if workflow is None:
        raise ValidationFailed(SLOT_RULES["video"].missing_workflow)
    if not slot.image_port:
        raise ValidationFailed(
            SLOT_RULES["video"].missing_required.format(name=repr(workflow.name))
        )
    if prompt.strip() and not slot.prompt_param:
        # There is a motion prompt and nowhere to put it. Building the shot anyway makes a step that
        # animates the picture while ignoring every word written about how it should move — which looks
        # like it worked and is the hardest kind of wrong to notice.
        raise ValidationFailed(
            f"This frame has a motion prompt, but no parameter of {workflow.name!r} is set to receive "
            "it. Choose one under 'Its prompt' for the image-to-video workflow, or clear the frame's "
            "motion prompt."
        )

    existing = next((s for s in project.shots if s.id == frame.shot_id), None) if frame.shot_id else None
    if existing is not None:
        raise ValidationFailed(f"This frame is already {existing.name!r}. Delete that shot to rebuild it.")

    # Whatever picture the frame is showing is the one that gets wired in, so a frame drawn and rerolled
    # four times animates the fourth. Keeping it as an asset is what a shot needs, and that is bookkeeping
    # rather than a decision — so it happens here rather than being reported as an error.
    showing = current(store, project, board, frame)
    if not showing.use_asset or showing.asset is None:
        capture(project, board, frame, showing)

    shot = Shot(name=frame.title or f"Shot {frame.order + 1}", notes=frame.action)
    step = Step(name=workflow.name, workflow_id=workflow.id, ui_pos=Vec2(x=380.0, y=40.0))
    shot.steps.append(step)

    still = ValueNode(
        kind="media",
        name=frame.title or "still",
        asset_id=frame.asset_id,
        media_kind="image",
        ui_pos=Vec2(x=40.0, y=40.0),
    )
    shot.nodes.append(still)
    shot.links.append(
        Link(from_step=still.id, from_port=VALUE_PORT, to_step=step.id, to_port=slot.image_port)
    )

    # The motion prompt goes in the same way the picture does: a node on the canvas, wired to the input.
    # Buried in the step's parameters it is a value you have to go looking for and cannot share; on the
    # canvas it sits next to the still it belongs to, reads as part of the shot, and can be re-wired into
    # a second step without being retyped.
    #
    # Only when the chosen parameter is a real input port, which is the usual case — a `WS Text Input`
    # yields a port and a parameter under one key. A raw widget or a promoted subgraph input has no port
    # to link to, so that falls back to setting the value on the step.
    if slot.prompt_param:
        port = next(
            (
                p for p in workflow.ports
                if p.key == slot.prompt_param and p.direction == "in" and p.kind == "string"
            ),
            None,
        )
        if port is not None:
            motion = ValueNode(
                kind="string",
                name=port.label or "motion",
                value=prompt,
                ui_pos=Vec2(x=40.0, y=260.0),
            )
            shot.nodes.append(motion)
            shot.links.append(
                Link(from_step=motion.id, from_port=VALUE_PORT, to_step=step.id, to_port=port.key)
            )
        else:
            step.param_overrides[slot.prompt_param] = prompt

    # Character references go into whatever inputs are left over, in order.
    spare = [port for port in slot.reference_params if port != slot.image_port]
    reference_nodes(project, board, frame, shot, step, spare)

    project.shots.append(shot)
    frame.shot_id = shot.id
    frame.status = "shot"
    board.touch()
    return shot
