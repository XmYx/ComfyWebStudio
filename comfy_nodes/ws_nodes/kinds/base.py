"""Port-kind handler protocol and registry.

Adding support for a new data type means writing one module in this package and calling :func:`register`.
Nothing else in the pack needs to change — the input/output nodes, the manifest route and the framework's
discovery all read from this registry.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from ..constants import KIND_TO_COMFY_TYPE
from ..paths import next_index

#: One saved file, in the triple ComfyUI uses everywhere for referring to media.
SavedFile = dict[str, str]


class KindHandler(Protocol):
    """How one port kind is persisted and reloaded."""

    kind: str
    formats: tuple[str, ...]
    default_format: str

    def save(
        self,
        value: Any,
        directory: str,
        subfolder: str,
        port_name: str,
        fmt: str,
        opts: dict[str, Any],
    ) -> tuple[list[SavedFile], dict[str, Any]]:
        """Persist ``value`` into ``directory``; return the saved files and metadata about them."""

    def load(self, path: str, opts: dict[str, Any]) -> Any:
        """Rebuild the in-memory ComfyUI value from a file previously written by :meth:`save`."""


_REGISTRY: dict[str, KindHandler] = {}


def register(handler: KindHandler) -> KindHandler:
    if handler.kind in _REGISTRY:
        raise ValueError(f"Duplicate kind handler: {handler.kind}")
    _REGISTRY[handler.kind] = handler
    return handler


def get(kind: str) -> KindHandler:
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise KeyError(f"Unknown WebStudio port kind: {kind!r}") from None


def has(kind: str) -> bool:
    return kind in _REGISTRY


def all_handlers() -> dict[str, KindHandler]:
    return dict(_REGISTRY)


def manifest() -> list[dict[str, Any]]:
    """Machine-readable description of every kind, served to the framework so discovery is version-driven."""
    return [
        {
            "kind": h.kind,
            "comfy_type": KIND_TO_COMFY_TYPE.get(h.kind, "STRING"),
            "formats": list(h.formats),
            "default_format": h.default_format,
        }
        for h in _REGISTRY.values()
    ]


def allocate(directory: str, port_name: str, extension: str) -> tuple[str, str, int]:
    """Reserve the next ``<port>_00001_.<ext>`` name in ``directory``.

    Returns ``(absolute_path, filename, counter)``. Reruns of a step share a directory, so numbering keeps
    earlier results intact and inspectable instead of silently overwriting them.
    """
    suffix = f".{extension.lstrip('.')}"
    counter = next_index(directory, port_name, suffix)
    filename = f"{port_name}_{counter:05}_{suffix}"
    return os.path.join(directory, filename), filename, counter


def saved(filename: str, subfolder: str) -> SavedFile:
    return {"filename": filename, "subfolder": subfolder, "type": "output"}
