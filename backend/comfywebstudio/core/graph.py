"""Step-graph validation and ordering.

A shot is a DAG of steps joined by port links. This module answers the two questions the executor and the
editor both need: *is this graph legal* and *in what order do the steps run*.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from .errors import GraphError
from .models import Link, Project, Shot, Step, can_connect, conversion_note


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
    """``step_ids`` plus everything they transitively depend on.

    This is what "run this step" actually means: a step whose inputs were never produced cannot run alone.
    """
    by_target: dict[str, list[Link]] = defaultdict(list)
    for link in shot.links:
        by_target[link.to_step].append(link)

    seen: set[str] = set()
    pending = deque(step_ids)
    while pending:
        current = pending.popleft()
        if current in seen:
            continue
        seen.add(current)
        for link in by_target.get(current, []):
            if link.from_step not in seen:
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

    # An input port driven by two links would silently take whichever ran last.
    driven: dict[tuple[str, str], str] = {}

    for link in shot.links:
        source = step_by_id.get(link.from_step)
        target = step_by_id.get(link.to_step)
        if source is None or target is None:
            report.issues.append(
                GraphIssue("error", "A link points at a step that no longer exists.", link_id=link.id)
            )
            continue

        source_wf = project.workflow(source.workflow_id)
        target_wf = project.workflow(target.workflow_id)
        if source_wf is None or target_wf is None:
            continue

        out_port = source_wf.port(link.from_port, "out")
        in_port = target_wf.port(link.to_port, "in")

        if out_port is None:
            report.issues.append(
                GraphIssue("error", f"{source.name!r} has no output port {link.from_port!r}.",
                           link_id=link.id, step_id=source.id, port_key=link.from_port)
            )
        if in_port is None:
            report.issues.append(
                GraphIssue("error", f"{target.name!r} has no input port {link.to_port!r}.",
                           link_id=link.id, step_id=target.id, port_key=link.to_port)
            )
        if out_port is None or in_port is None:
            continue

        if not can_connect(out_port.kind, in_port.kind):
            report.issues.append(
                GraphIssue(
                    "error",
                    f"Cannot connect {out_port.kind} output {out_port.display_name!r} to "
                    f"{in_port.kind} input {in_port.display_name!r}.",
                    link_id=link.id,
                )
            )
        else:
            note = conversion_note(out_port.kind, in_port.kind)
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

    source = shot.step(link.from_step)
    target = shot.step(link.to_step)
    if source is None or target is None:
        raise GraphError("Both ends of a link must be steps in this shot.")

    source_wf = project.workflow(source.workflow_id)
    target_wf = project.workflow(target.workflow_id)
    if source_wf is None or target_wf is None:
        raise GraphError("A step references a workflow that is not in the project.")

    out_port = source_wf.port(link.from_port, "out")
    if out_port is None:
        raise GraphError(f"{source.name!r} has no output port {link.from_port!r}.")
    in_port = target_wf.port(link.to_port, "in")
    if in_port is None:
        raise GraphError(f"{target.name!r} has no input port {link.to_port!r}.")

    if not can_connect(out_port.kind, in_port.kind):
        raise GraphError(
            f"Cannot connect a {out_port.kind} output to a {in_port.kind} input.",
            details={"from_kind": out_port.kind, "to_kind": in_port.kind},
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
