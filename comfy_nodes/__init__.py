"""comfyui-webstudio — the ComfyWebStudio companion node pack.

Provides typed input and output nodes that the ComfyWebStudio framework discovers, so several workflows can
be chained together: outputs are persisted to disk (ComfyUI's tensors do not survive a prompt) and inputs
are staged from whatever the previous step produced.
"""

from __future__ import annotations

import logging

from .ws_nodes import (  # noqa: F401
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    PACK_VERSION,
    PROTOCOL_VERSION,
)

logger = logging.getLogger(__name__)

#: Serves web/js/* at /extensions/comfyui-webstudio/ — the ComfyUI-side half of the editor bridge.
WEB_DIRECTORY = "./web"

try:
    from server import PromptServer  # provided by ComfyUI at runtime

    from .ws_nodes.routes import register_routes

    # getattr, not PromptServer.instance: ComfyUI 0.31 dropped the class-level default, so the attribute
    # only exists once a server has actually been constructed. Importing the pack without one — tests,
    # tooling, `comfy node` commands — must not blow up.
    server = getattr(PromptServer, "instance", None)
    if server is not None:
        register_routes(server)
        logger.info("WebStudio node pack %s (protocol %d) loaded", PACK_VERSION, PROTOCOL_VERSION)
except ImportError:
    # Imported outside a ComfyUI process (tests, tooling). Nodes still work; routes simply are not attached.
    logger.debug("WebStudio: PromptServer unavailable, skipping route registration")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
