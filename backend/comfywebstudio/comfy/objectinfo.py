"""Cached ``/object_info`` access.

``/object_info`` is a large response (every installed node's full schema) and it only changes when ComfyUI
restarts or nodes are reloaded, so it is cached per backend with a TTL. It is needed to type a *raw widget
binding* — exposing an arbitrary node's widget as an editable parameter for workflows the user does not want
to modify with our own input nodes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from .http import ComfyError, ComfyHttpClient

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 300.0


@dataclass(slots=True)
class WidgetSpec:
    """One editable widget on a node, normalised into the shape the framework's forms understand."""

    name: str
    kind: str
    default: Any = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: list[str] | None = None
    multiline: bool = False
    tooltip: str = ""
    required: bool = True


#: ComfyUI socket type -> our port kind. Anything else is not a widget we can edit as a scalar.
_TYPE_TO_KIND = {
    "STRING": "string",
    "INT": "int",
    "FLOAT": "float",
    "BOOLEAN": "boolean",
}


class ObjectInfoCache:
    """Per-backend cache of node schemas."""

    def __init__(self, http: ComfyHttpClient, *, ttl_s: float = DEFAULT_TTL_S):
        self._http = http
        self._ttl = ttl_s
        self._data: dict[str, Any] | None = None
        self._fetched_at = 0.0

    async def all(self, *, refresh: bool = False) -> dict[str, Any]:
        if refresh or self._data is None or (time.monotonic() - self._fetched_at) > self._ttl:
            self._data = await self._http.object_info()
            self._fetched_at = time.monotonic()
        return self._data or {}

    async def node(self, class_type: str) -> dict[str, Any] | None:
        data = await self.all()
        if class_type in data:
            return data[class_type]
        # A node installed since the last fetch: try the single-node endpoint before giving up.
        try:
            single = await self._http.object_info_for(class_type)
        except ComfyError:
            return None
        entry = (single or {}).get(class_type)
        if entry and self._data is not None:
            self._data[class_type] = entry
        return entry

    async def widgets(self, class_type: str) -> list[WidgetSpec]:
        """Editable widgets of a node — the candidates for raw widget binding."""
        entry = await self.node(class_type)
        if not entry:
            return []

        specs: list[WidgetSpec] = []
        inputs = entry.get("input") or {}
        for section, required in (("required", True), ("optional", False)):
            for name, definition in (inputs.get(section) or {}).items():
                spec = _widget_from_definition(name, definition, required=required)
                if spec is not None:
                    specs.append(spec)
        return specs

    async def missing_classes(self, class_types: set[str]) -> set[str]:
        """Which of these node types this ComfyUI does not have installed."""
        data = await self.all()
        return {c for c in class_types if c not in data}

    def invalidate(self) -> None:
        self._data = None


def _widget_from_definition(name: str, definition: Any, *, required: bool) -> WidgetSpec | None:
    """Normalise one ``/object_info`` input entry.

    Entries look like ``[type, options]`` or ``[type]``, where ``type`` is a string for scalars and a list
    of strings for combos.
    """
    if not isinstance(definition, list | tuple) or not definition:
        return None

    raw_type = definition[0]
    options: dict[str, Any] = definition[1] if len(definition) > 1 and isinstance(definition[1], dict) else {}

    # A connected socket, not a widget: nothing to type into.
    if options.get("forceInput"):
        return None

    if isinstance(raw_type, list):
        choices = [str(c) for c in raw_type]
        return WidgetSpec(
            name=name,
            kind="choice",
            default=options.get("default", choices[0] if choices else None),
            choices=choices,
            tooltip=str(options.get("tooltip", "")),
            required=required,
        )

    kind = _TYPE_TO_KIND.get(str(raw_type))
    if kind is None:
        return None  # IMAGE, MODEL, CLIP ... are links, not editable values

    return WidgetSpec(
        name=name,
        kind=kind,
        default=options.get("default"),
        min=_as_float(options.get("min")),
        max=_as_float(options.get("max")),
        step=_as_float(options.get("step")),
        multiline=bool(options.get("multiline")),
        tooltip=str(options.get("tooltip", "")),
        required=required,
    )


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
