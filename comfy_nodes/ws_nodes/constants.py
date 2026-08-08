"""Shared constants for the ComfyWebStudio node pack."""

from __future__ import annotations

#: Bumped whenever the framework <-> node pack contract changes. Reported by ``GET /webstudio/ping``.
PROTOCOL_VERSION = 1
PACK_VERSION = "0.1.0"

CATEGORY_INPUT = "WebStudio/inputs"
CATEGORY_OUTPUT = "WebStudio/outputs"

#: Root subfolder inside ComfyUI's ``output/`` where every framework artifact is written.
OUTPUT_ROOT = "webstudio"

#: Key our structured payload occupies inside a node's ``{"ui": {...}}`` return value.
UI_KEY = "webstudio"

#: Every port kind the framework understands, and the ComfyUI socket type it maps to.
KIND_TO_COMFY_TYPE: dict[str, str] = {
    "image": "IMAGE",
    "mask": "MASK",
    "video": "VIDEO",
    "audio": "AUDIO",
    "latent": "LATENT",
    "string": "STRING",
    "int": "INT",
    "float": "FLOAT",
    "boolean": "BOOLEAN",
    "file": "STRING",
}

#: Kinds whose values are tensors/objects that must be persisted to disk to survive past execution.
MEDIA_KINDS = frozenset({"image", "mask", "video", "audio", "latent", "file"})

#: Kinds that are plain JSON scalars and can be carried inline.
SCALAR_KINDS = frozenset({"string", "int", "float", "boolean"})
