"""The storyboard pipeline, as data rather than as code.

A storyboard is made in steps — write the shots, draw them, look at what came back, build the shots — and
until now each of those was a route handler with its prompt hardcoded above it. That works right up until
someone wants a different question asked, or a fifth step, or simply to *see* what was sent to the model.

So the steps are stages, and a stage is a record: what to say, which model to say it to, what shape the
answer must take, and where each part of that answer lands. Four kinds cover the flow:

* ``llm``     — ask a model something, structured.
* ``comfy``   — draw the frame by running a workflow.
* ``capture`` — keep what was drawn as a project asset.
* ``shot``    — turn the frame into a real shot.

and two scopes: ``board`` runs the stage once, ``frame`` runs it per frame.

**Overlays, not copies.** What a board or the app *stores* is only its divergence from the built-ins
(:class:`PipelineOverlay`), so a board that rewrote one prompt still picks up improvements to every other
stage, and "reset this one" is deleting a dict entry. A stored stage remembers which built-in it began as
and at what revision, which is what lets the UI say the default has moved on — the same bargain a placed
template instance makes with its library entry.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from .base import Base, utcnow
from .ids import new_id

#: How far a frame has got. Purely descriptive — nothing refuses to run because of it.
#:
#: Defined here rather than in `storyboard.py` because the dependency runs that way: a storyboard *has* a
#: pipeline overlay, and it is a stage that advances a frame's status. `core.storyboard` re-exports it.
FrameStatus = Literal["draft", "imaged", "described", "shot"]

StageKind = Literal["llm", "comfy", "capture", "shot"]
StageScope = Literal["board", "frame"]
ModelRole = Literal["write", "vision"]
FieldType = Literal["string", "text", "integer", "number", "boolean", "string_list", "object_list"]

#: Output keys are both a JSON property name and a template token, so they are held to the stricter of the
#: two: lowercase, no dots, short enough to read in a prompt.
KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: How many custom fields one frame or board may accumulate. A cap rather than a policy — it exists so a
#: stage stuck in a loop cannot grow the project file without bound.
MAX_CUSTOM_FIELDS = 32


class OutputField(Base):
    """One thing a stage asks the model for, and where the answer goes.

    The destination lives here rather than in a separate mapping on the stage. A parallel dict keyed by
    field name is a second thing to keep in step: rename a field and it dangles, delete one and it leaks.
    Here it is simply another column of the row being edited.
    """

    key: str
    type: FieldType = "string"
    #: The only instruction a constrained decoder actually reads, so it is worth writing properly.
    description: str = ""
    required: bool = True
    #: ``object_list`` only: the shape of one item.
    fields: list[OutputField] = Field(default_factory=list)
    #: Where the value lands — "frame.image_prompt", "frame.fields.wardrobe", "board.premise". Empty
    #: means propose-only: the answer is returned to the caller and nothing is written.
    writes: str = ""

    @field_validator("key")
    @classmethod
    def _usable_key(cls, value: str) -> str:
        if not KEY.match(value):
            raise ValueError(
                f"{value!r} will not work as an output name. Use lowercase letters, digits and "
                "underscores, starting with a letter — it becomes both a JSON key and a {token}."
            )
        return value


class StageModel(Base):
    """Which model answers this stage, and how loosely."""

    #: Which of the two configured models to use. Deliberately a role rather than a provider id: the
    #: Settings page promises "one that writes, one that sees", and a per-stage provider would quietly
    #: make that untrue.
    role: ModelRole = "write"
    #: Overrides the configured model for this stage alone. Empty follows the setting.
    model: str = ""
    #: None follows ``settings.story.temperature``.
    temperature: float | None = None
    #: Send the frame's current picture along. Only meaningful with a vision model.
    attach_image: bool = False


class StageRetry(Base):
    """Ask again, for just the parts that came back empty.

    A schema can insist a field is present; it cannot insist it says anything, and smaller models will
    satisfy it with "" and move on. Asking the one unanswered question on its own is a much easier thing
    to answer than all of them at once — and having it here, rather than buried in a function, means it
    can be seen, edited and turned off.
    """

    #: Output keys that being blank triggers a retry.
    when_empty: list[str] = Field(default_factory=list)
    prompt: str = ""
    temperature_delta: float = 0.2


class Stage(Base):
    #: A stable slug for the built-ins ("write", "draw", "capture", "describe", "shot") so an overlay can
    #: key against one; a generated id for stages the user added.
    id: str = Field(default_factory=lambda: new_id("stage"))
    kind: StageKind = "llm"
    scope: StageScope = "board"
    name: str = ""
    #: Shown under the stage's name in the panel. What this step is for, in a sentence.
    description: str = ""
    enabled: bool = True

    #: ``llm``: the system message.
    system: str = ""
    #: ``llm``: the user message. ``comfy``: the text put into the workflow's prompt parameter.
    #: One field, because they are the same thing — which is what makes the drawing step as editable as
    #: the writing one, rather than the flow being half data and half code.
    prompt: str = ""

    model: StageModel = Field(default_factory=StageModel)
    outputs: list[OutputField] = Field(default_factory=list)
    retry: StageRetry | None = None

    #: ``comfy`` / ``shot``: which binding slot supplies the workflow and its parameters.
    slot: str = "image"
    #: ``comfy``: a reroll varies the seed rather than only forcing a re-render, so the number that made
    #: the picture is recorded and the result stays cacheable.
    reroll_seed: bool = True
    #: ``capture`` / ``shot``: skip when the frame's picture is already an asset.
    only_if_missing: bool = True
    #: Advance the frame's status when this stage succeeds. Declarative, so a stage someone added can
    #: move a frame along too.
    sets_status: FrameStatus | None = None

    #: Set on a stored copy: which built-in it began as, and at what revision. "Reset" knows where to go
    #: back to, and the panel can say the built-in has changed since this was edited.
    builtin_id: str | None = None
    builtin_revision: int = 0


class Pipeline(Base):
    """A fully resolved, ordered pipeline — what gets run."""

    id: str = "storyboard"
    name: str = "Storyboard"
    revision: int = 1
    stages: list[Stage] = Field(default_factory=list)

    def stage(self, stage_id: str) -> Stage | None:
        return next((s for s in self.stages if s.id == stage_id), None)


class PipelineOverlay(Base):
    """What a board or the app stores: only the divergence.

    Replacements are whole stages rather than individual fields. A field-level merge means a three-way
    diff between the built-in, the app default and the board, and nobody can predict what comes out of
    one of those — whereas "this board uses its own describe stage" is a sentence you can hold in your
    head.
    """

    #: stage id -> the stage that replaces it. An id with no built-in behind it is a stage that was added.
    stages: dict[str, Stage] = Field(default_factory=dict)
    #: Stage ids in run order, including any additions. Empty means "the order of the layer below".
    order: list[str] = Field(default_factory=list)
    #: Stage ids removed at this layer. Distinct from disabling one, which the layer below still sees.
    removed: list[str] = Field(default_factory=list)
    revision: int = 1
    modified: datetime = Field(default_factory=utcnow)

    def is_empty(self) -> bool:
        return not self.stages and not self.order and not self.removed

    def touch(self) -> None:
        self.modified = utcnow()


# -- the transcript ------------------------------------------------------------------------------------


class PipelineRun(Base):
    """One pass over the flow, driven in the background because part of it waits on ComfyUI."""

    id: str = Field(default_factory=lambda: new_id("prun"))
    project_id: str = ""
    board_id: str = ""
    status: Literal["running", "success", "error", "cancelled"] = "running"
    #: The stages it intends to run, in order, so the panel can show what is still ahead.
    stage_ids: list[str] = Field(default_factory=list)
    frame_ids: list[str] = Field(default_factory=list)
    #: What it is doing right now.
    stage_id: str = ""
    done: list[str] = Field(default_factory=list)
    error: str | None = None
    started: datetime = Field(default_factory=utcnow)
    finished: datetime | None = None


class WriteRecord(Base):
    """One value, and what it displaced."""

    target: str
    frame_id: str | None = None
    before: str = ""
    after: str = ""
    #: False when the stage proposed rather than applied, or when a blank answer was declined.
    applied: bool = True
    reason: str = ""


class StageRun(Base):
    """What one execution of one stage actually sent, and actually got back.

    The point of the whole exercise. A prompt you cannot read is a prompt you cannot fix, and "the model
    ignored my instruction" and "my instruction never reached the model" look identical from the outside
    until someone writes down what was sent.
    """

    id: str = Field(default_factory=lambda: new_id("srun"))
    board_id: str = ""
    #: Set when this ran as part of a whole-pipeline run; None for a single stage.
    pipeline_run_id: str | None = None
    stage_id: str = ""
    stage_name: str = ""
    kind: StageKind = "llm"
    scope: StageScope = "board"
    frame_id: str | None = None
    status: Literal["running", "success", "error", "skipped"] = "running"
    #: True for the second, narrower ask made by :class:`StageRetry`.
    retry: bool = False

    # what was sent
    system: str = ""
    prompt: str = ""
    provider_id: str = ""
    model: str = ""
    temperature: float | None = None
    image_count: int = 0
    schema_sent: dict[str, Any] | None = None
    #: Tokens in the template that had no value. Left verbatim in the prompt, and named here.
    unknown_tokens: list[str] = Field(default_factory=list)

    # what came back
    reply: str = ""
    payload: dict[str, Any] | None = None
    writes: list[WriteRecord] = Field(default_factory=list)

    # comfy stages, which finish later
    run_id: str | None = None
    step_ids: dict[str, str] = Field(default_factory=dict)

    error: str | None = None
    #: Set when the text above was cut to fit the cap.
    truncated: bool = False
    started: datetime = Field(default_factory=utcnow)
    finished: datetime | None = None
    duration_ms: int = 0
