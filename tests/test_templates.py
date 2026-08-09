"""Shot templates: capturing one, placing it, and unpacking it again.

The load-bearing claim is that a placed instance is *live* — it holds no structure of its own, so what it
runs is whatever the template says today. These pin that down, along with the two things that make it
usable across projects: the workflows travel with the template, and the container node's surface is
derived from what the template does not wire up itself.
"""

from __future__ import annotations

import pytest

from comfywebstudio.core.graph import validate_placed, value_nodes_into
from comfywebstudio.core.models import (
    VALUE_PORT,
    Link,
    Project,
    Shot,
    Step,
    ValueNode,
)
from comfywebstudio.core.template_capture import (
    capture_shot,
    expand_instance,
    flatten_shot,
    inner_id,
    place_instance,
    sync_instance,
    templates_for,
)
from comfywebstudio.core.template_store import TemplateStore

from .test_execution import consumer_prompt, generator_prompt, register


@pytest.fixture
def library(tmp_path) -> TemplateStore:
    return TemplateStore(tmp_path / "templates")


@pytest.fixture
def source_project(app_state) -> Project:
    """Generate -> Consume, with a value node feeding the consumer's caption."""
    project = app_state.store.create("Source")
    gen = register(app_state, project, "Generate", generator_prompt())
    con = register(app_state, project, "Consume", consumer_prompt())

    step_a = Step(name="Generate", workflow_id=gen.id, exposed_params=["prompt"])
    step_b = Step(name="Consume", workflow_id=con.id)
    caption = ValueNode(kind="string", name="Caption", value="from the template")
    shot = Shot(
        name="Reusable bit",
        steps=[step_a, step_b],
        nodes=[caption],
        links=[
            Link(from_step=step_a.id, from_port="image", to_step=step_b.id, to_port="image"),
            Link(from_step=caption.id, from_port=VALUE_PORT, to_step=step_b.id, to_port="caption"),
        ],
    )
    project.shots = [shot]
    app_state.store.save(project)
    return project


def save(app_state, library, project, shot, **kwargs):
    captured = capture_shot(project, shot, app_state.store, **kwargs)
    return library.save(captured.template, captured.graphs)


# -- capture -------------------------------------------------------------------------------------------


def test_capture_lifts_the_whole_shot(app_state, library, source_project):
    template = save(app_state, library, source_project, source_project.shots[0])

    assert template.name == "Reusable bit"
    assert [s.name for s in template.steps] == ["Generate", "Consume"]
    assert [n.name for n in template.nodes] == ["Caption"]
    assert len(template.links) == 2
    # Both workflows travel with it, or it could not be placed in another project.
    assert {w.name for w in template.workflows} == {"Generate", "Consume"}
    assert library.has_workflow(template.id, template.workflows[0].key, "api")


def test_capture_promotes_what_the_template_does_not_wire_itself(app_state, library, source_project):
    template = save(app_state, library, source_project, source_project.shots[0])
    inputs = {p.key for p in template.ports if p.direction == "in"}
    outputs = {p.key for p in template.ports if p.direction == "out"}

    # `image` and `caption` on the consumer are driven from inside, so they are not the node's surface.
    assert "image" not in inputs and "caption" not in inputs
    # The generator's own text input is not driven, so it is.
    assert "prompt" in inputs
    # The consumer's outputs go nowhere inside, so they come out.
    assert {"final", "echo"} <= outputs
    # ...and the generator's image, which is consumed inside, does not.
    assert "image" not in outputs


def test_capture_shows_the_controls_each_step_already_pinned(app_state, library, source_project):
    template = save(app_state, library, source_project, source_project.shots[0])
    shown = {c.key for c in template.shown_controls}
    every = {c.key for c in template.controls}

    # The Generate step pinned `prompt`, and the value node is a knob by construction.
    assert shown == {"generate.prompt", "caption"}
    # ...while everything else is carried, ready to be shown without re-saving the template.
    assert "consume.caption" in every


def test_a_value_node_inside_a_template_stays_editable_per_instance(
    app_state, library, source_project
):
    """Someone who put a text node on the canvas meant it as a knob; a template must not freeze it."""
    template = save(app_state, library, source_project, source_project.shots[0])
    control = next(c for c in template.controls if c.inner_key == "caption")
    assert control.shown and control.spec is not None
    assert control.spec.kind == "string"
    assert control.spec.default == "from the template"

    target = app_state.store.create("Target")
    shot = Shot(name="Uses it")
    target.shots = [shot]
    instance = place_instance(target, shot, template, library, app_state.store)
    instance.param_overrides[control.key] = "set on the node"

    expanded = expand_instance(instance, template)
    node = next(n for n in expanded.nodes if n.id.endswith(":caption"))
    assert node.value == "set on the node"


def test_two_instances_can_hold_different_values_for_the_same_control(
    app_state, library, source_project
):
    template = save(app_state, library, source_project, source_project.shots[0])
    target = app_state.store.create("Target")
    shot = Shot(name="Twice")
    target.shots = [shot]
    first = place_instance(target, shot, template, library, app_state.store)
    second = place_instance(target, shot, template, library, app_state.store)
    first.param_overrides["caption"] = "one"
    second.param_overrides["caption"] = "two"

    values = {
        node.id.split(":")[0]: node.value
        for instance in (first, second)
        for node in expand_instance(instance, template).nodes
    }
    assert values == {first.id: "one", second.id: "two"}


def test_capture_refuses_a_shot_whose_workflow_is_gone(app_state, library, source_project):
    source_project.workflows.clear()
    with pytest.raises(ValueError, match="not in this project"):
        capture_shot(source_project, source_project.shots[0], app_state.store)


# -- placing -------------------------------------------------------------------------------------------


def test_placing_into_a_fresh_project_brings_the_workflows_along(app_state, library, source_project):
    template = save(app_state, library, source_project, source_project.shots[0])

    target = app_state.store.create("Target")
    shot = Shot(name="Uses it")
    target.shots = [shot]
    instance = place_instance(target, shot, template, library, app_state.store)

    assert len(target.workflows) == 2, "the template's workflows should have been imported"
    assert set(instance.workflow_map) == {w.key for w in template.workflows}
    for workflow_id in instance.workflow_map.values():
        assert app_state.store.has_workflow(target.id, workflow_id, "api")


def test_placing_the_same_template_twice_imports_its_workflows_once(app_state, library, source_project):
    template = save(app_state, library, source_project, source_project.shots[0])
    target = app_state.store.create("Target")
    shot = Shot(name="Uses it twice")
    target.shots = [shot]

    first = place_instance(target, shot, template, library, app_state.store)
    second = place_instance(target, shot, template, library, app_state.store)

    assert len(target.workflows) == 2, "identical workflows should be shared, not duplicated"
    assert first.workflow_map == second.workflow_map
    assert first.id != second.id


# -- expansion -----------------------------------------------------------------------------------------


def test_an_instance_expands_into_real_steps(app_state, library, source_project):
    template = save(app_state, library, source_project, source_project.shots[0])
    target = app_state.store.create("Target")
    shot = Shot(name="Uses it")
    target.shots = [shot]
    instance = place_instance(target, shot, template, library, app_state.store, name="Bit")

    expanded = expand_instance(instance, template)
    assert [s.name for s in expanded.steps] == ["Bit · Generate", "Bit · Consume"]
    assert [s.id for s in expanded.steps] == [
        inner_id(instance.id, "generate"), inner_id(instance.id, "consume")
    ]
    # The inner links come with it, rewritten onto the instance-scoped ids.
    assert len(expanded.links) == 2
    assert not expanded.errors


def test_flattening_keeps_the_links_from_inside_the_template(app_state, library, source_project):
    """The wiring inside a template is most of what a template *is*; it has to survive expansion."""
    template = save(app_state, library, source_project, source_project.shots[0])
    target = app_state.store.create("Target")
    shot = Shot(name="Uses it")
    target.shots = [shot]
    instance = place_instance(target, shot, template, library, app_state.store)

    flat = flatten_shot(shot, {template.id: template})
    assert len(flat.shot.links) == 2, "the template's own two links should be in the flattened shot"
    # ...including the one from its value node into a step, which is what carries the value at run time.
    assert any(
        link.from_step == inner_id(instance.id, "caption") and link.to_port == "caption"
        for link in flat.shot.links
    )


def test_a_value_node_control_reaches_the_step_it_feeds(app_state, library, source_project):
    """End to end through the same helper the executor uses, not just through expansion."""
    template = save(app_state, library, source_project, source_project.shots[0])
    target = app_state.store.create("Target")
    shot = Shot(name="Uses it")
    target.shots = [shot]
    instance = place_instance(target, shot, template, library, app_state.store)
    instance.param_overrides["caption"] = "set from outside"

    flat = flatten_shot(shot, {template.id: template})
    consume = inner_id(instance.id, "consume")
    feeding = value_nodes_into(flat.shot, consume)
    assert "caption" in feeding, "the inner value node should be feeding the consumer's caption input"
    assert feeding["caption"].value == "set from outside"


def test_two_instances_of_one_template_do_not_collide(app_state, library, source_project):
    template = save(app_state, library, source_project, source_project.shots[0])
    target = app_state.store.create("Target")
    shot = Shot(name="Twice")
    target.shots = [shot]
    place_instance(target, shot, template, library, app_state.store)
    place_instance(target, shot, template, library, app_state.store)

    flat = flatten_shot(shot, {template.id: template})
    assert len(flat.shot.steps) == 4
    assert len({s.id for s in flat.shot.steps}) == 4, "expanded step ids must be unique per instance"


def test_a_control_value_reaches_the_inner_step(app_state, library, source_project):
    template = save(app_state, library, source_project, source_project.shots[0])
    target = app_state.store.create("Target")
    shot = Shot(name="Uses it")
    target.shots = [shot]
    instance = place_instance(target, shot, template, library, app_state.store)
    instance.param_overrides["generate.prompt"] = "a dog"

    expanded = expand_instance(instance, template)
    generate = next(s for s in expanded.steps if s.id.endswith(":generate"))
    assert generate.param_overrides["prompt"] == "a dog"


def test_a_link_to_a_promoted_port_lands_on_the_inner_port(app_state, library, source_project):
    template = save(app_state, library, source_project, source_project.shots[0])
    target = app_state.store.create("Target")
    outside = register(app_state, target, "Outside", consumer_prompt())
    shot = Shot(name="Uses it")
    target.shots = [shot]

    instance = place_instance(target, shot, template, library, app_state.store)
    downstream = Step(name="Downstream", workflow_id=outside.id)
    shot.steps.append(downstream)
    shot.links.append(
        Link(from_step=instance.id, from_port="final", to_step=downstream.id, to_port="image")
    )

    flat = flatten_shot(shot, {template.id: template})
    link = next(link for link in flat.shot.links if link.to_step == downstream.id)
    assert link.from_step == inner_id(instance.id, "consume")
    assert link.from_port == "final", "the promoted port should resolve to the inner port it stands for"
    assert not flat.errors


def test_a_shot_with_no_instances_is_left_alone(source_project):
    shot = source_project.shots[0]
    assert flatten_shot(shot, {}).shot is shot


# -- staying live --------------------------------------------------------------------------------------


def test_editing_the_template_changes_what_a_placed_instance_runs(app_state, library, source_project):
    template = save(app_state, library, source_project, source_project.shots[0])
    target = app_state.store.create("Target")
    shot = Shot(name="Uses it")
    target.shots = [shot]
    instance = place_instance(target, shot, template, library, app_state.store)
    assert len(flatten_shot(shot, {template.id: template}).shot.steps) == 2

    # The template loses a step. Nothing about the instance changes...
    source_project.shots[0].steps.pop()
    source_project.shots[0].links = []
    updated = save(
        app_state, library, source_project, source_project.shots[0],
        template_id=template.id, revision=template.revision + 1,
    )

    # ...but what it expands to does, because the instance holds no structure of its own.
    flat = flatten_shot(shot, {updated.id: updated})
    assert len(flat.shot.steps) == 1
    assert instance.template_revision < updated.revision, "the instance should read as out of date"


def test_syncing_reports_what_it_reconciled(app_state, library, source_project):
    template = save(app_state, library, source_project, source_project.shots[0])
    target = app_state.store.create("Target")
    shot = Shot(name="Uses it")
    target.shots = [shot]
    instance = place_instance(target, shot, template, library, app_state.store)
    instance.param_overrides["generate.prompt"] = "kept"
    instance.param_overrides["gone.away"] = "dropped"

    updated = save(
        app_state, library, source_project, source_project.shots[0],
        template_id=template.id, revision=template.revision + 1,
    )
    changes = sync_instance(target, instance, updated, library, app_state.store)

    assert instance.template_revision == updated.revision
    assert instance.param_overrides == {"generate.prompt": "kept"}
    assert any("no longer exist" in change for change in changes)


def test_a_missing_template_is_an_error_not_a_silent_skip(app_state, library, source_project):
    target = app_state.store.create("Target")
    shot = Shot(name="Uses it")
    target.shots = [shot]
    template = save(app_state, library, source_project, source_project.shots[0])
    place_instance(target, shot, template, library, app_state.store)
    library.delete(template.id)

    report, _flat = validate_placed(target, shot, library)
    assert not report.ok
    assert any("not in the library" in issue.message for issue in report.errors)


def test_templates_for_skips_what_the_library_has_lost(app_state, library, source_project):
    target = app_state.store.create("Target")
    shot = Shot(name="Uses it")
    target.shots = [shot]
    template = save(app_state, library, source_project, source_project.shots[0])
    place_instance(target, shot, template, library, app_state.store)

    assert set(templates_for(shot, library)) == {template.id}
    library.delete(template.id)
    assert templates_for(shot, library) == {}
