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


async def test_subgraphs_are_refused_rather_than_mis_converted(object_info):
    ui = graph([{"id": 1, "type": "EmptyLatentImage", "widgets_values": [512, 512]}], [])
    ui["definitions"] = {"subgraphs": [{"id": "sg1", "nodes": []}]}

    result = await ui_graph_to_prompt(ui, object_info)

    assert result.reliable is False
    assert any("subgraph" in w.lower() for w in result.warnings)
    assert any("Save to ComfyWebStudio" in w for w in result.warnings)


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
