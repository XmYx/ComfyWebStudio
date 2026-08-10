"""Shot templates: a reusable piece of graph, placeable as one contained node.

A template is a shot's structure lifted out of the project that built it — its steps, its value nodes, the
links between them, and the workflows those steps need — stored in a library shared by every project. It
is placed as a :class:`~.models.TemplateInstance`, a single node that stands in for the whole thing and
expands back into real steps when the shot runs.

Three ideas make that work:

* **Keys, not ids.** Inside a template everything is referred to by a stable key rather than a project id,
  so the same template can be placed twice in one shot without the two colliding.
* **Promotion.** A template exposes a chosen subset of its innards: input ports nothing inside drives,
  output ports nothing inside consumes, and whichever parameters are worth reaching from outside. That is
  the node's surface, and it is stored explicitly so renaming a promoted port does not silently re-derive.
* **Revision.** Every save bumps it. A placed instance records the revision it was reconciled against, so
  "this template has moved on" is a fact the UI can state rather than a guess.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from .ids import new_id
from .models import (
    Base,
    ParamSpec,
    PortDirection,
    PortKind,
    PortSpec,
    SeedMode,
    Size,
    ValueNodeKind,
    Vec2,
    utcnow,
)

#: Joins an instance id to a key inside it, giving a step in an expanded shot a unique, readable id.
#: Deliberately the same convention ComfyUI uses for subgraph execution ids.
KEY_SEPARATOR = ":"

#: Marks a placed node that stands for another *shot* in the same project rather than a library template.
#:
#: It lives in the instance's ``template_id`` on purpose. Everything that draws, validates, expands or runs
#: a placed node already resolves it through one id, so giving a nested shot a reference in the same shape
#: means all of that works on it unchanged — which is precisely what "behave the same as a template" asks
#: for. The only code that has to know the difference is the resolver that turns the id into a template.
SHOT_SOURCE_PREFIX = "shot:"


def shot_reference(shot_id: str) -> str:
    """The reference a placed node uses to point at another shot."""
    return f"{SHOT_SOURCE_PREFIX}{shot_id}"


def referenced_shot(reference: str) -> str | None:
    """The shot id behind a reference, or None when it names a library template."""
    if reference.startswith(SHOT_SOURCE_PREFIX):
        return reference[len(SHOT_SOURCE_PREFIX):] or None
    return None


class TemplateWorkflow(Base):
    """A workflow a template's steps need, carried with it so the template travels between projects."""

    key: str
    name: str
    hash: str = ""
    ports: list[PortSpec] = Field(default_factory=list)
    params: list[ParamSpec] = Field(default_factory=list)
    #: True when a LiteGraph document was bundled alongside the API prompt.
    has_ui_graph: bool = False


class TemplateStep(Base):
    key: str
    name: str
    workflow_key: str
    param_overrides: dict[str, Any] = Field(default_factory=dict)
    exposed_params: list[str] = Field(default_factory=list)
    seed_mode: SeedMode | None = None
    enabled: bool = True
    notes: str = ""
    ui_pos: Vec2 = Field(default_factory=Vec2)
    ui_size: Size = Field(default_factory=Size)


class TemplateValueNode(Base):
    key: str
    name: str = ""
    kind: ValueNodeKind = "string"
    value: Any = None
    #: Media nodes reference a project asset, which a template cannot carry. The kind survives so the
    #: node keeps its shape and its links; the user picks the media again in the project they place it in.
    media_kind: PortKind = "image"
    ui_pos: Vec2 = Field(default_factory=Vec2)
    ui_size: Size = Field(default_factory=Size)


class TemplateLink(Base):
    """A link between two things inside the template, by key."""

    from_key: str
    from_port: str
    to_key: str
    to_port: str


class TemplatePort(Base):
    """One port the container node exposes, and the inner port it stands for."""

    key: str
    direction: PortDirection
    kind: PortKind
    inner_key: str
    inner_port: str
    label: str = ""
    optional: bool = False
    #: Unshown ports stay in the template — so the choice survives a re-save — but do not appear on the
    #: node and cannot be linked.
    shown: bool = True

    @property
    def display_name(self) -> str:
        return self.label or self.key


class TemplateControl(Base):
    """One parameter the container node exposes, and the inner parameter it writes to."""

    key: str
    inner_key: str
    inner_param: str
    label: str = ""
    #: Copied from the inner workflow's own ParamSpec so the node can render the right widget without
    #: reaching back into the workflow on every draw.
    spec: ParamSpec | None = None
    shown: bool = True

    @property
    def display_name(self) -> str:
        return self.label or self.key


class ShotTemplate(Base):
    """A reusable shot, stored in the shared library."""

    id: str = Field(default_factory=lambda: new_id("tpl"))
    name: str = "Template"
    description: str = ""
    #: Bumped on every save. A placed instance compares its own copy against this.
    revision: int = 1
    created: datetime = Field(default_factory=utcnow)
    modified: datetime = Field(default_factory=utcnow)
    #: Where it came from, purely so the user can find the original again.
    source_project: str = ""

    workflows: list[TemplateWorkflow] = Field(default_factory=list)
    steps: list[TemplateStep] = Field(default_factory=list)
    nodes: list[TemplateValueNode] = Field(default_factory=list)
    links: list[TemplateLink] = Field(default_factory=list)
    ports: list[TemplatePort] = Field(default_factory=list)
    controls: list[TemplateControl] = Field(default_factory=list)

    def workflow(self, key: str) -> TemplateWorkflow | None:
        return next((w for w in self.workflows if w.key == key), None)

    def step(self, key: str) -> TemplateStep | None:
        return next((s for s in self.steps if s.key == key), None)

    def node(self, key: str) -> TemplateValueNode | None:
        return next((n for n in self.nodes if n.key == key), None)

    def port(self, key: str, direction: PortDirection | None = None) -> TemplatePort | None:
        return next(
            (p for p in self.ports if p.key == key and (direction is None or p.direction == direction)),
            None,
        )

    def control(self, key: str) -> TemplateControl | None:
        return next((c for c in self.controls if c.key == key), None)

    @property
    def shown_ports(self) -> list[TemplatePort]:
        return [p for p in self.ports if p.shown]

    @property
    def shown_controls(self) -> list[TemplateControl]:
        return [c for c in self.controls if c.shown]

    def touch(self) -> None:
        self.revision += 1
        self.modified = utcnow()


class TemplateSummary(Base):
    """What the library list shows, without loading every template's whole structure."""

    id: str
    name: str
    description: str
    revision: int
    modified: datetime
    source_project: str = ""
    step_count: int = 0
    input_count: int = 0
    output_count: int = 0
    control_count: int = 0
