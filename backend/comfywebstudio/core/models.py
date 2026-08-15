"""The domain model.

A **project** holds workflows, shots and a timeline. A **shot** is a small DAG of **steps**; each step runs
one ComfyUI workflow. **Links** carry a named output port of one step into a named input port of another.

Everything here is plain pydantic and serialises straight to ``project.json`` — there is no database, which
is what makes export/import a file copy rather than a migration exercise.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, computed_field

from .base import Base, utcnow
from .ids import new_id
from .storyboard import Storyboard

#: Bumped when ``project.json`` changes shape; ``migrations.py`` maps older files forward.
PROJECT_SCHEMA_VERSION = 1

PortKind = Literal[
    "image", "mask", "video", "audio", "latent", "string", "int", "float", "boolean", "file"
]
PortDirection = Literal["in", "out"]
ParamKind = Literal["string", "int", "float", "boolean", "choice"]
#: What a value node on the shot canvas holds.
#:
#: The literals are self-explanatory. ``media`` points at an asset in the project's library, and ``shot``
#: at another shot's output — the last thing it produced, not a re-run of it. Both are *sources*: they
#: supply something a step consumes without themselves executing.
ValueNodeKind = Literal["string", "int", "float", "boolean", "media", "shot"]
SeedMode = Literal["fixed", "randomize", "increment"]
RunMode = Literal["step", "chain", "shot", "timeline"]
TrackKind = Literal["video", "audio", "text", "overlay"]

RunStatus = Literal["pending", "queued", "running", "success", "error", "cancelled", "skipped", "cached"]

TERMINAL_STATUSES: frozenset[str] = frozenset({"success", "error", "cancelled", "skipped", "cached"})
FAILED_STATUSES: frozenset[str] = frozenset({"error", "cancelled"})




# -- workflow description ------------------------------------------------------------------------------


class PortSpec(Base):
    """One named input or output port, discovered from a ``WS*Input``/``WS*Output`` node."""

    key: str
    direction: PortDirection
    kind: PortKind
    node_id: str
    label: str = ""
    group: str = ""
    order: int = 0
    optional: bool = False
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.label or self.key


class ParamTarget(Base):
    """One node input a parameter writes to."""

    node_id: str
    input_name: str


class ParamSpec(Base):
    """One editable parameter.

    ``source`` distinguishes the three discovery paths: ``ws_node`` for our own input nodes,
    ``raw_widget`` for an arbitrary widget on a stock node the user chose to expose, and ``subgraph`` for
    an input a subgraph promotes. All three render identically.
    """

    key: str
    kind: ParamKind
    label: str = ""
    default: Any = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: list[str] | None = None
    multiline: bool = False
    tooltip: str = ""
    group: str = ""
    order: int = 0
    node_id: str
    input_name: str
    source: Literal["ws_node", "raw_widget", "subgraph"] = "ws_node"
    #: Every input this parameter drives. A promoted subgraph input often feeds several at once — `width`
    #: typically sets both the latent size and the scheduler — so one value must reach all of them.
    #: Empty means the single ``(node_id, input_name)`` above.
    targets: list[ParamTarget] = Field(default_factory=list)
    #: Set for seed parameters so the runner knows it may randomise them.
    is_seed: bool = False

    @property
    def all_targets(self) -> list[ParamTarget]:
        return self.targets or [ParamTarget(node_id=self.node_id, input_name=self.input_name)]

    @property
    def display_name(self) -> str:
        return self.label or self.key


class WorkflowRef(Base):
    """A sub-workflow belonging to the project.

    Both formats are kept: the UI graph is what opens in ComfyUI, the API prompt is what executes. They are
    produced together by ComfyUI's own ``graphToPrompt()`` via the bridge extension.
    """

    id: str = Field(default_factory=lambda: new_id("wf"))
    name: str = "Workflow"
    #: Hash of the API prompt; changes here invalidate cached step results.
    hash: str = ""
    ports: list[PortSpec] = Field(default_factory=list)
    params: list[ParamSpec] = Field(default_factory=list)
    #: Path inside ComfyUI's user workflow directory, when we pushed a copy there for editing.
    comfy_userdata_path: str | None = None
    last_synced: datetime | None = None
    #: Node classes referenced by the graph that the target ComfyUI does not have installed.
    missing_nodes: list[str] = Field(default_factory=list)
    #: Non-fatal problems found during import or conversion, surfaced in the UI.
    warnings: list[str] = Field(default_factory=list)
    #: Which version of our UI-to-prompt converter produced the stored prompt, or ``EXACT`` when ComfyUI
    #: produced it itself. An older number means the graph is worth converting again: a fix to the
    #: converter otherwise never reaches a workflow that is already imported, because nothing re-reads a
    #: file that has not changed. Zero is "written before this was recorded".
    converted_by: int = 0
    created: datetime = Field(default_factory=utcnow)

    def port(self, key: str, direction: PortDirection | None = None) -> PortSpec | None:
        for p in self.ports:
            if p.key == key and (direction is None or p.direction == direction):
                return p
        return None

    def param(self, key: str) -> ParamSpec | None:
        return next((p for p in self.params if p.key == key), None)

    @property
    def inputs(self) -> list[PortSpec]:
        return [p for p in self.ports if p.direction == "in"]

    @property
    def outputs(self) -> list[PortSpec]:
        return [p for p in self.ports if p.direction == "out"]


# -- shots ---------------------------------------------------------------------------------------------


class Vec2(Base):
    x: float = 0.0
    y: float = 0.0


class Size(Base):
    """Canvas size of a node. Zero means "size to content"."""

    w: float = 0.0
    h: float = 0.0


class Step(Base):
    """One workflow execution inside a shot."""

    id: str = Field(default_factory=lambda: new_id("step"))
    name: str = "Step"
    workflow_id: str
    enabled: bool = True
    #: ``param key -> value``. Absent keys fall back to the workflow's declared default.
    param_overrides: dict[str, Any] = Field(default_factory=dict)
    #: Parameter keys the user pinned to the node itself, in the order they should appear there. A
    #: workflow can expose dozens of knobs; these are the two or three worth seeing without selecting the
    #: step. Keys that no longer exist are ignored rather than pruned, so re-adding a parameter in ComfyUI
    #: brings back the choice the user already made.
    exposed_params: list[str] = Field(default_factory=list)
    seed_mode: SeedMode | None = None
    #: Overrides the project's backend, so one step can run on a remote GPU and the rest locally.
    backend_id: str | None = None
    notes: str = ""
    ui_pos: Vec2 = Field(default_factory=Vec2)
    #: Left at 0×0 until the user resizes the node, so a default node still sizes to its own content.
    ui_size: Size = Field(default_factory=Size)


#: The single output every value node has. Fixed, because a value node carries exactly one value.
VALUE_PORT = "value"


class ValueNode(Base):
    """A constant on the shot canvas, feeding one or more step inputs.

    Not a step: nothing executes and nothing is cached against it. It exists so a value several steps share
    — a prompt, a frame count, a reference image — lives in one visible place on the canvas instead of
    being retyped into each step's parameters, and so changing it changes every step at once.

    ``media`` carries an imported :class:`Asset` rather than a literal, and takes that asset's kind as its
    output kind, so an imported clip offers a ``video`` output and a still offers an ``image`` one.
    """

    id: str = Field(default_factory=lambda: new_id("val"))
    name: str = ""
    kind: ValueNodeKind = "string"
    #: The literal, for every kind but ``media``.
    value: Any = None
    #: The asset, for ``media``.
    asset_id: str | None = None
    #: Which shot and output port to take the last result from, for ``shot``.
    source_shot_id: str | None = None
    source_port: str | None = None
    #: What an empty ``media`` or ``shot`` node offers, so it can be wired up before the source is
    #: chosen. Kept in step with the real thing once there is one — that is the truth, this the
    #: placeholder.
    media_kind: PortKind = "image"
    ui_pos: Vec2 = Field(default_factory=Vec2)
    ui_size: Size = Field(default_factory=Size)

    @property
    def display_name(self) -> str:
        return self.name or DEFAULT_VALUE_NODE_NAMES.get(self.kind, "Value")

    @property
    def is_source(self) -> bool:
        """True when this node supplies media from elsewhere rather than a value typed into it."""
        return self.kind in {"media", "shot"}

    def output_kind(self, project: Project | None = None) -> PortKind:
        """What this node's output port carries."""
        if self.kind == "media":
            asset = project.assets.get(self.asset_id or "") if project else None
            return asset.kind if asset else self.media_kind
        if self.kind == "shot":
            return self.media_kind
        return self.kind  # type: ignore[return-value]


#: Shown on the canvas until the user names a node.
DEFAULT_VALUE_NODE_NAMES: dict[str, str] = {
    "string": "Text",
    "int": "Integer",
    "float": "Number",
    "boolean": "Boolean",
    "media": "Media",
    "shot": "Shot output",
}


class Link(Base):
    """An output port feeding an input port.

    ``from_step`` is a step id or a value node id — a value node is a source like any other as far as the
    canvas and the executor are concerned, it simply never runs.
    """

    id: str = Field(default_factory=lambda: new_id("link"))
    from_step: str
    from_port: str
    to_step: str
    to_port: str


class TemplateInstance(Base):
    """A shot template placed on a canvas as one contained node.

    The instance holds no structure of its own: it names a template in the shared library and the values
    it overrides. What it *runs* is read from the template every time, so improving a template improves
    every shot that placed it — which is the point of having templates at all.

    ``workflow_map`` is the exception, and it has to be stored. Placing an instance copies the template's
    workflows into the project so the project stays self-contained and exportable; this records which
    project workflow each of the template's own workflow keys became. A template that has since grown a
    new step has a key with no mapping, which is exactly the signal that this instance needs re-syncing.
    """

    id: str = Field(default_factory=lambda: new_id("inst"))
    template_id: str
    #: Overrides the template's own name on this canvas.
    name: str = ""
    enabled: bool = True
    #: Values for the template's promoted controls, by promoted key.
    param_overrides: dict[str, Any] = Field(default_factory=dict)
    #: Template workflow key -> the project workflow id it was imported as.
    workflow_map: dict[str, str] = Field(default_factory=dict)
    #: The template revision this instance was last reconciled against.
    template_revision: int = 0
    ui_pos: Vec2 = Field(default_factory=Vec2)
    ui_size: Size = Field(default_factory=Size)


class Shot(Base):
    id: str = Field(default_factory=lambda: new_id("shot"))
    name: str = "Shot"
    notes: str = ""
    color: str | None = None
    #: Set when this shot is not a shot at all but an open editing session for a template.
    #:
    #: A template is a captured shot, so the honest way to let one be edited is to materialise it back
    #: into a real shot and hand it to the editor that already exists — every step, link and parameter
    #: control works unchanged. The marker keeps these out of the shot list and out of the timeline, and
    #: lets a second "edit" on the same template reuse the session rather than forking it.
    template_edit_id: str | None = None
    steps: list[Step] = Field(default_factory=list)
    #: Value nodes on the same canvas as the steps, kept separate because they never execute.
    nodes: list[ValueNode] = Field(default_factory=list)
    #: Placed shot templates, each standing in for the steps it expands to at run time.
    instances: list[TemplateInstance] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    created: datetime = Field(default_factory=utcnow)

    def step(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def node(self, node_id: str) -> ValueNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def instance(self, instance_id: str) -> TemplateInstance | None:
        return next((i for i in self.instances if i.id == instance_id), None)

    def links_into(self, step_id: str) -> list[Link]:
        return [link for link in self.links if link.to_step == step_id]

    def links_out_of(self, step_id: str) -> list[Link]:
        return [link for link in self.links if link.from_step == step_id]


# -- run results ---------------------------------------------------------------------------------------


class ComfyRef(Base):
    """A file as ComfyUI names it, kept so we can re-fetch or re-preview without guessing."""

    filename: str
    subfolder: str = ""
    type: str = "output"


class Artifact(Base):
    """One concrete output of a step run."""

    id: str = Field(default_factory=lambda: new_id("art"))
    kind: PortKind
    port_key: str
    #: Project-relative path when ingested into the asset store; absolute when referenced in place.
    path: str
    comfy_ref: ComfyRef | None = None
    thumb: str | None = None
    #: Content hash, used as the cache key for downstream steps.
    sha256: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    created: datetime = Field(default_factory=utcnow)


class StepRun(Base):
    step_id: str
    status: RunStatus = "pending"
    prompt_id: str | None = None
    started: datetime | None = None
    finished: datetime | None = None
    progress: float = 0.0
    current_node: str | None = None
    outputs: list[Artifact] = Field(default_factory=list)
    error: str | None = None
    error_node: str | None = None
    #: True when we reused a previous run's outputs instead of executing.
    cached: bool = False
    #: The resolved parameter values actually submitted, so a result is reproducible.
    resolved_params: dict[str, Any] = Field(default_factory=dict)
    cache_key: str = ""
    logs: list[str] = Field(default_factory=list)

    def output(self, port_key: str) -> Artifact | None:
        return next((a for a in self.outputs if a.port_key == port_key), None)

    @property
    def duration_s(self) -> float | None:
        if self.started and self.finished:
            return (self.finished - self.started).total_seconds()
        return None


class Run(Base):
    id: str = Field(default_factory=lambda: new_id("run"))
    shot_id: str | None = None
    mode: RunMode = "shot"
    status: RunStatus = "pending"
    started: datetime = Field(default_factory=utcnow)
    finished: datetime | None = None
    step_runs: list[StepRun] = Field(default_factory=list)
    error: str | None = None

    def step_run(self, step_id: str) -> StepRun | None:
        return next((sr for sr in self.step_runs if sr.step_id == step_id), None)

    @property
    def progress(self) -> float:
        if not self.step_runs:
            return 0.0
        done = sum(1.0 if sr.status in TERMINAL_STATUSES else sr.progress for sr in self.step_runs)
        return min(1.0, done / len(self.step_runs))


# -- timeline ------------------------------------------------------------------------------------------


class ClipSource(Base):
    """Where a clip's media comes from.

    Referencing ``(shot, step, port)`` rather than a file means re-running a shot updates the timeline
    automatically; a fixed ``asset_id`` is for imported media that no step produces.
    """

    kind: Literal["step_output", "asset"] = "step_output"
    shot_id: str | None = None
    step_id: str | None = None
    port_key: str | None = None
    asset_id: str | None = None


class Transform(Base):
    scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    rotation: float = 0.0
    fit: Literal["contain", "cover", "stretch", "none"] = "contain"


class Transition(Base):
    kind: Literal["none", "fade", "dissolve"] = "none"
    duration: float = 0.0


class Clip(Base):
    id: str = Field(default_factory=lambda: new_id("clip"))
    name: str = ""
    source: ClipSource = Field(default_factory=ClipSource)
    #: Seconds on the timeline.
    start: float = 0.0
    duration: float = 1.0
    #: Seconds into the source media.
    in_point: float = 0.0
    out_point: float | None = None
    transform: Transform = Field(default_factory=Transform)
    transition_in: Transition = Field(default_factory=Transition)
    transition_out: Transition = Field(default_factory=Transition)
    opacity: float = 1.0
    #: Audio gain, 1.0 being the clip's own level. Multiplied by its track's.
    volume: float = 1.0
    #: Stereo placement, -1 hard left to +1 hard right. Applied with an equal-power law, so panning
    #: a clip does not make it quieter the way a naive linear pan does.
    pan: float = 0.0
    #: Clips that move and trim together share this.
    #:
    #: A video and the sound that came with it are one thing to the person cutting, so they behave as
    #: one until deliberately untied — which is what makes it safe to place them automatically. Stored
    #: as a group rather than a pointer to a partner, so untying is simply clearing it.
    link_id: str | None = None
    #: Text tracks render this instead of media.
    text: str = ""
    text_style: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @property
    def end(self) -> float:
        return self.start + self.duration


class Track(Base):
    id: str = Field(default_factory=lambda: new_id("track"))
    kind: TrackKind = "video"
    name: str = "Track"
    muted: bool = False
    #: Soloing any track silences every track that is not soloed — the standard mixer behaviour, and the
    #: reason solo has to be checked before mute rather than alongside it.
    solo: bool = False
    locked: bool = False
    #: Applied on top of each clip's own gain and pan.
    volume: float = 1.0
    pan: float = 0.0
    clips: list[Clip] = Field(default_factory=list)

    def clip(self, clip_id: str) -> Clip | None:
        return next((c for c in self.clips if c.id == clip_id), None)


def default_tracks() -> list[Track]:
    """What a new timeline starts with: somewhere to put pictures, and somewhere to put sound.

    The audio track is there from the beginning rather than conjured the first time something needs it,
    because a shot that came with sound should land complete on a timeline the user has already seen —
    not make a new lane appear underneath them at the moment of the drop.
    """
    return [Track(kind="video", name="Video"), Track(kind="audio", name="Audio")]


class Timeline(Base):
    fps: float = 24.0
    width: int = 1024
    height: int = 1024
    background: str = "#000000"
    tracks: list[Track] = Field(default_factory=default_tracks)

    # Serialised so the UI does not have to re-derive it from every clip on every render. It is ignored
    # on input (``extra="ignore"``), so a round trip cannot corrupt it.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration(self) -> float:
        return max(
            (clip.end for track in self.tracks for clip in track.clips if clip.enabled),
            default=0.0,
        )

    def track(self, track_id: str) -> Track | None:
        return next((t for t in self.tracks if t.id == track_id), None)


# -- project -------------------------------------------------------------------------------------------


class AssetSource(Base):
    """What produces a generated asset: one output port of one step in one shot."""

    shot_id: str
    step_id: str
    port_key: str


class Asset(Base):
    """A named piece of media the project owns.

    Two kinds, deliberately one model. An **imported** asset is footage, a still or music the user brought
    in, and has no source. A **generated** one was produced by a step and remembers which one, so it can
    be refreshed from a later run rather than becoming a dead copy of an old result.

    Both look identical everywhere they are used — a media value node, a timeline clip, a dropped source
    node — which is the point: whether a piece of media was imported or made should not change how it is
    wired up.
    """

    id: str = Field(default_factory=lambda: new_id("asset"))
    name: str = ""
    kind: PortKind = "image"
    path: str
    thumb: str | None = None
    sha256: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    #: Absent for imported media; set for anything a step produced.
    source: AssetSource | None = None
    #: When the source last refreshed it, so the UI can say how current a generated asset is.
    generated: datetime | None = None
    created: datetime = Field(default_factory=utcnow)

    @property
    def is_generated(self) -> bool:
        return self.source is not None


class RenderChoice(Base):
    """What this project last rendered with, so the Render dialog opens where it was left.

    Kept on the project rather than in the app settings because format is a property of the piece: a
    portrait short and a 4K landscape piece live side by side, and having one silently adopt the other's
    settings because it was rendered second is exactly the surprise this avoids.

    Separate from `Timeline.width/height`, which is the *canvas* — what the clips are composited onto.
    Rendering a 1024×1024 cut out at 1080p is a legitimate thing to do and must not resize the project.
    """

    width: int | None = None
    height: int | None = None
    fps: float | None = None
    container: str | None = None
    video_codec: str | None = None
    crf: int | None = None
    #: The preset these came from, so the dialog can show it as still selected. Cleared as soon as any
    #: field is changed by hand — claiming a preset for settings that no longer match it would be a lie.
    preset_id: str | None = None


class ProjectSettings(Base):
    fps: float = 24.0
    width: int = 1024
    height: int = 1024
    #: Default ComfyUI backend for this project's steps; individual steps may override it.
    backend_id: str | None = None
    #: Remembered from the last render. Absent until the project has been rendered once.
    render: RenderChoice = Field(default_factory=RenderChoice)


class Project(Base):
    schema_version: int = PROJECT_SCHEMA_VERSION
    id: str = Field(default_factory=lambda: new_id("proj"))
    name: str = "Untitled Project"
    description: str = ""
    created: datetime = Field(default_factory=utcnow)
    modified: datetime = Field(default_factory=utcnow)
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    workflows: dict[str, WorkflowRef] = Field(default_factory=dict)
    shots: list[Shot] = Field(default_factory=list)
    timeline: Timeline = Field(default_factory=Timeline)
    assets: dict[str, Asset] = Field(default_factory=dict)
    #: Storyboards belong to the project so they travel with it — export, import and history all come
    #: free, and the shots a board produces sit beside the board that produced them.
    storyboards: list[Storyboard] = Field(default_factory=list)

    def shot(self, shot_id: str) -> Shot | None:
        return next((s for s in self.shots if s.id == shot_id), None)

    def workflow(self, workflow_id: str) -> WorkflowRef | None:
        return self.workflows.get(workflow_id)

    def find_step(self, step_id: str) -> tuple[Shot, Step] | None:
        for shot in self.shots:
            step = shot.step(step_id)
            if step is not None:
                return shot, step
        return None

    def touch(self) -> None:
        self.modified = utcnow()


# -- port compatibility --------------------------------------------------------------------------------

#: Kinds a port of the key kind will accept, beyond an exact match. Deliberately conservative: an implicit
#: conversion the user did not ask for is worse than a link they have to think about.
_IMPLICIT_CONVERSIONS: dict[str, set[str]] = {
    "image": {"mask"},          # a mask is a single-channel image
    "mask": {"image"},          # take luminance
    "float": {"int"},
    "string": {"int", "float", "boolean"},
    "file": {"image", "video", "audio", "latent", "mask"},  # anything on disk can be passed as a path
}


def can_connect(from_kind: str, to_kind: str) -> bool:
    """Whether an output of ``from_kind`` may feed an input of ``to_kind``."""
    return from_kind == to_kind or from_kind in _IMPLICIT_CONVERSIONS.get(to_kind, set())


def conversion_note(from_kind: str, to_kind: str) -> str | None:
    """Human-readable warning when a link is legal but lossy, so the UI can say so up front."""
    if from_kind == to_kind:
        return None
    if not can_connect(from_kind, to_kind):
        return None
    notes = {
        ("image", "mask"): "Image will be converted to a mask using its luminance.",
        ("mask", "image"): "Mask will be expanded to a greyscale image.",
        ("int", "float"): "Integer will be widened to a float.",
        ("int", "string"): "Number will be formatted as text.",
        ("float", "string"): "Number will be formatted as text.",
        ("boolean", "string"): "Boolean will be formatted as text.",
    }
    return notes.get((from_kind, to_kind), f"{from_kind} will be adapted to {to_kind}.")
