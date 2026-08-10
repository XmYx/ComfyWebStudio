"""Placing one shot inside another.

A shot dropped onto another shot's canvas becomes the same kind of node a template does: one box standing
in for everything inside it, with promoted ports to wire and promoted controls to edit. It gets there by
being *presented* as a :class:`~.templates.ShotTemplate` rather than by being copied — so the node, the
inspector, the preview and the expansion that runs it are all the code that already existed.

Two things make a nested shot different from a library template, and both are deliberate:

* **It is live.** There is no snapshot: the shot is read every time, so editing it updates every place it
  appears. That matches what a template does with its library entry, and it is why "stale" never applies
  here — a nested shot cannot fall behind itself.
* **Its values are instanced.** The controls a nested shot exposes default to whatever the source shot has,
  but the values a user types land in the *instance*. Two placements of the same shot hold different
  values, and neither writes back to the shot they came from.

Nesting is arbitrarily deep, because a shot is flattened before it is captured — a nested shot that itself
places shots contributes their steps too. Cycles are refused when the node is placed, which is what lets
that recursion terminate.
"""

from __future__ import annotations

import logging

from .errors import StudioError
from .models import Project, Shot
from .templates import ShotTemplate, referenced_shot, shot_reference

logger = logging.getLogger(__name__)


class NestingCycle(StudioError):
    """Placing this shot here would make it contain itself."""

    status_code = 409
    code = "nesting_cycle"


def is_shot_instance(instance) -> bool:
    """True when this placed node stands for another shot rather than a library template."""
    return referenced_shot(instance.template_id) is not None


def placed_shot_ids(shot: Shot) -> list[str]:
    """The shots this one places directly, in order."""
    return [
        found for found in (referenced_shot(i.template_id) for i in shot.instances) if found
    ]


def contains_shot(project: Project, container: Shot, target_id: str, *, seen: set[str] | None = None) -> bool:
    """True when `container` places `target_id`, at any depth."""
    seen = seen if seen is not None else set()
    if container.id in seen:
        return False
    seen.add(container.id)

    for placed_id in placed_shot_ids(container):
        if placed_id == target_id:
            return True
        nested = project.shot(placed_id)
        if nested is not None and contains_shot(project, nested, target_id, seen=seen):
            return True
    return False


def check_placeable(project: Project, host: Shot, source_id: str) -> Shot:
    """The shot being placed, or an error explaining why it cannot go here.

    Refusing cycles at placement is what keeps every later step — capture, expansion, validation, running —
    from having to defend itself against infinite recursion.
    """
    if source_id == host.id:
        raise NestingCycle(f"{host.name!r} cannot be placed inside itself.")

    source = project.shot(source_id)
    if source is None:
        raise StudioError(f"No shot {source_id!r} in this project.")
    if source.template_edit_id:
        raise StudioError(
            f"{source.name!r} is a template editing session, not a shot. Save it as a template first."
        )
    if contains_shot(project, source, host.id):
        raise NestingCycle(
            f"{source.name!r} already contains {host.name!r}, so placing it here would loop forever."
        )
    return source


#: What a nested shot always reports, so an instance of one is never considered stale.
LIVE_REVISION = 0


def capture_live_shot(project: Project, shot: Shot, templates: dict[str, ShotTemplate]) -> ShotTemplate:
    """Present a live shot as the template a placed node reads its surface and structure from.

    Unlike :func:`~.template_capture.capture_shot` this bundles nothing. A nested shot lives in the same
    project as the node placing it, so its workflows are already there — which is why the workflow keys
    here *are* the project's own workflow ids, making the instance's workflow map an identity it never has
    to store or re-sync.

    The shot is flattened first, so a nested shot that itself places shots contributes their steps too.
    """
    # Imported here rather than at module scope: template_capture imports this module.
    from .template_capture import capture_structure, flatten_shot

    flat = flatten_shot(shot, templates)
    template = capture_structure(project, flat.shot, name=shot.name)
    template.id = shot_reference(shot.id)
    template.description = shot.notes
    template.source_project = project.name
    # A live shot is read fresh every time, so there is no revision to fall behind. Pinning it means a
    # placed node never offers the "this has moved on, update it" affordance, which would be a lie here.
    template.revision = LIVE_REVISION
    return template


def collect_templates(
    project: Project,
    shot: Shot,
    template_store,
    *,
    into: dict[str, ShotTemplate] | None = None,
    seen: set[str] | None = None,
) -> dict[str, ShotTemplate]:
    """Every template a shot needs to be drawn, validated or run, nested shots included.

    Returned as one flat mapping from reference to template, which is the shape everything downstream
    already expects — so a nested shot reaches the validator, the canvas and the executor as a template
    without any of them being taught a new concept.

    Deepest first: a nested shot has to be captured before the shot placing it can be flattened.
    """
    loaded = into if into is not None else {}
    seen = seen if seen is not None else set()

    for instance in shot.instances:
        reference = instance.template_id
        if reference in loaded or reference in seen:
            continue
        seen.add(reference)

        shot_id = referenced_shot(reference)
        if shot_id is None:
            try:
                loaded[reference] = template_store.get(reference)
            except Exception as exc:  # noqa: BLE001 - a missing template is reported, not fatal
                logger.debug("Template %s could not be loaded: %s", reference, exc)
            continue

        source = project.shot(shot_id)
        if source is None:
            logger.debug("Placed shot %s is no longer in this project", shot_id)
            continue
        # Its own placements first, so flattening it below has everything it needs.
        collect_templates(project, source, template_store, into=loaded, seen=seen)
        loaded[reference] = capture_live_shot(project, source, loaded)

    return loaded
