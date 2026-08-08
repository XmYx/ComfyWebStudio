"""Port-kind handlers.

Importing this package registers every handler. ``video`` is imported defensively because it is the only
kind that depends on ``comfy_api``; on a ComfyUI too old to provide it the other kinds must still work.
"""

from __future__ import annotations

import logging

from .base import KindHandler, SavedFile, all_handlers, get, has, manifest, register, saved

logger = logging.getLogger(__name__)

from . import audio, image, latent, text  # noqa: E402,F401  (import order is the registration order)

try:
    from . import video  # noqa: F401
except Exception as exc:  # noqa: BLE001
    logger.warning("WebStudio: VIDEO port kind unavailable on this ComfyUI (%s)", exc)

__all__ = [
    "KindHandler",
    "SavedFile",
    "all_handlers",
    "get",
    "has",
    "manifest",
    "register",
    "saved",
]
