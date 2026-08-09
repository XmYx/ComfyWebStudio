"""Subgraphs: flattening them, and mapping their promoted inputs to editable parameters.

A ComfyUI subgraph is a reusable group stored once in ``definitions.subgraphs`` and instantiated by nodes
whose ``type`` is the subgraph's UUID. Its ``inputs`` are *promoted* slots — the knobs whoever built it
chose to expose (``width``, ``text``, ``unet_name`` …). Those are exactly the parameters worth surfacing.

Two things happen here:

* **Flattening** — the recursive expansion into plain nodes, so a subgraph workflow can execute at all.
  Node ids follow ComfyUI's own execution-id convention, ``<instance>:<inner>``, nesting for deeper
  subgraphs (``98:50:22``). Matching it matters: a workflow synced back through the bridge is flattened by
  ComfyUI itself, and the parameter map has to point at the same ids either way.

* **Promotion mapping** — each promoted slot resolved to every inner input it drives. One slot can feed
  several (``width`` typically drives both the latent size and the scheduler), so a promoted parameter
  carries a *list* of targets rather than one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: ComfyUI joins nested subgraph ids with a colon (`getLocatorIdFromNodeData` in the frontend).
ID_SEPARATOR = ":"

#: Virtual endpoints inside a subgraph definition, standing in for its own inputs and outputs.
DEFAULT_INPUT_NODE_ID = -10
DEFAULT_OUTPUT_NODE_ID = -20

MAX_DEPTH = 8


@dataclass(slots=True)
class ParamTargetRef:
    node_id: str
    input_name: str


@dataclass(slots=True)
class PromotedParam:
    """One promoted subgraph input, resolved to the inner inputs it actually drives."""

    key: str
    name: str
    type: str
    subgraph_name: str
    instance_path: str
    targets: list[ParamTargetRef] = field(default_factory=list)
    default: Any = None
    #: Set when the instance node overrides the inner default.
    overridden: bool = False


@dataclass(slots=True)
class _Scope:
    """One level of graph: the root document, or one subgraph instance."""

    path: tuple[str, ...]
    nodes: dict[str, dict[str, Any]]
    links: dict[int, tuple[str, int, str, int]]  # id -> (origin_id, origin_slot, target_id, target_slot)
    definition: dict[str, Any] | None = None  # None for the root graph

    @property
    def input_node_id(self) -> str:
        node = (self.definition or {}).get("inputNode") or {}
        return str(node.get("id", DEFAULT_INPUT_NODE_ID))

    @property
    def output_node_id(self) -> str:
        node = (self.definition or {}).get("outputNode") or {}
        return str(node.get("id", DEFAULT_OUTPUT_NODE_ID))

    def exec_id(self, node_id: Any) -> str:
        return ID_SEPARATOR.join([*self.path, str(node_id)])


def subgraph_definitions(ui_graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``uuid -> definition`` for every subgraph the document defines."""
    definitions = (ui_graph.get("definitions") or {}).get("subgraphs") or []
    return {str(sg["id"]): sg for sg in definitions if sg.get("id")}


def has_subgraphs(ui_graph: dict[str, Any]) -> bool:
    return bool(subgraph_definitions(ui_graph))


def _normalise_links(raw: Any) -> dict[int, tuple[str, int, str, int]]:
    """Links come as arrays at the root and as objects inside subgraph definitions."""
    links: dict[int, tuple[str, int, str, int]] = {}
    for link in raw or []:
        try:
            if isinstance(link, dict):
                links[int(link["id"])] = (
                    str(link["origin_id"]), int(link["origin_slot"]),
                    str(link["target_id"]), int(link["target_slot"]),
                )
            elif isinstance(link, list | tuple) and len(link) >= 5:
                links[int(link[0])] = (str(link[1]), int(link[2]), str(link[3]), int(link[4]))
        except (KeyError, TypeError, ValueError):
            logger.debug("Skipping malformed link: %r", link)
    return links


def _scope_for(
    path: tuple[str, ...], graph: dict[str, Any], definition: dict[str, Any] | None
) -> _Scope:
    return _Scope(
        path=path,
        nodes={str(n["id"]): n for n in graph.get("nodes") or [] if "id" in n},
        links=_normalise_links(graph.get("links")),
        definition=definition,
    )


# -- promoted parameters ---------------------------------------------------------------------------------


def collect_promoted_params(ui_graph: dict[str, Any]) -> list[PromotedParam]:
    """Every promoted subgraph input in the document, with the inner inputs it drives.

    Works on the UI document alone, so it applies equally to a workflow we converted ourselves and one
    ComfyUI flattened for us — both end up with the same ``instance:inner`` ids.
    """
    definitions = subgraph_definitions(ui_graph)
    if not definitions:
        return []

    params: list[PromotedParam] = []
    root = _scope_for((), ui_graph, None)
    _walk_instances(root, definitions, params, depth=0)

    # Two subgraphs can promote the same name; keys must stay unique so the UI can address them.
    seen: dict[str, int] = {}
    for param in params:
        if param.key in seen:
            seen[param.key] += 1
            param.key = f"{param.key}#{seen[param.key]}"
        else:
            seen[param.key] = 1
    return params


def _walk_instances(
    scope: _Scope, definitions: dict[str, dict[str, Any]], out: list[PromotedParam], depth: int
) -> None:
    if depth > MAX_DEPTH:
        logger.warning("Stopping subgraph traversal at depth %d", depth)
        return

    for node_id, node in scope.nodes.items():
        definition = definitions.get(str(node.get("type")))
        if definition is None:
            continue

        child_path = (*scope.path, node_id)
        child = _scope_for(child_path, definition, definition)
        out.extend(_params_for_instance(node, definition, child))
        _walk_instances(child, definitions, out, depth + 1)


def _params_for_instance(
    instance: dict[str, Any], definition: dict[str, Any], child: _Scope
) -> list[PromotedParam]:
    subgraph_name = str(definition.get("name") or "Subgraph")
    instance_path = ID_SEPARATOR.join(child.path)
    instance_slots = instance.get("inputs") or []
    overrides = _instance_widget_values(instance)

    params: list[PromotedParam] = []
    for index, slot in enumerate(definition.get("inputs") or []):
        name = str(slot.get("name") or f"input_{index}")
        # The instance can relabel a slot; that label is what the user sees in ComfyUI.
        label = name
        if index < len(instance_slots):
            label = str(instance_slots[index].get("label") or instance_slots[index].get("name") or name)

        # A slot the parent wired something into is not a knob — it is a connection.
        if index < len(instance_slots) and instance_slots[index].get("link") is not None:
            continue

        targets = _targets_for_slot(slot, child)
        if not targets:
            continue

        default, from_instance = _default_for_slot(label, name, overrides, targets, child)
        params.append(
            PromotedParam(
                key=label,
                name=label,
                type=str(slot.get("type") or "STRING"),
                subgraph_name=subgraph_name,
                instance_path=instance_path,
                targets=targets,
                default=default,
                overridden=from_instance,
            )
        )
    return params


def _targets_for_slot(slot: dict[str, Any], child: _Scope) -> list[ParamTargetRef]:
    """Follow a promoted slot's links to the inner node inputs it feeds."""
    targets: list[ParamTargetRef] = []
    for link_id in slot.get("linkIds") or []:
        link = child.links.get(int(link_id))
        if link is None:
            continue
        _origin, _origin_slot, target_id, target_slot = link

        node = child.nodes.get(target_id)
        if node is None:
            continue

        slots = node.get("inputs") or []
        if target_slot >= len(slots):
            continue
        input_name = slots[target_slot].get("name")
        # Only widget-backed inputs can take a value; a pure link socket cannot.
        widget = slots[target_slot].get("widget") or {}
        if widget.get("name"):
            input_name = widget["name"]
        if not input_name:
            continue

        targets.append(ParamTargetRef(node_id=child.exec_id(target_id), input_name=str(input_name)))
    return targets


def _instance_widget_values(instance: dict[str, Any]) -> dict[str, Any]:
    """Values the instance node itself supplies, keyed by promoted slot name.

    ``widgets_values`` is positional over the instance's widget-bearing inputs, the same convention the
    rest of LiteGraph uses.
    """
    values = instance.get("widgets_values")
    if isinstance(values, dict):
        return dict(values)
    if not isinstance(values, list):
        return {}

    widget_slots = [s for s in instance.get("inputs") or [] if (s.get("widget") or {}).get("name")]
    return {
        str(slot.get("label") or slot.get("name")): values[index]
        for index, slot in enumerate(widget_slots)
        if index < len(values)
    }


def _default_for_slot(
    label: str, name: str, overrides: dict[str, Any], targets: list[ParamTargetRef], child: _Scope
) -> tuple[Any, bool]:
    """The value this promoted input currently has.

    The instance wins if it supplies one; otherwise the inner node keeps its own widget value, which is
    what ComfyUI shows when a promoted slot has never been touched.
    """
    for key in (label, name):
        if key in overrides:
            return overrides[key], True

    # Fall back to the first inner target's own widget value.
    for target in targets:
        inner_id = target.node_id.rsplit(ID_SEPARATOR, 1)[-1]
        node = child.nodes.get(inner_id)
        if node is None:
            continue
        value = _widget_value(node, target.input_name)
        if value is not None:
            return value, False
    return None, False


def _widget_value(node: dict[str, Any], input_name: str) -> Any:
    """Read one widget's value out of a node's positional ``widgets_values``.

    Widget order follows the node's own ``inputs`` array, which for a saved LiteGraph node already lists
    widget-backed inputs in declaration order — so this does not need ``/object_info``.
    """
    values = node.get("widgets_values")
    if isinstance(values, dict):
        return values.get(input_name)
    if not isinstance(values, list):
        return None

    widget_slots = [s for s in node.get("inputs") or [] if (s.get("widget") or {}).get("name")]
    for index, slot in enumerate(widget_slots):
        if (slot.get("widget") or {}).get("name") == input_name or slot.get("name") == input_name:
            if index < len(values):
                return values[index]
            return None
    return None


# -- flattening ------------------------------------------------------------------------------------------


@dataclass(slots=True)
class FlattenResult:
    """Real nodes keyed by execution id, plus a resolver for their inputs."""

    nodes: dict[str, tuple[_Scope, dict[str, Any]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class SubgraphFlattener:
    """Expands subgraph instances into plain nodes, resolving links across every boundary."""

    #: Editor-only node types that never reach the API prompt.
    VIRTUAL = frozenset({"Reroute", "PrimitiveNode", "Note", "MarkdownNote", "Reroute (rgthree)"})
    MODE_MUTED = 2
    MODE_BYPASS = 4

    def __init__(self, ui_graph: dict[str, Any]):
        self.definitions = subgraph_definitions(ui_graph)
        self.root = _scope_for((), ui_graph, None)
        self.warnings: list[str] = []
        #: ``scope path -> (parent scope, instance node)``, so a subgraph can resolve upwards.
        self._parents: dict[tuple[str, ...], tuple[_Scope, dict[str, Any]]] = {}
        self._instance_values: dict[tuple[str, ...], dict[str, Any]] = {}

    def flatten(self) -> FlattenResult:
        result = FlattenResult()
        self._collect(self.root, result, depth=0)
        result.warnings = self.warnings
        return result

    def _collect(self, scope: _Scope, result: FlattenResult, depth: int) -> None:
        if depth > MAX_DEPTH:
            self.warnings.append("Subgraph nesting is deeper than this converter supports.")
            return

        for node_id, node in scope.nodes.items():
            node_type = str(node.get("type") or "")
            definition = self.definitions.get(node_type)

            if definition is not None:
                child_path = (*scope.path, node_id)
                child = _scope_for(child_path, definition, definition)
                self._parents[child_path] = (scope, node)
                self._instance_values[child_path] = _instance_widget_values(node)
                self._collect(child, result, depth + 1)
                continue

            if node_type in self.VIRTUAL or node.get("mode") in {self.MODE_MUTED, self.MODE_BYPASS}:
                continue

            result.nodes[scope.exec_id(node_id)] = (scope, node)

    # -- link resolution ---------------------------------------------------------------------------

    def resolve_input(self, scope: _Scope, link_id: Any, depth: int = 0) -> tuple[str, int] | None:
        """Follow a link back to the real node that produces it, crossing subgraph boundaries."""
        if link_id is None or depth > 32:
            return None

        link = scope.links.get(int(link_id))
        if link is None:
            return None
        origin_id, origin_slot, _target_id, _target_slot = link

        # Reading the subgraph's own input slot: continue in the parent graph.
        if scope.definition is not None and origin_id == scope.input_node_id:
            return self._resolve_from_parent(scope, origin_slot, depth)

        node = scope.nodes.get(origin_id)
        if node is None:
            return None

        node_type = str(node.get("type") or "")
        definition = self.definitions.get(node_type)

        # Reading a subgraph instance's output: descend into it.
        if definition is not None:
            return self._resolve_from_child(scope, origin_id, definition, origin_slot, depth)

        if node_type in self.VIRTUAL or node.get("mode") in {self.MODE_BYPASS, self.MODE_MUTED}:
            passthrough = self._passthrough(node, origin_slot)
            if passthrough is None:
                if node.get("mode") != self.MODE_MUTED:
                    self.warnings.append(
                        f"Could not trace through {node_type or 'node'} {origin_id}; a link was dropped."
                    )
                return None
            return self.resolve_input(scope, passthrough, depth + 1)

        return (scope.exec_id(origin_id), origin_slot)

    def _resolve_from_parent(self, scope: _Scope, slot_index: int, depth: int) -> tuple[str, int] | None:
        parent = self._parents.get(scope.path)
        if parent is None:
            return None
        parent_scope, instance = parent
        slots = instance.get("inputs") or []
        if slot_index >= len(slots):
            return None
        # No incoming link means the value is a widget, not a connection.
        return self.resolve_input(parent_scope, slots[slot_index].get("link"), depth + 1)

    def _resolve_from_child(
        self, scope: _Scope, instance_id: str, definition: dict[str, Any], slot_index: int, depth: int
    ) -> tuple[str, int] | None:
        child_path = (*scope.path, instance_id)
        child = _scope_for(child_path, definition, definition)
        self._parents.setdefault(child_path, (scope, scope.nodes[instance_id]))

        outputs = definition.get("outputs") or []
        if slot_index >= len(outputs):
            return None

        # Find the link that feeds this output slot from inside.
        for link_id in outputs[slot_index].get("linkIds") or []:
            link = child.links.get(int(link_id))
            if link is None:
                continue
            _origin, _origin_slot, target_id, target_slot = link
            if target_id == child.output_node_id and target_slot == slot_index:
                return self.resolve_input(child, link_id, depth + 1)

        # Some documents omit target_slot bookkeeping; fall back to the first link on the slot.
        for link_id in outputs[slot_index].get("linkIds") or []:
            if int(link_id) in child.links:
                return self.resolve_input(child, link_id, depth + 1)
        return None

    def _passthrough(self, node: dict[str, Any], out_slot: int) -> int | None:
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

    def instance_value(self, scope: _Scope, input_name: str) -> Any:
        """A value the instance node supplies for one of its subgraph's promoted inputs."""
        return self._instance_values.get(scope.path, {}).get(input_name)
