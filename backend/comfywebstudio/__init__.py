"""ComfyWebStudio — shot-based orchestration for chained ComfyUI workflows."""

__version__ = "0.1.0"

#: Bumped whenever the framework <-> node pack contract changes. The pack reports its own value from
#: ``GET /webstudio/ping``; a mismatch is surfaced to the user rather than silently tolerated.
PROTOCOL_VERSION = 1
