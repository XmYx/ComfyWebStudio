"""Storyboards: a premise, the frames it becomes, and the people in them.

A storyboard sits between "an idea" and "a shot that renders". Each frame carries three different pieces
of writing, and keeping them apart is the whole point:

* **action** — what happens, in prose. The script.
* **image_prompt** — what the *still* should look like, for the text-to-image workflow.
* **shot_prompt** — how it should *move*, for the image-to-video workflow.

They are different jobs. A prompt that makes a good still ("wide shot, dusk, rain on the window") is not
a prompt that makes good motion ("slow push in as she turns"), and a single field would force one to be a
compromise for the other.

Which workflow does which job — and which of its parameters is the prompt, the starting image, or a
character reference — is recorded in :class:`StoryboardBinding` rather than guessed at each step, so the
same storyboard can be re-run against a different set of workflows without rewriting a word of it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .base import Base, utcnow
from .ids import new_id
from .pipeline import FrameStatus, PipelineOverlay

__all__ = [
    "FrameStatus",
    "Storyboard",
    "StoryboardBinding",
    "StoryboardCharacter",
    "StoryboardFrame",
    "WorkflowSlot",
]


class StoryboardCharacter(Base):
    """Someone who appears in the story, and the pictures that say what they look like.

    Kept on the storyboard rather than the frame because a character is the thing that has to stay
    consistent *between* frames — which is exactly what their reference images are for.
    """

    id: str = Field(default_factory=lambda: new_id("char"))
    name: str = ""
    #: Who they are: the part that belongs in the script.
    description: str = ""
    #: What they look like: the part that belongs in an image prompt.
    appearance: str = ""
    #: Project assets showing them, fed to a workflow's reference-image inputs when it has any.
    reference_asset_ids: list[str] = Field(default_factory=list)


class StoryboardFrame(Base):
    id: str = Field(default_factory=lambda: new_id("frame"))
    #: Position in the board. Held explicitly so frames can be reordered without renaming anything.
    order: int = 0
    title: str = ""
    #: What happens, in prose.
    action: str = ""
    #: How it is framed — "wide", "over the shoulder", "slow push in". Written by the model, editable.
    camera: str = ""
    #: What the still should look like.
    image_prompt: str = ""
    #: How it should move.
    shot_prompt: str = ""
    character_ids: list[str] = Field(default_factory=list)

    #: The generated still, once it exists, as a project asset.
    asset_id: str | None = None
    #: The shot built from this frame, once it exists.
    shot_id: str | None = None
    status: FrameStatus = "draft"
    notes: str = ""
    #: Values written by stages someone added, keyed by output name. The fixed fields above stay fixed —
    #: too much reads them by name — so this is where anything new goes.
    fields: dict[str, str] = Field(default_factory=dict)


class WorkflowSlot(Base):
    """One job's worth of wiring, in the shape a stage asks for it."""

    workflow_id: str | None = None
    prompt_param: str = ""
    image_port: str = ""
    reference_params: list[str] = Field(default_factory=list)


class StoryboardBinding(Base):
    """Which workflow performs which job, and which of its parameters carries what.

    Recorded rather than inferred: two workflows that both take a prompt may call the parameter anything
    at all, and guessing wrongly means a generation that runs happily and ignores everything asked of it.

    This stays on the *board* rather than moving onto the pipeline's stages, and deliberately. A workflow
    id is a key into one project's workflows; a pipeline is meant to be an app-wide default. Put the id on
    a stage and the default carries something meaningful in exactly one project — which is the same class
    of quiet wrongness the paragraph above exists to prevent.
    """

    #: Text to image: makes the frame's still.
    image_workflow_id: str | None = None
    image_prompt_param: str = ""
    #: Extra image inputs to feed character references into, when the workflow has any.
    image_reference_params: list[str] = Field(default_factory=list)

    #: Image to video: turns a still into the shot.
    video_workflow_id: str | None = None
    video_prompt_param: str = ""
    #: Which input the starting image goes to.
    video_image_port: str = ""
    video_reference_params: list[str] = Field(default_factory=list)

    #: Room for the slots a stage someone added asks for. Empty on every board that exists today.
    slots: dict[str, WorkflowSlot] = Field(default_factory=dict)

    def slot(self, name: str) -> WorkflowSlot:
        """The wiring for one named slot — what a `comfy` or `shot` stage runs against."""
        if name == "image":
            return WorkflowSlot(
                workflow_id=self.image_workflow_id,
                prompt_param=self.image_prompt_param,
                reference_params=list(self.image_reference_params),
            )
        if name == "video":
            return WorkflowSlot(
                workflow_id=self.video_workflow_id,
                prompt_param=self.video_prompt_param,
                image_port=self.video_image_port,
                reference_params=list(self.video_reference_params),
            )
        return self.slots.get(name) or WorkflowSlot()


class Storyboard(Base):
    id: str = Field(default_factory=lambda: new_id("board"))
    name: str = "Storyboard"
    #: The idea, in the user's own words. Everything else is derived from this.
    premise: str = ""
    #: A house style applied to every image prompt — "grainy 16mm, muted palette".
    style: str = ""
    #: Carried into prompts that mention it, and into the shots this becomes.
    aspect: str = "16:9"

    frames: list[StoryboardFrame] = Field(default_factory=list)
    characters: list[StoryboardCharacter] = Field(default_factory=list)
    binding: StoryboardBinding = Field(default_factory=StoryboardBinding)

    #: How this board's flow differs from the app's. None means it follows the defaults exactly — which
    #: is the point: a board that changed one prompt still inherits improvements to every other stage.
    pipeline: PipelineOverlay | None = None
    #: Values written by stages someone added, keyed by output name.
    fields: dict[str, str] = Field(default_factory=dict)

    created: datetime = Field(default_factory=utcnow)
    modified: datetime = Field(default_factory=utcnow)

    def frame(self, frame_id: str) -> StoryboardFrame | None:
        return next((f for f in self.frames if f.id == frame_id), None)

    def character(self, character_id: str) -> StoryboardCharacter | None:
        return next((c for c in self.characters if c.id == character_id), None)

    def forget_shot(self, shot_id: str) -> list[StoryboardFrame]:
        """Let go of any frame whose shot has been deleted, and say which ones those were.

        A frame remembers the shot it became so it cannot quietly build a second one. Deleting that shot
        has to undo the memory too, or the frame is left pointing at nothing — able to do neither, and
        saying it is finished.

        The status goes back to whatever the frame can still show for itself, which is how it got there in
        the first place.
        """
        touched = [f for f in self.frames if f.shot_id == shot_id]
        for frame in touched:
            frame.shot_id = None
            if frame.status == "shot":
                frame.status = (
                    "described" if frame.notes else "imaged" if frame.asset_id else "draft"
                )
        return touched

    def reorder(self) -> None:
        """Renumber after an insert or a delete, so `order` always matches the real sequence."""
        for index, frame in enumerate(sorted(self.frames, key=lambda f: f.order)):
            frame.order = index
        self.frames.sort(key=lambda f: f.order)

    def touch(self) -> None:
        self.modified = utcnow()
