"""Placing one shot inside another.

The claim under test: a nested shot behaves exactly like a placed template — same ports, same controls,
same expansion into runnable steps — with two differences that are the whole point of it being a shot
rather than a library entry. It is *live*, so editing the source shot changes every node standing for it;
and its values are *instanced*, so what a user types into one placement stays there.
"""

from __future__ import annotations

import pytest

from comfywebstudio.core.errors import StudioError
from comfywebstudio.core.graph import validate_placed
from comfywebstudio.core.models import VALUE_PORT, Link, Project, Shot, Step, ValueNode
from comfywebstudio.core.shot_sources import (
    NestingCycle,
    capture_live_shot,
    check_placeable,
    collect_templates,
    contains_shot,
)
from comfywebstudio.core.template_capture import flatten_shot, inner_id, place_instance
from comfywebstudio.core.template_store import TemplateStore
from comfywebstudio.core.templates import shot_reference

from .test_execution import consumer_prompt, generator_prompt, register


@pytest.fixture
def library(tmp_path) -> TemplateStore:
    return TemplateStore(tmp_path / "templates")


@pytest.fixture
def project(app_state) -> Project:
    """A project with an inner shot worth reusing, and an empty outer shot to place it in."""
    project = app_state.store.create("Nesting")
    gen = register(app_state, project, "Generate", generator_prompt())
    con = register(app_state, project, "Consume", consumer_prompt())

    step_a = Step(name="Generate", workflow_id=gen.id, exposed_params=["prompt"])
    step_b = Step(name="Consume", workflow_id=con.id)
    caption = ValueNode(kind="string", name="Caption", value="from the inner shot")
    inner = Shot(
        name="Inner",
        steps=[step_a, step_b],
        nodes=[caption],
        links=[
            Link(from_step=step_a.id, from_port="image", to_step=step_b.id, to_port="image"),
            Link(from_step=caption.id, from_port=VALUE_PORT, to_step=step_b.id, to_port="caption"),
        ],
    )
    project.shots = [inner, Shot(name="Outer")]
    app_state.store.save(project)
    return project


def place(project: Project, host: Shot, source: Shot, library, store, **kwargs):
    """What the API does: check it is allowed, capture the shot live, place it."""
    check_placeable(project, host, source.id)
    template = capture_live_shot(project, source, collect_templates(project, source, library))
    return place_instance(project, host, template, library, store, **kwargs)


def test_a_nested_shot_offers_the_same_surface_a_template_would(project, library, app_state):
    inner, outer = project.shots
    instance = place(project, outer, inner, library, app_state.store)
    template = collect_templates(project, outer, library)[instance.template_id]

    # Surface is whatever the shot does not wire up itself: the generator's own input, and every output
    # nothing inside consumes. The image the generator hands the consumer is internal and stays hidden,
    # and so does the consumer's caption input, which the value node already drives.
    assert {(p.direction, p.key) for p in template.shown_ports} == {
        ("in", "prompt"), ("out", "caption"), ("out", "final"), ("out", "echo"),
    }
    assert not any(p.key == "image" for p in template.shown_ports)
    # The value node and the step's pinned parameter are the knobs, exactly as for a template.
    assert {c.key for c in template.shown_controls} == {"caption", "generate.prompt"}


def test_it_expands_into_runnable_steps(project, library, app_state):
    inner, outer = project.shots
    instance = place(project, outer, inner, library, app_state.store)

    flat = flatten_shot(outer, collect_templates(project, outer, library))

    assert not flat.errors
    assert [step.id for step in flat.shot.steps] == [
        inner_id(instance.id, "generate"), inner_id(instance.id, "consume")
    ]
    # The workflows are the project's own, so nothing had to be copied to make this runnable.
    assert {step.workflow_id for step in flat.shot.steps} == {s.workflow_id for s in inner.steps}
    assert all(flat.owner[step.id] == instance.id for step in flat.shot.steps)

    report, _ = validate_placed(project, outer, library)
    assert report.ok, [i.message for i in report.errors]


def test_placing_a_shot_copies_no_workflows(project, library, app_state):
    inner, outer = project.shots
    before = set(project.workflows)
    instance = place(project, outer, inner, library, app_state.store)

    assert set(project.workflows) == before, "a nested shot must reuse the project's workflows"
    assert instance.workflow_map == {}, "and so has no mapping to keep in sync"


def test_values_are_instanced_not_shared(project, library, app_state):
    """The point of the feature: two placements of one shot hold different values."""
    inner, outer = project.shots
    first = place(project, outer, inner, library, app_state.store)
    second = place(project, outer, inner, library, app_state.store)

    first.param_overrides["caption"] = "left"
    second.param_overrides["caption"] = "right"

    flat = flatten_shot(outer, collect_templates(project, outer, library))
    values = {
        node.id: node.value for node in flat.shot.nodes
    }
    assert values[inner_id(first.id, "caption")] == "left"
    assert values[inner_id(second.id, "caption")] == "right"
    # And the shot they came from is untouched, which is what "instanced" has to mean.
    assert inner.nodes[0].value == "from the inner shot"


def test_editing_the_source_shot_reaches_every_placement(project, library, app_state):
    """Live, like a template: there is no snapshot to fall behind."""
    inner, outer = project.shots
    instance = place(project, outer, inner, library, app_state.store)

    inner.steps[0].name = "Renamed"
    inner.nodes[0].value = "edited"

    flat = flatten_shot(outer, collect_templates(project, outer, library))
    assert any("Renamed" in step.name for step in flat.shot.steps)
    # No override on the instance, so the source shot's own value is what comes through.
    assert flat.shot.node(inner_id(instance.id, "caption")).value == "edited"


def test_a_nested_shot_is_never_stale(project, library, app_state):
    inner, outer = project.shots
    instance = place(project, outer, inner, library, app_state.store)
    template = collect_templates(project, outer, library)[instance.template_id]
    assert instance.template_revision == template.revision


def test_nesting_goes_deeper_than_one_level(project, library, app_state):
    """A placed shot that itself places a shot contributes those steps too."""
    inner, outer = project.shots
    middle = Shot(name="Middle")
    project.shots.append(middle)

    place(project, middle, inner, library, app_state.store)
    place(project, outer, middle, library, app_state.store)

    flat = flatten_shot(outer, collect_templates(project, outer, library))
    assert not flat.errors, flat.errors
    assert len(flat.shot.steps) == 2, "the innermost shot's steps have to arrive at the top"
    assert not flat.shot.instances


def test_a_shot_cannot_be_placed_inside_itself(project, library, app_state):
    _inner, outer = project.shots
    with pytest.raises(NestingCycle):
        check_placeable(project, outer, outer.id)


def test_a_cycle_two_levels_down_is_refused(project, library, app_state):
    inner, outer = project.shots
    place(project, inner, outer, library, app_state.store)  # inner now contains outer

    assert contains_shot(project, inner, outer.id)
    with pytest.raises(NestingCycle):
        check_placeable(project, outer, inner.id)


def test_placing_a_shot_that_does_not_exist_says_so(project, library, app_state):
    _inner, outer = project.shots
    with pytest.raises(StudioError):
        check_placeable(project, outer, "shot_nope")


def test_the_reference_names_the_shot_it_stands_for(project, library, app_state):
    inner, outer = project.shots
    instance = place(project, outer, inner, library, app_state.store)
    assert instance.template_id == shot_reference(inner.id)


async def test_a_nested_shot_actually_runs(project, library, app_state):
    """End to end against the fake ComfyUI: the steps inside a placed shot execute and report back."""
    from .test_execution import run_to_completion

    inner, outer = project.shots
    instance = place(project, outer, inner, library, app_state.store)
    instance.param_overrides["caption"] = "instanced"
    app_state.store.save(project)

    run = await run_to_completion(app_state, project, outer)

    assert run.status == "success", run.error
    # Reported against the expanded ids, which is what lets the container node show its own progress.
    assert {sr.step_id for sr in run.step_runs} == {
        inner_id(instance.id, "generate"), inner_id(instance.id, "consume")
    }
    assert all(sr.status == "success" for sr in run.step_runs)

    # The instance's own value is what the inner step consumed, not the source shot's.
    consumed = next(sr for sr in run.step_runs if sr.step_id.endswith("consume"))
    assert consumed.output("echo").meta["value"] == "instanced"
