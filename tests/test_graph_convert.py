"""Tests for the UI-graph -> API-prompt fallback converter.

This is the path used when a workflow arrives as LiteGraph JSON and the bridge extension is not available
to run ComfyUI's own graphToPrompt(). It is best-effort by design, so these tests pin down both what it
handles and where it explicitly gives up.
"""

from __future__ import annotations

import pytest

from comfywebstudio.comfy.graph_convert import ui_graph_to_prompt
from comfywebstudio.comfy.objectinfo import ObjectInfoCache


class FakeObjectInfo(ObjectInfoCache):
    """Stands in for a live ComfyUI's /object_info."""

    def __init__(self, schemas: dict):
        self._schemas = schemas

    async def all(self, *, refresh: bool = False) -> dict:  # noqa: ARG002
        return self._schemas

    async def node(self, class_type: str):
        return self._schemas.get(class_type)


def schema(name: str, required: dict, order: list[str] | None = None) -> dict:
    return {
        "name": name,
        "input": {"required": required, "optional": {}},
        "input_order": {"required": order or list(required), "optional": []},
        "output": ["IMAGE"],
    }


@pytest.fixture
def object_info() -> FakeObjectInfo:
    return FakeObjectInfo(
        {
            "KSampler": schema(
                "KSampler",
                {
                    "model": ["MODEL"],
                    "positive": ["CONDITIONING"],
                    "seed": ["INT", {"default": 0, "control_after_generate": True}],
                    "steps": ["INT", {"default": 20}],
                    "cfg": ["FLOAT", {"default": 8.0}],
                    "sampler_name": [["euler", "dpmpp_2m"], {"default": "euler"}],
                },
            ),
            "EmptyLatentImage": schema(
                "EmptyLatentImage",
                {"width": ["INT", {"default": 512}], "height": ["INT", {"default": 512}]},
            ),
            "CLIPTextEncode": schema(
                "CLIPTextEncode",
                {"text": ["STRING", {"multiline": True}], "clip": ["CLIP"]},
            ),
        }
    )


def graph(nodes: list[dict], links: list[list]) -> dict:
    return {"nodes": nodes, "links": links, "version": 0.4}


async def test_widget_values_map_positionally(object_info):
    result = await ui_graph_to_prompt(
        graph([{"id": 1, "type": "EmptyLatentImage", "widgets_values": [768, 512]}], []),
        object_info,
    )
    assert result.prompt["1"]["inputs"] == {"width": 768, "height": 512}
    assert result.reliable


async def test_control_after_generate_extra_value_is_skipped(object_info):
    """A seed widget serialises two values; only the first is a real input."""
    result = await ui_graph_to_prompt(
        graph(
            [{"id": 1, "type": "KSampler", "widgets_values": [12345, "randomize", 30, 7.5, "dpmpp_2m"]}],
            [],
        ),
        object_info,
    )
    inputs = result.prompt["1"]["inputs"]
    assert inputs["seed"] == 12345
    assert inputs["steps"] == 30, "the control_after_generate value shifted the mapping"
    assert inputs["cfg"] == 7.5
    assert inputs["sampler_name"] == "dpmpp_2m"


async def test_links_become_node_references(object_info):
    result = await ui_graph_to_prompt(
        graph(
            [
                {"id": 1, "type": "CLIPTextEncode", "widgets_values": ["a cat"],
                 "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING"}]},
                {"id": 2, "type": "KSampler", "widgets_values": [0, "fixed", 20, 8.0, "euler"],
                 "inputs": [{"name": "positive", "type": "CONDITIONING", "link": 10}]},
            ],
            [[10, 1, 0, 2, 0, "CONDITIONING"]],
        ),
        object_info,
    )
    assert result.prompt["2"]["inputs"]["positive"] == ["1", 0]


async def test_reroute_is_traced_through(object_info):
    result = await ui_graph_to_prompt(
        graph(
            [
                {"id": 1, "type": "CLIPTextEncode", "widgets_values": ["x"],
                 "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING"}]},
                {"id": 5, "type": "Reroute",
                 "inputs": [{"name": "", "type": "*", "link": 10}],
                 "outputs": [{"name": "", "type": "CONDITIONING"}]},
                {"id": 2, "type": "KSampler", "widgets_values": [0, "fixed", 20, 8.0, "euler"],
                 "inputs": [{"name": "positive", "type": "CONDITIONING", "link": 11}]},
            ],
            [[10, 1, 0, 5, 0, "CONDITIONING"], [11, 5, 0, 2, 0, "CONDITIONING"]],
        ),
        object_info,
    )
    assert "5" not in result.prompt, "the reroute must not appear in the API prompt"
    assert result.prompt["2"]["inputs"]["positive"] == ["1", 0]


async def test_bypassed_node_passes_its_input_through(object_info):
    """A bypassed node forwards the matching input, exactly as ComfyUI's own bypass does."""
    result = await ui_graph_to_prompt(
        graph(
            [
                {"id": 1, "type": "EmptyLatentImage", "widgets_values": [512, 512],
                 "outputs": [{"name": "LATENT", "type": "LATENT"}]},
                {"id": 3, "type": "KSampler", "mode": 4,
                 "inputs": [{"name": "model", "type": "LATENT", "link": 20}],
                 "outputs": [{"name": "LATENT", "type": "LATENT"}]},
                {"id": 4, "type": "KSampler", "widgets_values": [0, "fixed", 20, 8.0, "euler"],
                 "inputs": [{"name": "model", "type": "LATENT", "link": 21}]},
            ],
            [[20, 1, 0, 3, 0, "LATENT"], [21, 3, 0, 4, 0, "LATENT"]],
        ),
        object_info,
    )
    assert "3" not in result.prompt
    assert result.prompt["4"]["inputs"]["model"] == ["1", 0]


async def test_muted_node_produces_nothing(object_info):
    result = await ui_graph_to_prompt(
        graph(
            [
                {"id": 1, "type": "EmptyLatentImage", "mode": 2, "widgets_values": [512, 512],
                 "outputs": [{"name": "LATENT", "type": "LATENT"}]},
                {"id": 2, "type": "KSampler", "widgets_values": [0, "fixed", 20, 8.0, "euler"],
                 "inputs": [{"name": "model", "type": "LATENT", "link": 30}]},
            ],
            [[30, 1, 0, 2, 0, "LATENT"]],
        ),
        object_info,
    )
    assert "1" not in result.prompt
    assert "model" not in result.prompt["2"]["inputs"]


async def test_notes_and_primitives_are_dropped(object_info):
    result = await ui_graph_to_prompt(
        graph(
            [
                {"id": 1, "type": "Note", "widgets_values": ["remember this"]},
                {"id": 2, "type": "EmptyLatentImage", "widgets_values": [512, 512]},
            ],
            [],
        ),
        object_info,
    )
    assert set(result.prompt) == {"2"}


async def test_unknown_node_is_reported_and_marks_the_result_unreliable(object_info):
    result = await ui_graph_to_prompt(
        graph([{"id": 1, "type": "SomeCustomNode", "widgets_values": [1]}], []),
        object_info,
    )
    assert result.prompt == {}
    assert result.reliable is False
    assert any("not installed" in w for w in result.warnings)


async def test_an_empty_subgraph_definition_is_harmless(object_info):
    ui = graph([{"id": 1, "type": "EmptyLatentImage", "widgets_values": [512, 512]}], [])
    ui["definitions"] = {"subgraphs": [{"id": "sg1", "name": "Unused", "nodes": [], "links": []}]}

    result = await ui_graph_to_prompt(ui, object_info)

    assert result.reliable is True
    assert result.prompt["1"]["inputs"] == {"width": 512, "height": 512}


async def test_node_title_is_preserved_as_meta(object_info):
    result = await ui_graph_to_prompt(
        graph([{"id": 1, "type": "EmptyLatentImage", "title": "Canvas", "widgets_values": [512, 512]}], []),
        object_info,
    )
    assert result.prompt["1"]["_meta"] == {"title": "Canvas"}


async def test_dict_widget_values_are_used_directly(object_info):
    """Newer frontends can serialise widgets by name, which needs no positional guessing."""
    result = await ui_graph_to_prompt(
        graph([{"id": 1, "type": "EmptyLatentImage", "widgets_values": {"width": 640, "height": 480}}], []),
        object_info,
    )
    assert result.prompt["1"]["inputs"] == {"width": 640, "height": 480}


async def test_empty_graph_warns(object_info):
    result = await ui_graph_to_prompt({"nodes": [], "links": []}, object_info)
    assert result.prompt == {}
    assert any("no nodes" in w for w in result.warnings)


# -- subgraphs ------------------------------------------------------------------------------------------


def subgraph_workflow() -> dict:
    """A graph with one subgraph instance whose `size` input is promoted and drives two inner nodes.

    Shaped like a real ComfyUI document: object-form links inside the definition, array-form outside, the
    virtual -10/-20 endpoints, and a promoted slot the parent leaves unconnected.
    """
    definition = {
        "id": "sg-uuid",
        "name": "Make Latent",
        "inputNode": {"id": -10},
        "outputNode": {"id": -20},
        "inputs": [
            {"id": "i1", "name": "size", "type": "INT", "linkIds": [101, 102]},
            {"id": "i2", "name": "text", "type": "STRING", "linkIds": [103]},
        ],
        "outputs": [{"id": "o1", "name": "LATENT", "type": "LATENT", "linkIds": [110]}],
        "nodes": [
            {
                "id": 5,
                "type": "EmptyLatentImage",
                "widgets_values": [640, 480],
                "inputs": [
                    {"name": "width", "type": "INT", "widget": {"name": "width"}, "link": 101},
                    {"name": "height", "type": "INT", "widget": {"name": "height"}, "link": 102},
                ],
                "outputs": [{"name": "LATENT", "type": "LATENT"}],
            },
            {
                "id": 6,
                "type": "CLIPTextEncode",
                "widgets_values": ["inner prompt"],
                "inputs": [
                    {"name": "text", "type": "STRING", "widget": {"name": "text"}, "link": 103},
                    {"name": "clip", "type": "CLIP", "link": None},
                ],
                "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING"}],
            },
        ],
        "links": [
            {"id": 101, "origin_id": -10, "origin_slot": 0, "target_id": 5, "target_slot": 0, "type": "INT"},
            {"id": 102, "origin_id": -10, "origin_slot": 0, "target_id": 5, "target_slot": 1, "type": "INT"},
            {"id": 103, "origin_id": -10, "origin_slot": 1, "target_id": 6, "target_slot": 0, "type": "STRING"},
            {"id": 110, "origin_id": 5, "origin_slot": 0, "target_id": -20, "target_slot": 0, "type": "LATENT"},
        ],
    }

    document = graph(
        [
            {
                "id": 20,
                "type": "sg-uuid",
                "inputs": [
                    {"name": "size", "type": "INT", "widget": {"name": "size"}, "link": None},
                    {"name": "text", "type": "STRING", "widget": {"name": "text"}, "link": None},
                ],
                "outputs": [{"name": "LATENT", "type": "LATENT", "links": [300]}],
                "widgets_values": [],
            },
            {
                "id": 30,
                "type": "KSampler",
                "widgets_values": [1, "fixed", 20, 8.0, "euler"],
                "inputs": [{"name": "model", "type": "LATENT", "link": 300}],
            },
        ],
        [[300, 20, 0, 30, 0, "LATENT"]],
    )
    document["definitions"] = {"subgraphs": [definition]}
    return document


async def test_subgraph_is_expanded_with_comfyui_execution_ids(object_info):
    result = await ui_graph_to_prompt(subgraph_workflow(), object_info)

    # The instance itself disappears; its contents appear under `<instance>:<inner>`.
    assert "20" not in result.prompt
    assert set(result.prompt) == {"20:5", "20:6", "30"}
    assert result.reliable is True


async def test_a_link_out_of_a_subgraph_reaches_the_inner_producer(object_info):
    result = await ui_graph_to_prompt(subgraph_workflow(), object_info)
    # The parent consumed the instance's LATENT output; it must now point at the inner node.
    assert result.prompt["30"]["inputs"]["model"] == ["20:5", 0]


async def test_unconnected_promoted_inputs_keep_the_inner_widget_values(object_info):
    result = await ui_graph_to_prompt(subgraph_workflow(), object_info)
    assert result.prompt["20:5"]["inputs"] == {"width": 640, "height": 480}
    assert result.prompt["20:6"]["inputs"]["text"] == "inner prompt"


async def test_promoted_inputs_are_collected_with_every_target():
    from comfywebstudio.comfy.subgraphs import collect_promoted_params

    params = {p.name: p for p in collect_promoted_params(subgraph_workflow())}

    assert set(params) == {"size", "text"}
    size = params["size"]
    assert size.type == "INT"
    assert size.subgraph_name == "Make Latent"
    assert size.instance_path == "20"
    # One promoted slot drives two inner inputs; both must be recorded.
    assert [(t.node_id, t.input_name) for t in size.targets] == [("20:5", "width"), ("20:5", "height")]
    assert size.default == 640
    assert params["text"].default == "inner prompt"


async def test_promoted_inputs_become_typed_parameters(object_info):
    from comfywebstudio.comfy.discovery import subgraph_params

    ui = subgraph_workflow()
    prompt = (await ui_graph_to_prompt(ui, object_info)).prompt
    specs = {p.label: p for p in await subgraph_params(ui, prompt, object_info)}

    assert set(specs) == {"size", "text"}
    size = specs["size"]
    assert size.source == "subgraph"
    assert size.key == "$20.size"
    assert size.group == "Make Latent"
    assert size.kind == "int"
    assert [(t.node_id, t.input_name) for t in size.targets] == [("20:5", "width"), ("20:5", "height")]
    # Typed from the node it feeds, not from the coarse slot type.
    assert specs["text"].kind == "string" and specs["text"].multiline is True


async def test_a_promoted_value_reaches_every_input_it_drives(object_info):
    from comfywebstudio.comfy.discovery import subgraph_params
    from comfywebstudio.comfy.inject import prepare_prompt
    from comfywebstudio.core.models import WorkflowRef

    ui = subgraph_workflow()
    prompt = (await ui_graph_to_prompt(ui, object_info)).prompt
    workflow = WorkflowRef(name="SG", params=await subgraph_params(ui, prompt, object_info))

    injected = prepare_prompt(prompt, workflow, overrides={"$20.size": 1024}, run_key="r/s")

    assert injected.prompt["20:5"]["inputs"]["width"] == 1024
    assert injected.prompt["20:5"]["inputs"]["height"] == 1024, "the fan-out missed a target"
    assert injected.warnings == []


async def test_an_instance_widget_value_overrides_the_inner_default():
    from comfywebstudio.comfy.subgraphs import collect_promoted_params

    ui = subgraph_workflow()
    instance = next(n for n in ui["nodes"] if n["id"] == 20)
    instance["widgets_values"] = [768, "outer prompt"]

    params = {p.name: p for p in collect_promoted_params(ui)}
    assert params["size"].default == 768 and params["size"].overridden is True
    assert params["text"].default == "outer prompt"


async def test_a_connected_promoted_input_is_not_a_parameter():
    """Something the parent wired in is a connection, not a knob."""
    from comfywebstudio.comfy.subgraphs import collect_promoted_params

    ui = subgraph_workflow()
    instance = next(n for n in ui["nodes"] if n["id"] == 20)
    instance["inputs"][0]["link"] = 999

    assert {p.name for p in collect_promoted_params(ui)} == {"text"}


async def test_nested_subgraphs_nest_their_ids(object_info):
    """A subgraph inside a subgraph keeps extending the id path, as ComfyUI does."""
    ui = subgraph_workflow()
    inner = ui["definitions"]["subgraphs"][0]

    outer = {
        "id": "outer-uuid",
        "name": "Outer",
        "inputNode": {"id": -10},
        "outputNode": {"id": -20},
        "inputs": [],
        "outputs": [{"id": "o", "name": "LATENT", "type": "LATENT", "linkIds": [501]}],
        "nodes": [
            {
                "id": 7,
                "type": "sg-uuid",
                "inputs": [
                    {"name": "size", "type": "INT", "widget": {"name": "size"}, "link": None},
                    {"name": "text", "type": "STRING", "widget": {"name": "text"}, "link": None},
                ],
                "outputs": [{"name": "LATENT", "type": "LATENT"}],
                "widgets_values": [],
            }
        ],
        "links": [
            {"id": 501, "origin_id": 7, "origin_slot": 0, "target_id": -20, "target_slot": 0,
             "type": "LATENT"},
        ],
    }
    ui["definitions"]["subgraphs"] = [inner, outer]
    ui["nodes"] = [
        {"id": 40, "type": "outer-uuid", "inputs": [], "outputs": [{"name": "LATENT", "links": [600]}]},
        {"id": 41, "type": "KSampler", "widgets_values": [1, "fixed", 20, 8.0, "euler"],
         "inputs": [{"name": "model", "type": "LATENT", "link": 600}]},
    ]
    ui["links"] = [[600, 40, 0, 41, 0, "LATENT"]]

    result = await ui_graph_to_prompt(ui, object_info)

    assert "40:7:5" in result.prompt, f"nested ids wrong: {sorted(result.prompt)}"
    assert result.prompt["41"]["inputs"]["model"] == ["40:7:5", 0]


# -- combo encodings ------------------------------------------------------------------------------------


async def test_both_combo_encodings_are_mapped():
    """ComfyUI 0.24 emits combos two ways and uses both in the same response.

    Model pickers keep the legacy ``[[...options...], {}]`` form while sampler and scheduler pickers use
    ``["COMBO", {"options": [...]}]``. Handling only the first silently dropped every sampler and scheduler
    choice from a converted graph.
    """
    info = FakeObjectInfo(
        {
            "KSamplerSelect": schema("KSamplerSelect", {
                "sampler_name": ["COMBO", {"options": ["euler", "heun"], "default": "euler"}],
            }),
            "VAELoader": schema("VAELoader", {
                "vae_name": [["ae.safetensors", "other.safetensors"], {}],
            }),
        }
    )

    result = await ui_graph_to_prompt(
        graph(
            [
                {"id": 1, "type": "KSamplerSelect", "widgets_values": ["heun"]},
                {"id": 2, "type": "VAELoader", "widgets_values": ["other.safetensors"]},
            ],
            [],
        ),
        info,
    )

    assert result.prompt["1"]["inputs"] == {"sampler_name": "heun"}
    assert result.prompt["2"]["inputs"] == {"vae_name": "other.safetensors"}
    assert not any("unmapped" in w for w in result.warnings)


async def test_widget_specs_expose_choices_for_both_encodings():
    from comfywebstudio.comfy.objectinfo import combo_options

    info = FakeObjectInfo(
        {
            "N": schema("N", {
                "new_style": ["COMBO", {"options": ["a", "b"]}],
                "old_style": [["x", "y", "z"], {}],
                "plain": ["INT", {"default": 3}],
            }),
        }
    )
    widgets = {w.name: w for w in await info.widgets("N")}

    assert widgets["new_style"].kind == "choice" and widgets["new_style"].choices == ["a", "b"]
    assert widgets["old_style"].kind == "choice" and widgets["old_style"].choices == ["x", "y", "z"]
    assert widgets["plain"].kind == "int"

    assert combo_options("COMBO", {"options": ["p"]}) == ["p"]
    assert combo_options(["q"], {}) == ["q"]
    assert combo_options("INT", {}) is None
