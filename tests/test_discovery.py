from __future__ import annotations

import random

import pytest

from comfywebstudio.comfy.discovery import (
    NodeKindMap,
    build_raw_param,
    discover,
    prompt_hash,
    raw_param_key,
)
from comfywebstudio.comfy.inject import prepare_prompt, resolve_param_values
from comfywebstudio.comfy.objectinfo import WidgetSpec
from comfywebstudio.core.models import WorkflowRef


def sample_prompt() -> dict:
    """A txt2img-shaped graph wired through our input and output nodes."""
    return {
        "1": {
            "class_type": "WSStringInput",
            "inputs": {"port_name": "prompt", "value": "a cat", "label": "Prompt",
                       "group": "Text", "order": 1},
        },
        "2": {
            "class_type": "WSSeedInput",
            "inputs": {"port_name": "seed", "value": 42, "label": "Seed", "group": "Sampling", "order": 2},
        },
        "3": {
            "class_type": "WSImageInput",
            "inputs": {"port_name": "init_image", "source": "", "label": "Init image"},
        },
        "4": {
            "class_type": "KSampler",
            "inputs": {"seed": ["2", 0], "steps": 20, "cfg": 7.5, "sampler_name": "euler",
                       "scheduler": "normal", "denoise": 1.0},
            "_meta": {"title": "Sampler"},
        },
        "5": {
            "class_type": "WSImageOutput",
            "inputs": {"image": ["4", 0], "port_name": "result", "format": "png", "run_key": ""},
        },
        "6": {
            "class_type": "WSTextOutput",
            "inputs": {"text": ["1", 0], "port_name": "used_prompt", "format": "txt", "run_key": ""},
        },
    }


def workflow_from(prompt: dict) -> WorkflowRef:
    result = discover(prompt)
    return WorkflowRef(name="Sample", ports=result.ports, params=result.params)


# -- discovery -----------------------------------------------------------------------------------------


def test_discovers_input_ports_including_scalars():
    result = discover(sample_prompt())
    inputs = {p.key: p for p in result.ports if p.direction == "in"}

    assert set(inputs) == {"prompt", "seed", "init_image"}
    assert inputs["init_image"].kind == "image"
    assert inputs["prompt"].kind == "string"
    # A scalar can run unconnected using its own value; a media input cannot.
    assert inputs["prompt"].optional is True
    assert inputs["init_image"].optional is False


def test_discovers_output_ports():
    result = discover(sample_prompt())
    outputs = {p.key: p.kind for p in result.ports if p.direction == "out"}
    assert outputs == {"result": "image", "used_prompt": "string"}


def test_scalar_inputs_also_become_editable_params():
    result = discover(sample_prompt())
    params = {p.key: p for p in result.params}

    assert set(params) == {"prompt", "seed"}
    assert params["prompt"].default == "a cat"
    assert params["prompt"].multiline is True
    assert params["prompt"].group == "Text"
    assert params["seed"].is_seed is True
    assert params["seed"].default == 42
    # Media inputs are ports you connect or assign, not fields you type into.
    assert "init_image" not in params


def test_records_every_node_class_for_the_installed_check():
    result = discover(sample_prompt())
    assert "KSampler" in result.node_classes
    assert "WSImageOutput" in result.node_classes


def test_unnamed_port_warns_and_gets_a_fallback_key():
    prompt = {"7": {"class_type": "WSImageOutput", "inputs": {"port_name": "", "run_key": ""}}}
    result = discover(prompt)

    assert [p.key for p in result.ports] == ["node_7"]
    assert any("no name" in w for w in result.warnings)
    # The key has to stay addressable, but "node_7" tells the user nothing on its own.
    assert result.ports[0].display_name == "Unnamed image output"


def test_unnamed_port_is_labelled_with_the_node_title_when_there_is_one():
    prompt = {
        "7": {
            "class_type": "WSVideoOutput",
            "inputs": {"port_name": "", "run_key": ""},
            "_meta": {"title": "Final render"},
        }
    }
    result = discover(prompt)

    assert result.ports[0].key == "node_7"
    assert result.ports[0].display_name == "Final render"


def test_a_named_port_keeps_its_name_as_its_label():
    prompt = {"7": {"class_type": "WSImageOutput", "inputs": {"port_name": "hero", "run_key": ""}}}
    assert discover(prompt).ports[0].display_name == "hero"


def test_duplicate_port_names_are_disambiguated_and_reported():
    prompt = {
        "1": {"class_type": "WSImageOutput", "inputs": {"port_name": "out", "run_key": ""}},
        "2": {"class_type": "WSImageOutput", "inputs": {"port_name": "out", "run_key": ""}},
    }
    result = discover(prompt)

    assert sorted(p.key for p in result.ports) == ["out", "out_2"]
    assert any("Duplicate" in w for w in result.warnings)


def test_manifest_overrides_the_builtin_kind_map():
    manifest = {
        "input_nodes": [{"class_type": "WSFutureInput", "kind": "audio", "value_input": "source"}],
        "output_nodes": [{"class_type": "WSFutureOutput", "kind": "audio"}],
    }
    kinds = NodeKindMap.from_manifest(manifest)
    prompt = {
        "1": {"class_type": "WSFutureInput", "inputs": {"port_name": "music", "source": ""}},
        "2": {"class_type": "WSFutureOutput", "inputs": {"port_name": "mix", "run_key": ""}},
    }
    result = discover(prompt, kind_map=kinds)

    assert {p.key: p.kind for p in result.ports} == {"music": "audio", "mix": "audio"}
    # Built-in classes still work alongside a manifest.
    assert kinds.inputs["WSImageInput"] == "image"


def test_non_webstudio_graph_yields_nothing_but_still_lists_classes():
    result = discover({"1": {"class_type": "KSampler", "inputs": {"steps": 20}}})
    assert result.ports == [] and result.params == []
    assert result.node_classes == {"KSampler"}


# -- hashing -------------------------------------------------------------------------------------------


def test_prompt_hash_is_stable_and_ignores_run_key():
    a = sample_prompt()
    b = sample_prompt()
    b["5"]["inputs"]["run_key"] = "run123/stepA"
    assert prompt_hash(a) == prompt_hash(b)


def test_prompt_hash_changes_with_a_real_edit():
    a = sample_prompt()
    b = sample_prompt()
    b["4"]["inputs"]["steps"] = 30
    assert prompt_hash(a) != prompt_hash(b)


# -- injection -----------------------------------------------------------------------------------------


def test_injects_overrides_and_run_key():
    prompt = sample_prompt()
    workflow = workflow_from(prompt)

    result = prepare_prompt(
        prompt, workflow,
        overrides={"prompt": "a dog"},
        staged_inputs={"init_image": "/abs/path/img.png"},
        run_key="run1/stepA",
    )

    assert result.prompt["1"]["inputs"]["value"] == "a dog"
    assert result.prompt["3"]["inputs"]["source"] == "/abs/path/img.png"
    assert result.prompt["5"]["inputs"]["run_key"] == "run1/stepA"
    assert result.prompt["6"]["inputs"]["run_key"] == "run1/stepA"
    assert sorted(result.output_node_ids) == ["5", "6"]
    assert result.warnings == []


def test_injection_does_not_mutate_the_stored_prompt():
    prompt = sample_prompt()
    workflow = workflow_from(prompt)
    prepare_prompt(prompt, workflow, overrides={"prompt": "changed"}, run_key="r/s")

    assert prompt["1"]["inputs"]["value"] == "a cat"
    assert prompt["5"]["inputs"]["run_key"] == ""


@pytest.mark.parametrize(
    ("mode", "check"),
    [
        ("fixed", lambda before, after: after == before),
        ("increment", lambda before, after: after == before + 1),
        ("randomize", lambda before, after: after != before),
    ],
)
def test_seed_modes(mode, check):
    prompt = sample_prompt()
    workflow = workflow_from(prompt)
    result = prepare_prompt(prompt, workflow, run_key="r/s", seed_mode=mode,
                            rng=random.Random(1234))
    assert check(42, result.prompt["2"]["inputs"]["value"])


def test_values_are_coerced_to_the_widget_type():
    prompt = sample_prompt()
    workflow = workflow_from(prompt)
    result = prepare_prompt(prompt, workflow, overrides={"seed": "77"}, run_key="r/s")
    assert result.prompt["2"]["inputs"]["value"] == 77
    assert isinstance(result.prompt["2"]["inputs"]["value"], int)


def test_stale_port_reference_warns_instead_of_crashing():
    prompt = sample_prompt()
    workflow = workflow_from(prompt)
    del prompt["3"]  # the workflow was edited in ComfyUI and the node removed

    result = prepare_prompt(prompt, workflow, staged_inputs={"init_image": "/x.png"}, run_key="r/s")
    assert any("no longer in the workflow" in w for w in result.warnings)


def test_unknown_staged_port_warns():
    prompt = sample_prompt()
    workflow = workflow_from(prompt)
    result = prepare_prompt(prompt, workflow, staged_inputs={"nope": "/x.png"}, run_key="r/s")
    assert any("No input port" in w for w in result.warnings)


def test_resolve_param_values_applies_defaults_and_overrides():
    workflow = workflow_from(sample_prompt())
    assert resolve_param_values(workflow, {}) == {"prompt": "a cat", "seed": 42}
    assert resolve_param_values(workflow, {"seed": 9}) == {"prompt": "a cat", "seed": 9}


# -- raw widget binding --------------------------------------------------------------------------------


def test_raw_param_has_a_collision_proof_key():
    widget = WidgetSpec(name="steps", kind="int", default=20, min=1, max=100, step=1)
    param = build_raw_param("4", "KSampler", widget, current_value=25, title="Sampler")

    assert param.key == raw_param_key("4", "steps") == "@4.steps"
    assert param.source == "raw_widget"
    assert param.default == 25
    assert param.node_id == "4" and param.input_name == "steps"
    assert "Sampler" in param.label


def test_raw_param_injects_into_a_stock_node():
    prompt = sample_prompt()
    result = discover(prompt)
    widget = WidgetSpec(name="steps", kind="int", default=20)
    workflow = WorkflowRef(
        name="Sample",
        ports=result.ports,
        params=[*result.params, build_raw_param("4", "KSampler", widget, current_value=20)],
    )

    injected = prepare_prompt(prompt, workflow, overrides={"@4.steps": 35}, run_key="r/s")
    assert injected.prompt["4"]["inputs"]["steps"] == 35
    # Links into the same node are untouched.
    assert injected.prompt["4"]["inputs"]["seed"] == ["2", 0]
