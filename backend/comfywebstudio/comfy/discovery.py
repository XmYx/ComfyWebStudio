"""Turn a workflow into ports and parameters.

Discovery reads the API-format prompt and looks for our node pack's classes. It runs entirely on the JSON,
so a freshly imported workflow shows its ports and its editable fields immediately — nothing has to execute
and no GPU is involved.

Three sources feed the same :class:`ParamSpec` shape, so the UI renders them identically:

* ``ws_node``   — a ``WS*Input`` node the user added. First-class, named, stable across graph edits.
* ``raw_widget`` — any widget on any stock node, exposed by ``(node_id, input_name)``. The escape hatch for
  existing workflows the user would rather not modify.
* ``subgraph``  — an input a subgraph promotes. Those are the knobs whoever built the subgraph chose to
  expose, so they are worth surfacing without the user having to open it up.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..core.models import ParamSpec, ParamTarget, PortKind, PortSpec
from .objectinfo import ObjectInfoCache, WidgetSpec
from .subgraphs import collect_promoted_params

logger = logging.getLogger(__name__)

#: Fallback mapping used when a backend's manifest is unavailable (offline import, backend down). Kept in
#: sync with ``comfy_nodes/ws_nodes/inputs.py``; the live manifest wins whenever we can reach a backend.
DEFAULT_INPUT_KINDS: dict[str, PortKind] = {
    "WSStringInput": "string",
    "WSIntInput": "int",
    "WSFloatInput": "float",
    "WSBooleanInput": "boolean",
    "WSSeedInput": "int",
    "WSImageInput": "image",
    "WSMaskInput": "mask",
    "WSLatentInput": "latent",
    "WSAudioInput": "audio",
    "WSVideoInput": "video",
    "WSFileInput": "file",
}

DEFAULT_OUTPUT_KINDS: dict[str, PortKind] = {
    "WSImageOutput": "image",
    "WSMaskOutput": "mask",
    "WSLatentOutput": "latent",
    "WSAudioOutput": "audio",
    "WSVideoOutput": "video",
    "WSTextOutput": "string",
    "WSNumberOutput": "float",
    "WSFileOutput": "file",
}

#: Kinds whose value is edited inline rather than staged as a file.
SCALAR_KINDS: frozenset[str] = frozenset({"string", "int", "float", "boolean"})

#: Which widget on an input node carries its value.
MEDIA_VALUE_INPUT = "source"
SCALAR_VALUE_INPUT = "value"

SEED_CLASSES = frozenset({"WSSeedInput"})


@dataclass(slots=True)
class NodeKindMap:
    """Which node classes are ours, and what kind each one carries."""

    inputs: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_INPUT_KINDS))
    outputs: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_OUTPUT_KINDS))
    value_inputs: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any] | None) -> NodeKindMap:
        """Build from a live ``/webstudio/manifest``, falling back to the built-in defaults."""
        result = cls()
        if not manifest:
            return result
        for entry in manifest.get("input_nodes") or []:
            class_type = entry.get("class_type")
            if class_type:
                result.inputs[class_type] = entry.get("kind", "string")
                result.value_inputs[class_type] = entry.get("value_input", SCALAR_VALUE_INPUT)
        for entry in manifest.get("output_nodes") or []:
            class_type = entry.get("class_type")
            if class_type:
                result.outputs[class_type] = entry.get("kind", "image")
        return result

    def value_input_for(self, class_type: str) -> str:
        if class_type in self.value_inputs:
            return self.value_inputs[class_type]
        kind = self.inputs.get(class_type, "string")
        return SCALAR_VALUE_INPUT if kind in SCALAR_KINDS else MEDIA_VALUE_INPUT


@dataclass(slots=True)
class DiscoveryResult:
    ports: list[PortSpec] = field(default_factory=list)
    params: list[ParamSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Every class_type in the graph, for the "is this installed" check.
    node_classes: set[str] = field(default_factory=set)


def discover(api_prompt: dict[str, Any], *, kind_map: NodeKindMap | None = None) -> DiscoveryResult:
    """Find every ComfyWebStudio port and parameter in an API-format prompt.

    Every ``WS*Input`` becomes an input port so it can be chained. Scalars additionally become editable
    parameters, which is what makes an unlinked text input something you can just type into — and a linked
    one something the upstream step overwrites.
    """
    kinds = kind_map or NodeKindMap()
    result = DiscoveryResult()

    used_input_keys: dict[str, str] = {}
    used_output_keys: dict[str, str] = {}

    for node_id, node in sorted(api_prompt.items(), key=lambda kv: _sort_key(kv[0])):
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type", ""))
        if not class_type:
            continue
        result.node_classes.add(class_type)
        inputs = node.get("inputs") or {}

        if class_type in kinds.inputs:
            _add_input(result, kinds, node_id, class_type, inputs, used_input_keys)
        elif class_type in kinds.outputs:
            _add_output(result, kinds, node_id, class_type, inputs, used_output_keys)

    result.ports.sort(key=lambda p: (p.direction, p.group, p.order, p.key))
    result.params.sort(key=lambda p: (p.group, p.order, p.key))
    return result


def _sort_key(node_id: str) -> tuple[int, str]:
    """Order numerically when node ids are numbers, which is the common case."""
    try:
        return (0, f"{int(node_id):012d}")
    except (TypeError, ValueError):
        return (1, str(node_id))


def _add_input(
    result: DiscoveryResult,
    kinds: NodeKindMap,
    node_id: str,
    class_type: str,
    inputs: dict[str, Any],
    used: dict[str, str],
) -> None:
    kind = kinds.inputs[class_type]
    key = _unique_key(
        str(inputs.get("port_name") or "").strip(),
        node_id=node_id,
        used=used,
        result=result,
        what="input port",
    )

    label = str(inputs.get("label") or "").strip()
    group = str(inputs.get("group") or "").strip()
    order = _as_int(inputs.get("order"), 0)

    result.ports.append(
        PortSpec(
            key=key,
            direction="in",
            kind=kind,  # type: ignore[arg-type]
            node_id=node_id,
            label=label,
            group=group,
            order=order,
            # An input that carries an editable value can run unconnected, using that value.
            optional=kind in SCALAR_KINDS,
            meta={"class_type": class_type, "value_input": kinds.value_input_for(class_type)},
        )
    )

    if kind not in SCALAR_KINDS:
        return

    value_input = kinds.value_input_for(class_type)
    result.params.append(
        ParamSpec(
            key=key,
            kind=kind,  # type: ignore[arg-type]
            label=label or key,
            default=inputs.get(value_input),
            min=_as_float(inputs.get("min")),
            max=_as_float(inputs.get("max")),
            step=_as_float(inputs.get("step")),
            multiline=kind == "string",
            group=group,
            order=order,
            node_id=node_id,
            input_name=value_input,
            source="ws_node",
            is_seed=class_type in SEED_CLASSES,
        )
    )


def _add_output(
    result: DiscoveryResult,
    kinds: NodeKindMap,
    node_id: str,
    class_type: str,
    inputs: dict[str, Any],
    used: dict[str, str],
) -> None:
    key = _unique_key(
        str(inputs.get("port_name") or "").strip(),
        node_id=node_id,
        used=used,
        result=result,
        what="output port",
    )
    result.ports.append(
        PortSpec(
            key=key,
            direction="out",
            kind=kinds.outputs[class_type],  # type: ignore[arg-type]
            node_id=node_id,
            label=key,
            meta={"class_type": class_type, "format": inputs.get("format")},
        )
    )


def _unique_key(
    raw: str, *, node_id: str, used: dict[str, str], result: DiscoveryResult, what: str
) -> str:
    """Resolve the port name, warning rather than failing on an empty or duplicated one.

    A duplicate would make two ports indistinguishable and silently chain the wrong one, so it is renamed
    and reported — the user can then fix it in ComfyUI.
    """
    key = raw
    if not key:
        key = f"node_{node_id}"
        result.warnings.append(
            f"An {what} on node {node_id} has no name; using {key!r}. "
            "Give it a port name in ComfyUI so links survive graph edits."
        )
    if key in used:
        original = key
        key = f"{key}_{node_id}"
        result.warnings.append(
            f"Duplicate {what} name {original!r} on nodes {used[original]} and {node_id}; "
            f"the second is exposed as {key!r}."
        )
    used[key] = node_id
    return key


# -- raw widget binding --------------------------------------------------------------------------------


# -- subgraph parameters ---------------------------------------------------------------------------------

#: Prefix keeping promoted subgraph parameters from colliding with port names or raw widget bindings.
SUBGRAPH_PREFIX = "$"

#: ComfyUI socket types on a promoted slot -> our parameter kinds.
_SLOT_TYPE_TO_KIND = {
    "INT": "int",
    "FLOAT": "float",
    "STRING": "string",
    "BOOLEAN": "boolean",
    "COMBO": "choice",
}


def subgraph_param_key(instance_path: str, name: str) -> str:
    """Stable key for a promoted input. Survives a re-sync as long as the instance and slot persist."""
    return f"{SUBGRAPH_PREFIX}{instance_path}.{name}"


async def subgraph_params(
    ui_graph: dict[str, Any],
    api_prompt: dict[str, Any],
    object_info: ObjectInfoCache | None = None,
) -> list[ParamSpec]:
    """Editable parameters for every input a subgraph promotes.

    A subgraph's promoted inputs are the knobs whoever built it chose to expose, so they are exactly what
    should be editable from the framework. Each one is resolved to *all* the inner inputs it drives —
    ``width`` typically sets both the latent size and the scheduler — and typed from the target node's
    schema so a COMBO becomes a real dropdown rather than a text box.
    """
    promoted = collect_promoted_params(ui_graph)
    if not promoted:
        return []

    specs: list[ParamSpec] = []
    for order, param in enumerate(promoted):
        targets = [
            ParamTarget(node_id=t.node_id, input_name=t.input_name)
            for t in param.targets
            if t.node_id in api_prompt
        ]
        if not targets:
            # Its inner node did not survive conversion (uninstalled type, muted, bypassed).
            logger.debug("Promoted input %s has no live targets; skipping", param.key)
            continue

        spec = ParamSpec(
            key=subgraph_param_key(param.instance_path, param.name),
            kind=_SLOT_TYPE_TO_KIND.get(param.type.upper(), "string"),  # type: ignore[arg-type]
            label=param.name,
            default=param.default,
            group=param.subgraph_name,
            order=order,
            node_id=targets[0].node_id,
            input_name=targets[0].input_name,
            targets=targets,
            source="subgraph",
            is_seed=param.name in {"seed", "noise_seed"},
        )
        await _type_from_schema(spec, api_prompt, object_info)
        specs.append(spec)

    return specs


async def _type_from_schema(
    spec: ParamSpec, api_prompt: dict[str, Any], object_info: ObjectInfoCache | None
) -> None:
    """Fill in choices and bounds from the target node's own definition.

    The promoted slot only carries a coarse type (``COMBO``); the real option list lives on the node it
    feeds, which is what makes a model picker usable rather than a free-text field.
    """
    if object_info is None:
        return
    node = api_prompt.get(spec.node_id)
    if not isinstance(node, dict):
        return

    try:
        widgets = await object_info.widgets(str(node.get("class_type", "")))
    except Exception as exc:  # noqa: BLE001 - an unreachable backend must not block discovery
        logger.debug("Could not type promoted input %s: %s", spec.key, exc)
        return

    widget = next((w for w in widgets if w.name == spec.input_name), None)
    if widget is None:
        return

    spec.kind = widget.kind  # type: ignore[assignment]
    spec.choices = widget.choices
    spec.min = widget.min
    spec.max = widget.max
    spec.step = widget.step
    spec.multiline = widget.multiline
    spec.tooltip = widget.tooltip
    if spec.default is None:
        spec.default = widget.default


def raw_param_key(node_id: str, input_name: str) -> str:
    """Stable key for a raw widget binding. Prefixed so it cannot collide with a named port."""
    return f"@{node_id}.{input_name}"


def build_raw_param(
    node_id: str,
    class_type: str,
    widget: WidgetSpec,
    *,
    current_value: Any = None,
    title: str | None = None,
    group: str = "",
    order: int = 0,
) -> ParamSpec:
    """Expose one widget of an arbitrary node as an editable parameter."""
    label = f"{title or class_type} · {widget.name}"
    return ParamSpec(
        key=raw_param_key(node_id, widget.name),
        kind=widget.kind,  # type: ignore[arg-type]
        label=label,
        default=current_value if current_value is not None else widget.default,
        min=widget.min,
        max=widget.max,
        step=widget.step,
        choices=widget.choices,
        multiline=widget.multiline,
        tooltip=widget.tooltip,
        group=group or class_type,
        order=order,
        node_id=node_id,
        input_name=widget.name,
        source="raw_widget",
        is_seed=widget.name in {"seed", "noise_seed"},
    )


async def bindable_widgets(
    api_prompt: dict[str, Any], object_info: ObjectInfoCache
) -> list[dict[str, Any]]:
    """Every widget in the graph that could be exposed as a parameter.

    Powers the "expose a parameter" picker: the user browses their existing nodes rather than having to
    rebuild the workflow around our input nodes.
    """
    candidates: list[dict[str, Any]] = []
    for node_id, node in sorted(api_prompt.items(), key=lambda kv: _sort_key(kv[0])):
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type", ""))
        if class_type in DEFAULT_INPUT_KINDS or class_type in DEFAULT_OUTPUT_KINDS:
            continue  # already first-class

        inputs = node.get("inputs") or {}
        title = ((node.get("_meta") or {}).get("title")) or class_type

        for widget in await object_info.widgets(class_type):
            value = inputs.get(widget.name)
            # A list value is a link ``[node_id, slot]``, not something the user can type into.
            if isinstance(value, list):
                continue
            candidates.append(
                {
                    "node_id": node_id,
                    "class_type": class_type,
                    "title": title,
                    "input_name": widget.name,
                    "kind": widget.kind,
                    "current": value if value is not None else widget.default,
                    "key": raw_param_key(node_id, widget.name),
                    "choices": widget.choices,
                    "min": widget.min,
                    "max": widget.max,
                    "step": widget.step,
                    "multiline": widget.multiline,
                }
            )
    return candidates


# -- misc ----------------------------------------------------------------------------------------------


def prompt_hash(api_prompt: dict[str, Any]) -> str:
    """Stable hash of a graph's structure, used to invalidate cached step results.

    Values injected per run (``run_key``, staged ``source`` paths) are excluded so that re-running an
    unchanged workflow with unchanged inputs is recognised as a cache hit.
    """
    scrubbed: dict[str, Any] = {}
    for node_id, node in api_prompt.items():
        if not isinstance(node, dict):
            continue
        inputs = {k: v for k, v in (node.get("inputs") or {}).items() if k not in {"run_key"}}
        scrubbed[str(node_id)] = {"class_type": node.get("class_type"), "inputs": inputs}
    payload = json.dumps(scrubbed, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


async def find_missing_nodes(
    node_classes: set[str], object_info: ObjectInfoCache
) -> list[str]:
    """Node classes the target ComfyUI does not have installed."""
    try:
        return sorted(await object_info.missing_classes(node_classes))
    except Exception as exc:  # noqa: BLE001 - an unreachable backend must not block import
        logger.debug("Could not check installed nodes: %s", exc)
        return []


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
