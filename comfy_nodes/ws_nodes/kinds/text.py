"""Scalar and opaque-file kinds.

Scalars (string / int / float / boolean) do not strictly need a file — the framework reads their values
straight out of the ``ui`` payload. We write one anyway so a long generated prompt or caption is inspectable
in ``output/`` and can be referenced by the timeline's text tracks like any other artifact.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any

from .base import allocate, register, saved


class _ScalarHandler:
    """Shared behaviour for the four JSON-scalar kinds."""

    formats = ("txt", "json")
    default_format = "txt"

    def __init__(self, kind: str, cast):
        self.kind = kind
        self._cast = cast

    def save(self, value, directory, subfolder, port_name, fmt, opts):
        cast = self._cast(value)
        path, filename, _ = allocate(directory, port_name, fmt)
        text = json.dumps(cast) if fmt == "json" else str(cast)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        # `value` travels inline in the metadata; that is what downstream steps actually consume.
        return [saved(filename, subfolder)], {"count": 1, "value": cast, "format": fmt}

    def load(self, path: str, opts: dict[str, Any]):
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
        if path.endswith(".json"):
            return self._cast(json.loads(raw))
        return self._cast(raw)


def _to_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class FileHandler:
    """An opaque file passed through untouched — the escape hatch for kinds we do not model."""

    kind = "file"
    formats = ("bin",)
    default_format = "bin"

    def save(self, value, directory, subfolder, port_name, fmt, opts):
        source = str(value or "").strip()
        if not source or not os.path.isfile(source):
            raise ValueError(f"WSFileOutput: not a readable file: {source!r}")
        extension = (os.path.splitext(source)[1] or ".bin").lstrip(".")
        path, filename, _ = allocate(directory, port_name, extension)
        shutil.copy2(source, path)
        return [saved(filename, subfolder)], {
            "count": 1,
            "size": os.path.getsize(path),
            "format": extension,
            "original_name": os.path.basename(source),
        }

    def load(self, path: str, opts: dict[str, Any]) -> str:
        return path


register(_ScalarHandler("string", str))
register(_ScalarHandler("int", int))
register(_ScalarHandler("float", float))
register(_ScalarHandler("boolean", _to_bool))
register(FileHandler())
