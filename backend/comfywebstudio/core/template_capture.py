"""Turning a shot into a template, and a template back into steps.

Two halves of one idea, kept together because they have to agree exactly on what a key means.

:func:`capture_shot` lifts a shot out of its project: project ids become template-local keys, the
workflows its steps use are bundled, and the surface of the container node is derived — an input nothing
inside drives is a port worth exposing, an output nothing inside consumes is a result worth handing back.

:func:`expand_instance` does the reverse for a placed instance, producing ordinary steps, value nodes and
links that the validator and the executor handle without knowing templates exist at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .ids import slugify
from .models import (
    VALUE_PORT,
    Link,
    ParamSpec,
    Project,
    Shot,
    Step,
    TemplateInstance,
    ValueNode,
    Vec2,
    WorkflowRef,
    utcnow,
)
from .templates import (
    KEY_SEPARATOR,
    ShotTemplate,
    TemplateControl,
    TemplateLink,
    TemplatePort,
    TemplateStep,
    TemplateValueNode,
    TemplateWorkflow,
)

logger = logging.getLogger(__name__)


# -- capture ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class CapturedTemplate:
    """A template and the workflow graphs that have to be stored beside it."""

    template: ShotTemplate
    #: ``"<workflow key>.<fmt>" -> document``, ready for :meth:`TemplateStore.save`.
    graphs: dict[str, dict[str, Any]] = field(default_factory=dict)


def capture_shot(
    project: Project,
    shot: Shot,
    store,
    *,
    name: str = "",
    description: str = "",
    template_id: str | None = None,
    revision: int = 1,
) -> CapturedTemplate:
    """Lift a shot into a reusable template.

    Placed instances inside the shot are *not* captured. Nesting a template inside a template would mean
    a placement that breaks when either one changes, and the honest thing is to say so rather than to
    quietly flatten someone's structure into a copy that no longer tracks its original.
    """
    keys = _keys_for(shot)
    workflow_keys: dict[str, str] = {}
    graphs: dict[str, dict[str, Any]] = {}
    workflows: list[TemplateWorkflow] = []

    for step in shot.steps:
        workflow = project.workflow(step.workflow_id)
        if workflow is None:
            raise ValueError(
                f"Step {step.name!r} points at a workflow that is not in this project, so this shot "
                "cannot be saved as a template."
            )
        if step.workflow_id in workflow_keys:
            continue

        key = _unique(slugify(workflow.name, fallback="workflow"), set(workflow_keys.values()))
        workflow_keys[step.workflow_id] = key

        has_ui = False
        for fmt in ("api", "ui"):
            if not store.has_workflow(project.id, workflow.id, fmt):
                continue
            document = store.read_workflow(project.id, workflow.id, fmt)
            if fmt == "ui" and not document.get("nodes"):
                continue
            graphs[f"{key}.{fmt}"] = document
            has_ui = has_ui or fmt == "ui"

        if f"{key}.api" not in graphs:
            raise ValueError(
                f"Workflow {workflow.name!r} has no stored prompt, so a template using it could not run. "
                "Open it in ComfyUI and save it back first."
            )

        workflows.append(
            TemplateWorkflow(
                key=key,
                name=workflow.name,
                hash=workflow.hash,
                ports=[p.model_copy(deep=True) for p in workflow.ports],
                params=[p.model_copy(deep=True) for p in workflow.params],
                has_ui_graph=has_ui,
            )
        )

    template = ShotTemplate(
        name=name or shot.name,
        description=description,
        revision=revision,
        source_project=project.name,
        workflows=workflows,
        steps=[
            TemplateStep(
                key=keys[step.id],
                name=step.name,
                workflow_key=workflow_keys[step.workflow_id],
                param_overrides=dict(step.param_overrides),
                exposed_params=list(step.exposed_params),
                seed_mode=step.seed_mode,
                enabled=step.enabled,
                notes=step.notes,
                ui_pos=step.ui_pos.model_copy(),
                ui_size=step.ui_size.model_copy(),
            )
            for step in shot.steps
        ],
        nodes=[
            TemplateValueNode(
                key=keys[node.id],
                name=node.name,
                kind=node.kind,
                value=node.value,
                media_kind=node.media_kind,
                ui_pos=node.ui_pos.model_copy(),
                ui_size=node.ui_size.model_copy(),
            )
            for node in shot.nodes
        ],
        links=[
            TemplateLink(
                from_key=keys[link.from_step],
                from_port=link.from_port,
                to_key=keys[link.to_step],
                to_port=link.to_port,
            )
            for link in shot.links
            # A link touching a placed instance cannot come along, because the instance did not.
            if link.from_step in keys and link.to_step in keys
        ],
    )
    if template_id:
        template.id = template_id

    template.ports = _derive_ports(template, project, workflow_keys)
    template.controls = _derive_controls(template, project, workflow_keys)
    return CapturedTemplate(template=template, graphs=graphs)


def _keys_for(shot: Shot) -> dict[str, str]:
    """Project id -> template key, readable and unique within the template."""
    keys: dict[str, str] = {}
    used: set[str] = set()
    for step in shot.steps:
        keys[step.id] = _unique(slugify(step.name, fallback="step"), used)
        used.add(keys[step.id])
    for node in shot.nodes:
        keys[node.id] = _unique(slugify(node.display_name, fallback="value"), used)
        used.add(keys[node.id])
    return keys


def _unique(candidate: str, used: set[str]) -> str:
    if candidate not in used:
        return candidate
    index = 2
    while f"{candidate}-{index}" in used:
        index += 1
    return f"{candidate}-{index}"


def _derive_ports(
    template: ShotTemplate, project: Project, workflow_keys: dict[str, str]
) -> list[TemplatePort]:
    """The container node's ports: whatever the template does not wire up itself.

    An input already driven from inside is not a knob the outside world should reach, and an output
    already consumed inside is an intermediate value. Everything else is surface.
    """
    driven = {(link.to_key, link.to_port) for link in template.links}
    consumed = {(link.from_key, link.from_port) for link in template.links}

    ports: list[TemplatePort] = []
    used: set[str] = set()
    for step in template.steps:
        workflow = template.workflow(step.workflow_key)
        if workflow is None:
            continue
        for port in workflow.ports:
            inside = (step.key, port.key)
            if port.direction == "in" and inside in driven:
                continue
            if port.direction == "out" and inside in consumed:
                continue
            key = _unique(port.key, used)
            used.add(key)
            ports.append(
                TemplatePort(
                    key=key,
                    direction=port.direction,
                    kind=port.kind,
                    inner_key=step.key,
                    inner_port=port.key,
                    label=port.display_name,
                    optional=port.optional,
                )
            )

    # A value node inside the template is a constant it owns, so its output is not surface — unless
    # nothing consumes it, in which case it is a dangling node and exposing it helps no one either.
    return ports


def _derive_controls(
    template: ShotTemplate, project: Project, workflow_keys: dict[str, str]
) -> list[TemplateControl]:
    """Every inner parameter, as a control the node could show.

    All of them are carried so the user can reach anything later without re-saving the template. What is
    *shown* by default is narrower: the parameters each step already pins to its own node, because those
    are the ones its author had already picked out. A step that pins nothing contributes nothing to the
    node's face, which keeps a ten-step template from arriving with two hundred rows.

    Value nodes are the exception and are shown by default. Someone who put a text node on the canvas and
    wired it into a prompt was already saying "this is the knob"; freezing it as a constant the moment the
    shot became a template would take away the very thing they built it for.
    """
    controls: list[TemplateControl] = []
    used: set[str] = set()

    for node in template.nodes:
        spec = _value_node_spec(node)
        if spec is None:
            continue  # a media node holds a project asset, which a template cannot carry
        key = _unique(node.key, used)
        used.add(key)
        controls.append(
            TemplateControl(
                key=key,
                inner_key=node.key,
                inner_param=VALUE_PORT,
                label=node.name or node.key,
                spec=spec,
                shown=True,
            )
        )

    for step in template.steps:
        workflow = template.workflow(step.workflow_key)
        if workflow is None:
            continue
        pinned = set(step.exposed_params)
        for param in workflow.params:
            key = _unique(f"{step.key}.{param.key}", used)
            used.add(key)
            controls.append(
                TemplateControl(
                    key=key,
                    inner_key=step.key,
                    inner_param=param.key,
                    label=f"{step.name} · {param.display_name}",
                    spec=_control_spec(param, step.param_overrides.get(param.key)),
                    shown=param.key in pinned,
                )
            )
    return controls


def _value_node_spec(node: TemplateValueNode) -> ParamSpec | None:
    """A value node as a parameter the container node can render, or None if it cannot be one."""
    if node.kind == "media":
        return None
    return ParamSpec(
        key=node.key,
        kind=node.kind,  # type: ignore[arg-type]
        label=node.name or node.key,
        default=node.value,
        multiline=node.kind == "string",
        # A value node has no node inside a ComfyUI graph to write to — its value lands on the node
        # itself. These are filled in so the spec is well-formed for the widget that renders it.
        node_id=node.key,
        input_name=VALUE_PORT,
    )


def _control_spec(param: ParamSpec, override: Any) -> ParamSpec:
    """The inner parameter as the node should render it, with the shot's own value as the default."""
    spec = param.model_copy(deep=True)
    if override is not None:
        spec.default = override
    return spec


# -- expansion -------------------------------------------------------------------------------------------


@dataclass(slots=True)
class ExpandedInstance:
    """One placed instance, unpacked into things the rest of the system already understands."""

    steps: list[Step] = field(default_factory=list)
    nodes: list[ValueNode] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    #: Instance-scoped id -> the template key it came from, for reporting.
    origin: dict[str, str] = field(default_factory=dict)
    #: Something the user asked for will not happen — a step that cannot run, a link being ignored.
    errors: list[str] = field(default_factory=list)
    #: Something was tidied away and the result is still what the user meant.
    warnings: list[str] = field(default_factory=list)


def inner_id(instance_id: str, key: str) -> str:
    """The id an inner step gets once expanded. Readable, and unique across instances by construction."""
    return f"{instance_id}{KEY_SEPARATOR}{key}"


def expand_instance(
    instance: TemplateInstance, template: ShotTemplate, *, project: Project | None = None
) -> ExpandedInstance:
    """Unpack a placed instance into real steps, value nodes and links.

    The instance's control overrides are written into the inner steps' parameter overrides here, so by the
    time the executor sees a step it is an ordinary step with ordinary values — nothing downstream has to
    know a template was involved.
    """
    result = ExpandedInstance()
    label = instance.name or template.name

    for step in template.steps:
        workflow_id = instance.workflow_map.get(step.workflow_key)
        if not workflow_id:
            result.errors.append(
                f"{label!r} has no workflow for {step.name!r}. The template has changed since it was "
                "placed — update the instance to pick it up."
            )
            continue
        expanded = Step(
            id=inner_id(instance.id, step.key),
            name=f"{label} · {step.name}",
            workflow_id=workflow_id,
            enabled=step.enabled and instance.enabled,
            param_overrides=dict(step.param_overrides),
            exposed_params=list(step.exposed_params),
            seed_mode=step.seed_mode,
            notes=step.notes,
            ui_pos=step.ui_pos.model_copy(),
            ui_size=step.ui_size.model_copy(),
        )
        result.steps.append(expanded)
        result.origin[expanded.id] = step.key

    for node in template.nodes:
        result.nodes.append(
            ValueNode(
                id=inner_id(instance.id, node.key),
                name=node.name,
                kind=node.kind,
                value=node.value,
                media_kind=node.media_kind,
                ui_pos=node.ui_pos.model_copy(),
                ui_size=node.ui_size.model_copy(),
            )
        )

    live = {step.id for step in result.steps} | {node.id for node in result.nodes}
    for link in template.links:
        source, target = inner_id(instance.id, link.from_key), inner_id(instance.id, link.to_key)
        if source in live and target in live:
            result.links.append(
                Link(
                    from_step=source,
                    from_port=link.from_port,
                    to_step=target,
                    to_port=link.to_port,
                )
            )

    _apply_controls(instance, template, result)
    return result


def _apply_controls(
    instance: TemplateInstance, template: ShotTemplate, result: ExpandedInstance
) -> None:
    """Write the instance's control values onto the inner steps and value nodes they address."""
    steps = {step.id: step for step in result.steps}
    nodes = {node.id: node for node in result.nodes}

    for key, value in instance.param_overrides.items():
        control = template.control(key)
        if control is None:
            result.warnings.append(
                f"{instance.name or template.name!r} has a value for {key!r}, which its template no "
                "longer exposes; it was ignored."
            )
            continue

        target_id = inner_id(instance.id, control.inner_key)
        step = steps.get(target_id)
        if step is not None:
            step.param_overrides[control.inner_param] = value
            continue
        node = nodes.get(target_id)
        if node is not None:
            node.value = value


def rewrite_outer_link(
    link: Link, instances: dict[str, TemplateInstance], templates: dict[str, ShotTemplate]
) -> Link | None:
    """A link touching an instance, redirected at the inner port its promoted port stands for.

    Returns None when either end names a promoted port the template no longer has — a link left dangling
    by a template edit, which the validator reports rather than silently honouring.
    """
    rewritten = link.model_copy(deep=True)

    for end in ("from", "to"):
        node_id = getattr(link, f"{end}_step")
        instance = instances.get(node_id)
        if instance is None:
            continue
        template = templates.get(instance.template_id)
        if template is None:
            return None
        direction = "out" if end == "from" else "in"
        port = template.port(getattr(link, f"{end}_port"), direction)
        if port is None or not port.shown:
            return None
        setattr(rewritten, f"{end}_step", inner_id(instance.id, port.inner_key))
        setattr(rewritten, f"{end}_port", port.inner_port)

    return rewritten


def instance_ports(template: ShotTemplate) -> list[TemplatePort]:
    """What the container node offers, in a stable order."""
    return sorted(template.shown_ports, key=lambda p: (p.direction, p.key))


def value_node_output(node: TemplateValueNode) -> str:
    """The port key a template value node exposes internally. Same as any other value node."""
    return VALUE_PORT


# -- placement -------------------------------------------------------------------------------------------


def adopt_workflows(
    project: Project, template: ShotTemplate, template_store, project_store
) -> dict[str, str]:
    """Make sure the project owns a copy of every workflow the template needs.

    The project keeps its own workflows so it stays self-contained: exporting it, or deleting the
    template, must not leave a shot that cannot run. Copies are shared by content hash, so placing the
    same template five times imports its workflows once.
    """
    by_hash = {w.hash: w.id for w in project.workflows.values() if w.hash}
    mapping: dict[str, str] = {}

    for entry in template.workflows:
        existing = by_hash.get(entry.hash) if entry.hash else None
        if existing:
            mapping[entry.key] = existing
            continue

        workflow = WorkflowRef(
            name=entry.name,
            hash=entry.hash,
            ports=[p.model_copy(deep=True) for p in entry.ports],
            params=[p.model_copy(deep=True) for p in entry.params],
            last_synced=utcnow(),
        )
        for fmt in ("api", "ui"):
            if template_store.has_workflow(template.id, entry.key, fmt):
                project_store.write_workflow(
                    project.id, workflow.id, fmt,
                    template_store.read_workflow(template.id, entry.key, fmt),
                )
        project.workflows[workflow.id] = workflow
        mapping[entry.key] = workflow.id
        if entry.hash:
            by_hash[entry.hash] = workflow.id
        logger.info("Imported workflow %r from template %s", entry.name, template.id)

    return mapping


def place_instance(
    project: Project,
    shot: Shot,
    template: ShotTemplate,
    template_store,
    project_store,
    *,
    name: str = "",
    ui_pos: Vec2 | None = None,
) -> TemplateInstance:
    """Put a template on a shot's canvas as one node."""
    instance = TemplateInstance(
        template_id=template.id,
        name=name,
        workflow_map=adopt_workflows(project, template, template_store, project_store),
        template_revision=template.revision,
        ui_pos=ui_pos or Vec2(x=40.0 + 300.0 * len(shot.instances), y=340.0),
    )
    shot.instances.append(instance)
    return instance


def sync_instance(
    project: Project,
    instance: TemplateInstance,
    template: ShotTemplate,
    template_store,
    project_store,
) -> list[str]:
    """Bring a placed instance up to date with its template. Returns what changed, for reporting.

    Only additive reconciliation happens automatically: new workflows are imported, and control values
    the template no longer exposes are dropped. Anything destructive — a promoted port that has gone,
    taking a link with it — is reported by the validator instead, because silently deleting a user's
    connection during a routine update is not a trade worth making.
    """
    changes: list[str] = []

    before = set(instance.workflow_map)
    instance.workflow_map = adopt_workflows(project, template, template_store, project_store)
    added = set(instance.workflow_map) - before
    if added:
        changes.append(f"imported {len(added)} new workflow(s)")

    stale = [key for key in instance.param_overrides if template.control(key) is None]
    for key in stale:
        del instance.param_overrides[key]
    if stale:
        changes.append(f"dropped {len(stale)} value(s) for controls that no longer exist")

    if instance.template_revision != template.revision:
        changes.append(f"revision {instance.template_revision} → {template.revision}")
    instance.template_revision = template.revision
    return changes


# -- flattening ------------------------------------------------------------------------------------------


@dataclass(slots=True)
class FlatShot:
    """A shot with every placed instance unpacked, plus what was lost doing it."""

    shot: Shot
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Expanded step id -> the instance it came from, so a run can be reported against the node.
    owner: dict[str, str] = field(default_factory=dict)


def flatten_shot(shot: Shot, templates: dict[str, ShotTemplate]) -> FlatShot:
    """Replace every placed instance with the steps, value nodes and links it stands for.

    Everything downstream — validation, ordering, execution — runs on the result, so none of it needs to
    know templates exist. A shot with no instances comes back untouched.
    """
    if not shot.instances:
        return FlatShot(shot=shot)

    flat = shot.model_copy(deep=True)
    instances = {i.id: i for i in shot.instances}
    result = FlatShot(shot=flat)
    #: Links from inside the templates. Kept apart from the shot's own, which are rewritten below — the
    #: two lists are combined at the end rather than one overwriting the other.
    inner_links: list[Link] = []

    for instance in shot.instances:
        template = templates.get(instance.template_id)
        if template is None:
            result.errors.append(
                f"{instance.name or instance.template_id!r} refers to a template that is not in the "
                "library, so its steps cannot run."
            )
            continue
        expanded = expand_instance(instance, template)
        flat.steps.extend(expanded.steps)
        flat.nodes.extend(expanded.nodes)
        inner_links.extend(expanded.links)
        result.errors.extend(expanded.errors)
        result.warnings.extend(expanded.warnings)
        for step in expanded.steps:
            result.owner[step.id] = instance.id

    # Links the *shot* drew to an instance's promoted ports now have to point at the inner ports.
    rewritten: list[Link] = []
    for link in shot.links:
        if link.from_step not in instances and link.to_step not in instances:
            rewritten.append(link)
            continue
        redirected = rewrite_outer_link(link, instances, templates)
        if redirected is None:
            result.errors.append(
                f"A link to {_instance_label(link, instances, templates)} points at a port its template "
                "no longer exposes; it was ignored."
            )
            continue
        rewritten.append(redirected)
    flat.links = rewritten + inner_links
    flat.instances = []

    return result


def _instance_label(
    link: Link, instances: dict[str, TemplateInstance], templates: dict[str, ShotTemplate]
) -> str:
    for node_id in (link.from_step, link.to_step):
        instance = instances.get(node_id)
        if instance is not None:
            template = templates.get(instance.template_id)
            return repr(instance.name or (template.name if template else node_id))
    return "a template instance"


def templates_for(shot: Shot, template_store) -> dict[str, ShotTemplate]:
    """Load every template a shot places, skipping any the library has lost."""
    loaded: dict[str, ShotTemplate] = {}
    for instance in shot.instances:
        if instance.template_id in loaded:
            continue
        try:
            loaded[instance.template_id] = template_store.get(instance.template_id)
        except Exception as exc:  # noqa: BLE001 - a missing template is reported, not fatal
            logger.debug("Template %s could not be loaded: %s", instance.template_id, exc)
    return loaded
