"""ComfyWebStudio node pack internals.

Registration is explicit: every class is listed by hand rather than discovered by reflection, so a class
imported from another pack can never be accidentally registered under our namespace.
"""

from __future__ import annotations

import logging

from . import kinds
from .constants import PACK_VERSION, PROTOCOL_VERSION
from .inputs import DISPLAY_NAMES as INPUT_DISPLAY_NAMES
from .inputs import INPUT_KINDS, INPUT_NODES
from .outputs import DISPLAY_NAMES as OUTPUT_DISPLAY_NAMES
from .outputs import OUTPUT_KINDS, OUTPUT_NODES

logger = logging.getLogger(__name__)


def _available(class_kinds: dict[str, str]) -> set[str]:
    """Class names whose port kind actually registered on this ComfyUI.

    VIDEO depends on ``comfy_api``; on a build without it those nodes are skipped rather than registered
    broken, so the rest of the pack still loads.
    """
    return {name for name, kind in class_kinds.items() if kinds.has(kind)}


_usable_inputs = _available(INPUT_KINDS)
_usable_outputs = _available(OUTPUT_KINDS)

_skipped = (set(INPUT_KINDS) | set(OUTPUT_KINDS)) - (_usable_inputs | _usable_outputs)
if _skipped:
    logger.warning("WebStudio: skipping nodes with unavailable kinds: %s", ", ".join(sorted(_skipped)))

NODE_CLASS_MAPPINGS: dict[str, type] = {
    **{name: cls for name, cls in INPUT_NODES.items() if name in _usable_inputs},
    **{name: cls for name, cls in OUTPUT_NODES.items() if name in _usable_outputs},
}

NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {
    name: display
    for name, display in {**INPUT_DISPLAY_NAMES, **OUTPUT_DISPLAY_NAMES}.items()
    if name in NODE_CLASS_MAPPINGS
}

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "PACK_VERSION",
    "PROTOCOL_VERSION",
    "INPUT_KINDS",
    "OUTPUT_KINDS",
]
