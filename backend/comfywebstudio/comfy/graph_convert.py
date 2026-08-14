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
from .subgraphs import SubgraphFlattener, subgraph_definitions, widget_names

logger = logging.getLogger(__name__)

#: LiteGraph node modes.
MODE_ALWAYS = 0
MODE_MUTED = 2
MODE_BYPASS = 4

#: Node types that exist only in the editor and never reach the API prompt.
VIRTUAL_NODES = frozenset({"Reroute", "PrimitiveNode", "Note", "MarkdownNote", "Reroute (rgthree)"})

#: Socket types that are values a user types, as opposed to links between nodes.
WIDGET_TYPES = frozenset({"STRING", "INT", "FLOAT", "BOOLEAN"})

#: Bumped whenever this converter's output changes.
#:
#: A workflow records the version that produced its stored prompt, so a fix here reaches graphs that were
#: imported before it — otherwise nothing re-reads a ComfyUI file that has not changed, and a workflow
#: converted by a buggy version keeps its buggy prompt until somebody deletes and re-imports it.
#:
#: 2: widget order taken from the schema rather than the node's converted inputs, and dynamic combos
#:    expanded into the dotted inputs ComfyUI requires.
CONVERTER_VERSION = 2

#: Marks a prompt ComfyUI produced itself, through the bridge or an API-format export. No version of this
#: converter improves on that, so it is never re-converted.
EXACT = -1


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
            listed = combo_options(raw_type, options) is not None
            # A *dynamic* combo is one too, and it brings friends: picking a mode reveals that mode's own
            # widgets, each taking a slot of its own and each required by name. Skipping it entirely — as
            # this used to — lost its value and shifted everything after it.
            dynamic = not listed and "COMBO" in str(raw_type).upper()
            if not listed and not dynamic and str(raw_type) not in WIDGET_TYPES:
                continue
            ordered.append((name, {**options, "_dynamic": dynamic} if dynamic else options))
    return ordered


def _is_widget(raw_type: Any, options: dict[str, Any]) -> bool:
    if options.get("forceInput"):
        return False
    return (
        combo_options(raw_type, options) is not None
        or "COMBO" in str(raw_type).upper()
        or str(raw_type) in WIDGET_TYPES
    )


def _revealed_by(
    options: dict[str, Any], chosen: Any, prefix: str
) -> list[tuple[str, dict[str, Any]]]:
    """The widgets a dynamic combo reveals once a mode is picked, in the order it serialises them.

    Each option in the schema carries its own ``inputs`` block, so this is read rather than guessed —
    picking ``on`` on a sampler reveals temperature, top_k, top_p and the rest.

    They arrive **under the combo's own name**: ComfyUI builds their ids by joining the path with dots
    (`_io.finalize_prefix`), so what it expects in the prompt is ``sampling_mode.temperature``, not
    ``temperature``. Sending the bare name looks right, validates as missing, and gets the output that
    depends on the node quietly ignored.
    """
    for option in options.get("options") or []:
        if str(option.get("key")) != str(chosen):
            continue
        revealed: list[tuple[str, dict[str, Any]]] = []
        for section in ("required", "optional"):
            for name, definition in ((option.get("inputs") or {}).get(section) or {}).items():
                if not isinstance(definition, list | tuple) or not definition:
                    continue
                inner = definition[1] if len(definition) > 1 and isinstance(definition[1], dict) else {}
                if not _is_widget(definition[0], inner):
                    continue
                # A revealed input can itself be a dynamic combo, so it is marked the same way and
                # expands under its own, longer, path when the walk reaches it.
                nested = "COMBO" in str(definition[0]).upper() and combo_options(
                    definition[0], inner
                ) is None
                revealed.append(
                    (f"{prefix}.{name}", {**inner, "_dynamic": True} if nested else inner)
                )
        return revealed
    return []


def _map_positionally(
    values: list[Any], ordered: list[tuple[str, dict[str, Any]]], control: set[str]
) -> tuple[dict[str, Any], int]:
    """Lay a positional ``widgets_values`` array over an ordered list of widgets.

    Returns the mapping and how many of the values it accounted for — which is what tells two candidate
    orderings apart. A ``control_after_generate`` widget occupies a slot of its own without being an
    input, so it is stepped over.

    A **dynamic combo** expands as it is read: its own slot holds the chosen mode, and that mode's widgets
    follow immediately, so they are spliced into the walk the moment the mode is known. Skipping over them
    instead would leave ComfyUI asking for a `temperature` nobody sent, and would shift every widget after
    them by however many slots the mode happened to have.
    """
    mapped: dict[str, Any] = {}
    queue = list(ordered)
    index = 0

    while queue and index < len(values):
        name, options = queue.pop(0)
        value = values[index]
        index += 1
        mapped[name] = value

        if options.get("_dynamic"):
            queue = _revealed_by(options, value, name) + queue
        elif name in control and index < len(values):
            index += 1

    return mapped, index


def _apply_widget_values(
    node: dict[str, Any], schema: dict[str, Any], inputs: dict[str, Any], result: ConversionResult
) -> None:
    """Recover a node's widget values from the positional array the frontend serialised.

    The order that array follows is the **schema's**, and that matters more than it sounds: a node's own
    ``inputs`` list holds only the widgets somebody converted into link sockets, which is usually one of
    them and sometimes none. Reading the order from there — as this used to — mapped a KSampler's seven
    values onto its one converted widget and dropped steps, cfg, sampler and scheduler on the floor, so
    ComfyUI refused the prompt for missing required inputs.

    The node's list is still worth trying, because a dynamic combo can expand into entries the schema does
    not mention. So both orders are laid over the values and the one that accounts for all of them wins.
    """
    values = node.get("widgets_values")
    if values is None:
        return

    # Newer frontends sometimes serialise a dict keyed by widget name, which needs no positional guessing.
    if isinstance(values, dict):
        inputs.update(values)
        return

    ordered = _widget_names(schema)
    control = {name for name, options in ordered if options.get("control_after_generate")}

    by_schema, used_schema = _map_positionally(values, ordered, control)
    own = widget_names(node)
    by_node, used_node = (
        _map_positionally(values, [(name, {}) for name in own], control) if own else ({}, 0)
    )

    if used_schema >= len(values):
        chosen, consumed = by_schema, used_schema
    elif used_node >= len(values):
        chosen, consumed = by_node, used_node
    else:
        chosen, consumed = (
            (by_schema, used_schema) if used_schema >= used_node else (by_node, used_node)
        )

    inputs.update(chosen)
    if consumed < len(values):
        result.warnings.append(
            f"Node {node.get('id')} ({node.get('type')}) had {len(values) - consumed} unmapped "
            "widget value(s); check its parameters."
        )
