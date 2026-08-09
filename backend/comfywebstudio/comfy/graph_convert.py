"""Best-effort UI-graph to API-prompt conversion.

**This is the fallback, not the main path.** ComfyUI performs this conversion in the browser
(``graphToPrompt``) and there is no server-side equivalent — verified by grepping ComfyUI 0.24.1 for
``widgets_values`` outside build scripts. Our node pack's bridge extension therefore posts *both* formats
back, and the framework normally just stores what ComfyUI itself produced.

This module exists for the cases where that is impossible: a workflow dragged in as UI JSON, or one edited
on a ComfyUI without our pack installed and picked up by the ``/userdata`` poller.

Handled: widget-value ordering from ``/object_info``, link resolution, Reroute passthrough, muted and
bypassed nodes, ``control_after_generate`` extra widget values, PrimitiveNode, and **subgraphs** — expanded
recursively using ComfyUI's own ``<instance>:<inner>`` execution ids, so a graph converted here and the
same graph flattened by ComfyUI address their nodes identically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .objectinfo import ObjectInfoCache, combo_options
from .subgraphs import SubgraphFlattener, subgraph_definitions

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
    """Convert a LiteGraph workflow into an API-format prompt, expanding any subgraphs."""
    result = ConversionResult()

    if not (ui_graph.get("nodes") or []):
        result.warnings.append("The workflow contains no nodes.")
        return result

    flattener = SubgraphFlattener(ui_graph)
    flat = flattener.flatten()

    names = subgraph_names(ui_graph)
    if names:
        result.warnings.append(
            f"Expanded {len(names)} subgraph(s) ({', '.join(names[:3])}) into {len(flat.nodes)} nodes. "
            "Their promoted inputs are available as parameters."
        )

    for exec_id, (scope, node) in flat.nodes.items():
        class_type = str(node.get("type") or "")

        schema = await object_info.node(class_type)
        if schema is None:
            result.reliable = False
            result.warnings.append(
                f"Node type {class_type!r} (node {exec_id}) is not installed on this ComfyUI, so its "
                "inputs could not be typed."
            )
            continue

        inputs: dict[str, Any] = {}
        _apply_widget_values(node, schema, inputs, result)

        for slot in node.get("inputs") or []:
            name = slot.get("name")
            if not name or slot.get("link") is None:
                continue
            source = flattener.resolve_input(scope, slot.get("link"))
            if source is not None:
                inputs[name] = [source[0], source[1]]
            # A promoted input the parent left unconnected keeps the widget value written above, which is
            # exactly what ComfyUI shows for an untouched promoted slot.

        entry: dict[str, Any] = {"class_type": class_type, "inputs": inputs}
        title = node.get("title")
        if title:
            entry["_meta"] = {"title": title}
        result.prompt[exec_id] = entry

    result.warnings.extend(flat.warnings)
    return result


def subgraph_names(ui_graph: dict[str, Any]) -> list[str]:
    return [str(sg.get("name") or sg.get("id")) for sg in subgraph_definitions(ui_graph).values()]


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
            # A combo is a widget whichever way it is encoded; missing this silently dropped every
            # sampler, scheduler and model choice from converted graphs.
            is_combo = combo_options(raw_type, options) is not None
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
