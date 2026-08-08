"""Best-effort UI-graph to API-prompt conversion.

**This is the fallback, not the main path.** ComfyUI performs this conversion in the browser
(``graphToPrompt``) and there is no server-side equivalent — verified by grepping ComfyUI 0.24.1 for
``widgets_values`` outside build scripts. Our node pack's bridge extension therefore posts *both* formats
back, and the framework normally just stores what ComfyUI itself produced.

This module exists for the cases where that is impossible: a workflow dragged in as UI JSON, or one edited
on a ComfyUI without our pack installed and picked up by the ``/userdata`` poller.

Handled: widget-value ordering from ``/object_info``, link resolution, Reroute passthrough, muted and
bypassed nodes, ``control_after_generate`` extra widget values, PrimitiveNode.

**Not handled: subgraphs.** A graph containing subgraph definitions is reported as a conversion warning
rather than silently mis-converted, because a wrong graph that still executes is far worse than a refusal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .objectinfo import ObjectInfoCache

logger = logging.getLogger(__name__)

#: LiteGraph node modes.
MODE_ALWAYS = 0
MODE_MUTED = 2
MODE_BYPASS = 4

#: Node types that exist only in the editor and never reach the API prompt.
VIRTUAL_NODES = frozenset({"Reroute", "PrimitiveNode", "Note", "MarkdownNote", "Reroute (rgthree)"})

#: Socket types that are values a user types, as opposed to links between nodes.
WIDGET_TYPES = frozenset({"STRING", "INT", "FLOAT", "BOOLEAN"})


@dataclass(slots=True)
class ConversionResult:
    prompt: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: True when we are confident the result matches what ComfyUI would have produced.
    reliable: bool = True


async def ui_graph_to_prompt(
    ui_graph: dict[str, Any], object_info: ObjectInfoCache
) -> ConversionResult:
    """Convert a LiteGraph workflow into an API-format prompt."""
    result = ConversionResult()

    if ui_graph.get("definitions", {}).get("subgraphs"):
        result.reliable = False
        result.warnings.append(
            "This workflow contains subgraphs, which ComfyWebStudio cannot flatten on its own. "
            "Open it in ComfyUI and use 'Save to ComfyWebStudio' so ComfyUI performs the conversion."
        )

    nodes = {str(n["id"]): n for n in ui_graph.get("nodes") or [] if "id" in n}
    if not nodes:
        result.warnings.append("The workflow contains no nodes.")
        return result

    # link_id -> (origin_node_id, origin_slot)
    links: dict[int, tuple[str, int]] = {}
    for link in ui_graph.get("links") or []:
        if isinstance(link, list) and len(link) >= 5:
            links[int(link[0])] = (str(link[1]), int(link[2]))

    resolver = _LinkResolver(nodes, links, result)

    for node_id, node in nodes.items():
        class_type = str(node.get("type") or "")
        if class_type in VIRTUAL_NODES or node.get("mode") in {MODE_MUTED, MODE_BYPASS}:
            continue

        schema = await object_info.node(class_type)
        if schema is None:
            result.reliable = False
            result.warnings.append(
                f"Node type {class_type!r} (node {node_id}) is not installed on this ComfyUI, so its "
                "inputs could not be typed."
            )
            continue

        inputs: dict[str, Any] = {}
        _apply_widget_values(node, schema, inputs, result)
        _apply_linked_inputs(node, inputs, resolver)

        entry: dict[str, Any] = {"class_type": class_type, "inputs": inputs}
        title = node.get("title")
        if title:
            entry["_meta"] = {"title": title}
        result.prompt[node_id] = entry

    return result


def _widget_names(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Widget inputs in the order ``widgets_values`` uses.

    The frontend serialises widget values positionally in ``INPUT_TYPES`` order, skipping anything that is
    a link socket or explicitly ``forceInput``.
    """
    ordered: list[tuple[str, dict[str, Any]]] = []
    inputs = schema.get("input") or {}
    order = (schema.get("input_order") or {})

    for section in ("required", "optional"):
        entries = inputs.get(section) or {}
        names = order.get(section) or list(entries)
        for name in names:
            definition = entries.get(name)
            if not isinstance(definition, list | tuple) or not definition:
                continue
            raw_type = definition[0]
            options = definition[1] if len(definition) > 1 and isinstance(definition[1], dict) else {}
            if options.get("forceInput"):
                continue
            is_combo = isinstance(raw_type, list)
            if not is_combo and str(raw_type) not in WIDGET_TYPES:
                continue
            ordered.append((name, options))
    return ordered


def _apply_widget_values(
    node: dict[str, Any], schema: dict[str, Any], inputs: dict[str, Any], result: ConversionResult
) -> None:
    values = node.get("widgets_values")
    if values is None:
        return

    # Newer frontends sometimes serialise a dict keyed by widget name, which needs no positional guessing.
    if isinstance(values, dict):
        inputs.update(values)
        return

    names = _widget_names(schema)
    index = 0
    for name, options in names:
        if index >= len(values):
            break
        inputs[name] = values[index]
        index += 1
        # A seed-like INT widget is followed by its control_after_generate value, which is editor-only.
        if options.get("control_after_generate") and index < len(values):
            index += 1

    if index < len(values):
        # Extra trailing values are usually control widgets we did not predict. Harmless, but a signal
        # that the positional mapping may have drifted.
        result.warnings.append(
            f"Node {node.get('id')} ({node.get('type')}) had {len(values) - index} unmapped widget "
            "value(s); check its parameters."
        )


def _apply_linked_inputs(node: dict[str, Any], inputs: dict[str, Any], resolver: _LinkResolver) -> None:
    for slot in node.get("inputs") or []:
        name = slot.get("name")
        link_id = slot.get("link")
        if not name or link_id is None:
            continue
        source = resolver.resolve(int(link_id))
        if source is not None:
            inputs[name] = [source[0], source[1]]
        elif name in inputs:
            # The slot was converted from a widget but has no link; keep the widget value.
            continue


class _LinkResolver:
    """Follows a link back to a real node, hopping over reroutes and bypassed nodes."""

    def __init__(
        self,
        nodes: dict[str, dict[str, Any]],
        links: dict[int, tuple[str, int]],
        result: ConversionResult,
    ):
        self.nodes = nodes
        self.links = links
        self.result = result

    def resolve(self, link_id: int, depth: int = 0) -> tuple[str, int] | None:
        if depth > 32:
            self.result.warnings.append("A link chain was too deep to resolve; it was dropped.")
            return None

        origin = self.links.get(link_id)
        if origin is None:
            return None

        node_id, slot = origin
        node = self.nodes.get(node_id)
        if node is None:
            return None

        node_type = str(node.get("type") or "")
        mode = node.get("mode")

        if node_type in VIRTUAL_NODES or mode in {MODE_BYPASS, MODE_MUTED}:
            passthrough = self._passthrough_link(node, slot)
            if passthrough is None:
                if mode == MODE_MUTED:
                    return None  # a muted node genuinely produces nothing
                self.result.warnings.append(
                    f"Could not trace through {node_type or 'node'} {node_id}; a link was dropped."
                )
                return None
            return self.resolve(passthrough, depth + 1)

        return (node_id, slot)

    def _passthrough_link(self, node: dict[str, Any], out_slot: int) -> int | None:
        """Which incoming link a bypassed or reroute node forwards from.

        Reroute has one input. A bypassed node forwards the input whose type matches the output being
        asked for, which is what ComfyUI's own bypass does.
        """
        slots = node.get("inputs") or []
        if not slots:
            return None
        if len(slots) == 1:
            return slots[0].get("link")

        outputs = node.get("outputs") or []
        wanted = outputs[out_slot].get("type") if out_slot < len(outputs) else None
        for slot in slots:
            if wanted is not None and slot.get("type") == wanted and slot.get("link") is not None:
                return slot.get("link")
        return next((s.get("link") for s in slots if s.get("link") is not None), None)
