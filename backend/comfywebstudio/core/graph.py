"""Step-graph validation and ordering.

A shot is a DAG of steps joined by port links. This module answers the two questions the executor and the
editor both need: *is this graph legal* and *in what order do the steps run*.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from .errors import GraphError
from .models import (
    VALUE_PORT,
    Link,
    PortKind,
    Project,
    Shot,
    Step,
    ValueNode,
    can_connect,
    conversion_note,
)


@dataclass(slots=True)
class LinkSource:
    """The producing end of a link, whichever kind of node it is.

    Steps and value nodes are both sources on the canvas, and every consumer here cares about the same
    three things — what to call it, what kind it carries, and whether it has to run first.
    """

    name: str
    kind: PortKind
    #: None for a value node: it produces its value without executing.
    step: Step | None = None

    @property
    def is_step(self) -> bool:
        return self.step is not None


def resolve_link_source(project: Project, shot: Shot, link: Link) -> LinkSource | None:
    """What feeds this link, or None when the source or its port has gone."""
    step = shot.step(link.from_step)
    if step is not None:
        workflow = project.workflow(step.workflow_id)
        port = workflow.port(link.from_port, "out") if workflow else None
        return LinkSource(name=step.name, kind=port.kind, step=step) if port else None

    node = shot.node(link.from_step)
    if node is not None and link.from_port == VALUE_PORT:
        return LinkSource(name=node.display_name, kind=node.output_kind(project))
    return None


def source_label(shot: Shot, node_id: str) -> str | None:
    """What to call the producing end of a link in a message, whichever kind of node it is."""
    step = shot.step(node_id)
    if step is not None:
        return step.name
    node = shot.node(node_id)
    return node.display_name if node is not None else None


def value_nodes_into(shot: Shot, step_id: str) -> dict[str, ValueNode]:
    """Value nodes feeding this step, keyed by the input port each one drives."""
    by_id = {node.id: node for node in shot.nodes}
    return {
        link.to_port: by_id[link.from_step]
        for link in shot.links_into(step_id)
        if link.from_step in by_id and link.from_port == VALUE_PORT
    }


@dataclass(slots=True)
class GraphIssue:
    level: str  # "error" | "warning"
    message: str
    step_id: str | None = None
    link_id: str | None = None
    port_key: str | None = None


@dataclass(slots=True)
class GraphReport:
    order: list[str] = field(default_factory=list)
    issues: list[GraphIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[GraphIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[GraphIssue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


def topological_order(shot: Shot, *, only: set[str] | None = None) -> list[str]:
    """Step ids in dependency order.

    Raises :class:`GraphError` on a cycle, naming the steps involved — "there is a cycle" is not actionable,
    "step A -> step B -> step A" is.
    """
    steps = [s for s in shot.steps if only is None or s.id in only]
    ids = {s.id for s in steps}

    incoming: dict[str, int] = dict.fromkeys(ids, 0)
    outgoing: dict[str, list[str]] = defaultdict(list)
    for link in shot.links:
        if link.from_step in ids and link.to_step in ids and link.from_step != link.to_step:
            outgoing[link.from_step].append(link.to_step)
            incoming[link.to_step] += 1

    # Seed in declaration order so an unlinked graph runs top-to-bottom as the user arranged it.
    ready = deque(s.id for s in steps if incoming[s.id] == 0)
    order: list[str] = []
    while ready:
        current = ready.popleft()
        order.append(current)
        for nxt in outgoing[current]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)

    if len(order) != len(ids):
        stuck = sorted(ids - set(order))
        names = {s.id: s.name for s in steps}
        raise GraphError(
            "These steps form a cycle and cannot be ordered: "
            + ", ".join(f"{names.get(sid, sid)}" for sid in stuck),
            details={"steps": stuck},
        )
    return order


def upstream_closure(shot: Shot, step_ids: set[str]) -> set[str]:
    """``step_ids`` plus every *step* they transitively depend on.

    This is what "run this step" actually means: a step whose inputs were never produced cannot run alone.
    A value node upstream is skipped — it supplies its value without running, so it is never something the
    closure has to schedule.
    """
    by_target: dict[str, list[Link]] = defaultdict(list)
    for link in shot.links:
        by_target[link.to_step].append(link)

    steps = {step.id for step in shot.steps}
    seen: set[str] = set()
    pending = deque(sid for sid in step_ids if sid in steps)
    while pending:
        current = pending.popleft()
        if current in seen:
            continue
        seen.add(current)
        for link in by_target.get(current, []):
            if link.from_step in steps and link.from_step not in seen:
                pending.append(link.from_step)
    return seen


def validate_shot(project: Project, shot: Shot) -> GraphReport:
    """Full check of one shot: workflows exist, ports exist, kinds match, no cycles, no double-driven inputs."""
    report = GraphReport()
    step_by_id = {s.id: s for s in shot.steps}

    for step in shot.steps:
        workflow = project.workflow(step.workflow_id)
        if workflow is None:
            report.issues.append(
                GraphIssue("error", f"Step {step.name!r} references a workflow that is not in the project.",
                           step_id=step.id)
            )
            continue
        if workflow.missing_nodes:
            report.issues.append(
                GraphIssue(
                    "warning",
                    f"Step {step.name!r} uses nodes not installed on the target ComfyUI: "
                    + ", ".join(sorted(workflow.missing_nodes)[:5]),
                    step_id=step.id,
                )
            )

    # A media node with nothing chosen has nothing to hand downstream, and the step it feeds will fail at
    # run time rather than when the user wired it up.
    wired = {link.from_step for link in shot.links}
    for node in shot.nodes:
        if node.kind == "media" and node.id in wired and not project.assets.get(node.asset_id or ""):
            report.issues.append(
                GraphIssue(
                    "error",
                    f"{node.display_name!r} has no media selected, so the step it feeds cannot run.",
                )
            )

    # An input port driven by two links would silently take whichever ran last.
    driven: dict[tuple[str, str], str] = {}

    for link in shot.links:
        source = resolve_link_source(project, shot, link)
        target = step_by_id.get(link.to_step)
        if source is None:
            origin = source_label(shot, link.from_step)
            report.issues.append(
                GraphIssue(
                    "error",
                    f"{origin!r} no longer has an output port {link.from_port!r}."
                    if origin is not None
                    else "A link starts at a node that is no longer in this shot.",
                    link_id=link.id,
                    port_key=link.from_port,
                )
            )
            continue
        if target is None:
            report.issues.append(
                GraphIssue("error", "A link points at a step that no longer exists.", link_id=link.id)
            )
            continue

        target_wf = project.workflow(target.workflow_id)
        if target_wf is None:
            continue

        in_port = target_wf.port(link.to_port, "in")
        if in_port is None:
            report.issues.append(
                GraphIssue("error", f"{target.name!r} has no input port {link.to_port!r}.",
                           link_id=link.id, step_id=target.id, port_key=link.to_port)
            )
            continue

        if not can_connect(source.kind, in_port.kind):
            report.issues.append(
                GraphIssue(
                    "error",
                    f"Cannot connect {source.kind} output of {source.name!r} to "
                    f"{in_port.kind} input {in_port.display_name!r}.",
                    link_id=link.id,
                )
            )
        else:
            note = conversion_note(source.kind, in_port.kind)
            if note:
                report.issues.append(GraphIssue("warning", note, link_id=link.id))

        key = (link.to_step, link.to_port)
        if key in driven:
            report.issues.append(
                GraphIssue(
                    "error",
                    f"Input {in_port.display_name!r} on {target.name!r} is driven by more than one link.",
                    link_id=link.id,
                    step_id=target.id,
                    port_key=link.to_port,
                )
            )
        driven[key] = link.id

    # Required inputs with neither a link nor a usable default cannot run.
    for step in shot.steps:
        workflow = project.workflow(step.workflow_id)
        if workflow is None or not step.enabled:
            continue
        linked = {link.to_port for link in shot.links_into(step.id)}
        for port in workflow.inputs:
            if port.optional or port.key in linked:
                continue
            report.issues.append(
                GraphIssue(
                    "warning",
                    f"Input {port.display_name!r} on {step.name!r} is not connected; "
                    "its workflow value will be used.",
                    step_id=step.id,
                    port_key=port.key,
                )
            )

    try:
        report.order = topological_order(shot)
    except GraphError as exc:
        report.issues.append(GraphIssue("error", exc.message))

    return report


def validate_new_link(project: Project, shot: Shot, link: Link) -> None:
    """Check a link the user is about to create, raising :class:`GraphError` with a specific reason.

    Called before the link is stored so the editor can refuse the connection rather than accept it and then
    fail at run time.
    """
    if link.from_step == link.to_step:
        raise GraphError("A step cannot feed itself.")

    target = shot.step(link.to_step)
    if target is None:
        raise GraphError("A link must end at a step in this shot.")

    source = resolve_link_source(project, shot, link)
    if source is None:
        label = source_label(shot, link.from_step)
        raise GraphError(
            f"{label!r} has no output port {link.from_port!r}."
            if label is not None
            else "A link must start at a step or value node in this shot."
        )

    target_wf = project.workflow(target.workflow_id)
    if target_wf is None:
        raise GraphError("A step references a workflow that is not in the project.")

    in_port = target_wf.port(link.to_port, "in")
    if in_port is None:
        raise GraphError(f"{target.name!r} has no input port {link.to_port!r}.")

    if not can_connect(source.kind, in_port.kind):
        raise GraphError(
            f"Cannot connect a {source.kind} output to a {in_port.kind} input.",
            details={"from_kind": source.kind, "to_kind": in_port.kind},
        )

    existing = next(
        (link_ for link_ in shot.links if link_.to_step == link.to_step and link_.to_port == link.to_port),
        None,
    )
    if existing is not None and existing.id != link.id:
        raise GraphError(
            f"Input {in_port.display_name!r} is already connected. Disconnect it first.",
            details={"existing_link": existing.id},
        )

    # Reject the cycle before storing, using a probe graph rather than mutating the real shot.
    probe = shot.model_copy(deep=True)
    probe.links = [link_ for link_ in probe.links if link_.id != link.id] + [link]
    topological_order(probe)


def runnable_steps(shot: Shot, order: list[str]) -> list[Step]:
    """Steps in execution order, skipping disabled ones."""
    by_id = {s.id: s for s in shot.steps}
    return [by_id[sid] for sid in order if sid in by_id and by_id[sid].enabled]


def drop_links_for_removed_ports(
    project: Project, workflow_id: str, removed_ports: set[str]
) -> list[dict[str, str]]:
    """Remove links that referenced ports a re-sync no longer finds.

    A link to a port that no longer exists is an error at run time and an error in :func:`validate_shot`, so
    it is dropped here and reported instead — the caller tells the user which shots changed rather than
    leaving them to discover it when a run fails.
    """
    if not removed_ports:
        return []

    affected: list[dict[str, str]] = []
    step_ids = {
        step.id for shot in project.shots for step in shot.steps if step.workflow_id == workflow_id
    }
    for shot in project.shots:
        keep: list[Link] = []
        for link in shot.links:
            broken = (link.from_step in step_ids and link.from_port in removed_ports) or (
                link.to_step in step_ids and link.to_port in removed_ports
            )
            if broken:
                affected.append(
                    {"shot_id": shot.id, "link_id": link.id, "port": link.from_port or link.to_port}
                )
            else:
                keep.append(link)
        shot.links = keep
    return affected
