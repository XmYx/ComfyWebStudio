from __future__ import annotations

import pytest

from comfywebstudio.core.errors import GraphError
from comfywebstudio.core.graph import (
    topological_order,
    upstream_closure,
    validate_new_link,
    validate_shot,
)
from comfywebstudio.core.models import Link, Shot, Step, can_connect, conversion_note

from .factories import make_workflow


def test_topological_order_follows_links(project):
    shot = project.shots[0]
    order = topological_order(shot)
    assert order == [shot.steps[0].id, shot.steps[1].id]


def test_unlinked_steps_keep_declaration_order(project):
    shot = project.shots[0]
    shot.links = []
    assert topological_order(shot) == [s.id for s in shot.steps]


def test_cycle_is_rejected_and_names_the_steps(project):
    shot = project.shots[0]
    a, b = shot.steps
    shot.links.append(Link(from_step=b.id, from_port="image", to_step=a.id, to_port="image"))

    with pytest.raises(GraphError) as exc:
        topological_order(shot)
    assert "cycle" in str(exc.value).lower()
    assert set(exc.value.details["steps"]) == {a.id, b.id}


def test_upstream_closure_pulls_in_dependencies(project):
    shot = project.shots[0]
    a, b = shot.steps
    assert upstream_closure(shot, {b.id}) == {a.id, b.id}
    assert upstream_closure(shot, {a.id}) == {a.id}


def test_validate_shot_is_clean_for_a_good_graph(project):
    report = validate_shot(project, project.shots[0])
    assert report.ok, [i.message for i in report.errors]
    assert report.order == [s.id for s in project.shots[0].steps]


def test_validate_shot_flags_a_missing_port(project):
    shot = project.shots[0]
    shot.links[0].to_port = "nope"
    report = validate_shot(project, shot)
    assert not report.ok
    assert any("no input port" in i.message for i in report.errors)


def test_validate_shot_flags_a_double_driven_input(project):
    shot = project.shots[0]
    third = make_workflow("Other", outputs=[("image", "image")])
    project.workflows[third.id] = third
    step_c = Step(name="Other", workflow_id=third.id)
    shot.steps.append(step_c)
    shot.links.append(
        Link(from_step=step_c.id, from_port="image", to_step=shot.steps[1].id, to_port="image")
    )

    report = validate_shot(project, shot)
    assert any("more than one link" in i.message for i in report.errors)


def test_validate_shot_warns_about_unconnected_required_input(project):
    shot = project.shots[0]
    shot.links = []
    report = validate_shot(project, shot)
    assert report.ok, "an unconnected input is a warning, not an error"
    assert any("not connected" in i.message for i in report.warnings)


# -- link creation -------------------------------------------------------------------------------------


def test_new_link_rejects_self_connection(project):
    shot = project.shots[0]
    a = shot.steps[0]
    with pytest.raises(GraphError, match="cannot feed itself"):
        validate_new_link(project, shot, Link(from_step=a.id, from_port="image", to_step=a.id, to_port="image"))


def test_new_link_rejects_incompatible_kinds(project):
    shot = project.shots[0]
    audio_wf = make_workflow("Audio", inputs=[("audio", "audio")])
    project.workflows[audio_wf.id] = audio_wf
    step_c = Step(name="Audio", workflow_id=audio_wf.id)
    shot.steps.append(step_c)

    with pytest.raises(GraphError, match="Cannot connect a image output to a audio input"):
        validate_new_link(
            project, shot,
            Link(from_step=shot.steps[0].id, from_port="image", to_step=step_c.id, to_port="audio"),
        )


def test_new_link_rejects_an_already_connected_input(project):
    shot = project.shots[0]
    third = make_workflow("Other", outputs=[("image", "image")])
    project.workflows[third.id] = third
    step_c = Step(name="Other", workflow_id=third.id)
    shot.steps.append(step_c)

    with pytest.raises(GraphError, match="already connected"):
        validate_new_link(
            project, shot,
            Link(from_step=step_c.id, from_port="image", to_step=shot.steps[1].id, to_port="image"),
        )


def test_new_link_rejects_a_cycle_before_storing(project):
    shot = project.shots[0]
    a, b = shot.steps
    before = list(shot.links)

    with pytest.raises(GraphError):
        validate_new_link(project, shot, Link(from_step=b.id, from_port="image", to_step=a.id, to_port="image"))

    assert shot.links == before, "validation must not mutate the shot"


def test_new_link_accepts_a_valid_connection(project):
    shot = project.shots[0]
    shot.links = []
    a, b = shot.steps
    validate_new_link(project, shot, Link(from_step=a.id, from_port="image", to_step=b.id, to_port="image"))


# -- kind compatibility --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("image", "image", True),
        ("image", "mask", True),
        ("mask", "image", True),
        ("int", "float", True),
        ("float", "int", False),
        ("int", "string", True),
        ("image", "audio", False),
        ("audio", "video", False),
        ("image", "file", True),
    ],
)
def test_can_connect(source, target, expected):
    assert can_connect(source, target) is expected


def test_conversion_note_only_for_lossy_links():
    assert conversion_note("image", "image") is None
    assert conversion_note("image", "audio") is None  # illegal, so no note
    assert "luminance" in conversion_note("image", "mask")


def test_disabled_steps_are_skipped_by_runnable_steps(project):
    from comfywebstudio.core.graph import runnable_steps

    shot: Shot = project.shots[0]
    shot.steps[0].enabled = False
    order = topological_order(shot)
    assert [s.id for s in runnable_steps(shot, order)] == [shot.steps[1].id]
