"""Output nodes — the reason this pack exists.

ComfyUI's IMAGE / MASK / AUDIO / LATENT values are in-memory tensors that are discarded once a prompt
finishes, so nothing downstream of a *different* workflow can ever see them. These nodes persist each value
to a deterministic location under ``output/webstudio/<run_key>/<port_name>/`` and report a structured
payload the framework picks up from ``GET /history/{prompt_id}``.

The return value keeps ComfyUI's own preview keys populated alongside ours, so these nodes still behave like
normal output nodes when someone queues the workflow directly in the ComfyUI UI.
"""

from __future__ import annotations

import logging

from . import kinds
from .constants import CATEGORY_OUTPUT, PROTOCOL_VERSION, UI_KEY
from .paths import output_location

logger = logging.getLogger(__name__)

_PORT_NAME = (
    "STRING",
    {"default": "", "tooltip": "Unique port name within this workflow. The framework chains on this."},
)

_RUN_KEY = (
    "STRING",
    {
        "default": "",
        "tooltip": "Set by ComfyWebStudio to '<run_id>/<step_id>'. Leave empty when running by hand; "
        "output then lands in a timestamped manual/ folder.",
    },
)

#: ComfyUI's own preview key per kind, so native previews keep working. ``None`` means no native preview.
_NATIVE_UI_KEY = {
    "image": "images",
    "mask": "images",
    "audio": "audio",
    "video": "images",
    "latent": "latents",
}


class _OutputBase:
    CATEGORY = CATEGORY_OUTPUT
    FUNCTION = "store"
    OUTPUT_NODE = True
    RETURN_TYPES = ()

    KIND = ""
    SOCKET = "IMAGE"
    SOCKET_NAME = "value"

    @classmethod
    def INPUT_TYPES(cls):
        handler = kinds.get(cls.KIND)
        required = {
            cls.SOCKET_NAME: (cls.SOCKET,),
            "port_name": _PORT_NAME,
        }
        if len(handler.formats) > 1:
            required["format"] = (list(handler.formats), {"default": handler.default_format})
        required["run_key"] = _RUN_KEY
        return {"required": required, "optional": cls.extra_inputs()}

    @classmethod
    def extra_inputs(cls) -> dict:
        return {}

    def store(self, port_name, run_key="", format=None, **extra):  # noqa: A002 - ComfyUI widget is named `format`
        value = extra.pop(self.SOCKET_NAME, None)
        handler = kinds.get(self.KIND)
        fmt = format or handler.default_format
        name = str(port_name or "").strip() or "output"

        directory, subfolder = output_location(run_key, name)
        files, meta = handler.save(value, directory, subfolder, name, fmt, extra)

        entry = {
            "protocol": PROTOCOL_VERSION,
            "port_name": name,
            "kind": self.KIND,
            "run_key": str(run_key or ""),
            "files": files,
            "meta": meta,
        }

        ui: dict[str, object] = {UI_KEY: [entry]}
        native_key = _NATIVE_UI_KEY.get(self.KIND)
        if native_key:
            ui[native_key] = files
            if self.KIND in {"image", "mask"} and len(files) > 1:
                ui["animated"] = (False,)
        if self.KIND in {"string", "int", "float", "boolean"}:
            ui["text"] = [str(meta.get("value", ""))]

        return {"ui": ui}


# -- concrete nodes ------------------------------------------------------------------------------------


class WSImageOutput(_OutputBase):
    DESCRIPTION = "Saves images so ComfyWebStudio can preview them and chain them into the next workflow."
    KIND = "image"
    SOCKET = "IMAGE"
    SOCKET_NAME = "image"

    @classmethod
    def extra_inputs(cls):
        return {
            "quality": ("INT", {"default": 92, "min": 1, "max": 100}),
            "lossless": ("BOOLEAN", {"default": False}),
        }


class WSMaskOutput(_OutputBase):
    DESCRIPTION = "Saves a mask for ComfyWebStudio."
    KIND = "mask"
    SOCKET = "MASK"
    SOCKET_NAME = "mask"


class WSLatentOutput(_OutputBase):
    DESCRIPTION = "Saves a latent for ComfyWebStudio, in ComfyUI's own .latent format."
    KIND = "latent"
    SOCKET = "LATENT"
    SOCKET_NAME = "latent"


class WSAudioOutput(_OutputBase):
    DESCRIPTION = "Saves audio for ComfyWebStudio."
    KIND = "audio"
    SOCKET = "AUDIO"
    SOCKET_NAME = "audio"

    @classmethod
    def extra_inputs(cls):
        return {"quality": (["64k", "96k", "128k", "192k", "320k", "V0"], {"default": "192k"})}


class WSVideoOutput(_OutputBase):
    DESCRIPTION = "Saves a video for ComfyWebStudio."
    KIND = "video"
    SOCKET = "VIDEO"
    SOCKET_NAME = "video"

    @classmethod
    def extra_inputs(cls):
        return {"codec": (["auto", "h264", "vp9", "av1"], {"default": "auto"})}


class WSTextOutput(_OutputBase):
    DESCRIPTION = "Publishes a string from this workflow to ComfyWebStudio."
    KIND = "string"
    SOCKET = "STRING"
    SOCKET_NAME = "text"

    @classmethod
    def INPUT_TYPES(cls):
        base = super().INPUT_TYPES()
        base["required"][cls.SOCKET_NAME] = ("STRING", {"forceInput": True})
        return base


class WSNumberOutput(_OutputBase):
    DESCRIPTION = "Publishes a number from this workflow to ComfyWebStudio."
    KIND = "float"
    SOCKET = "FLOAT"
    SOCKET_NAME = "number"

    @classmethod
    def INPUT_TYPES(cls):
        base = super().INPUT_TYPES()
        base["required"][cls.SOCKET_NAME] = ("FLOAT", {"forceInput": True})
        return base


class WSFileOutput(_OutputBase):
    DESCRIPTION = "Copies an arbitrary file produced by this workflow into ComfyWebStudio's artifact store."
    KIND = "file"
    SOCKET = "STRING"
    SOCKET_NAME = "path"

    @classmethod
    def INPUT_TYPES(cls):
        base = super().INPUT_TYPES()
        base["required"][cls.SOCKET_NAME] = ("STRING", {"forceInput": True})
        return base


OUTPUT_NODES: dict[str, type] = {
    "WSImageOutput": WSImageOutput,
    "WSMaskOutput": WSMaskOutput,
    "WSLatentOutput": WSLatentOutput,
    "WSAudioOutput": WSAudioOutput,
    "WSVideoOutput": WSVideoOutput,
    "WSTextOutput": WSTextOutput,
    "WSNumberOutput": WSNumberOutput,
    "WSFileOutput": WSFileOutput,
}

OUTPUT_KINDS: dict[str, str] = {name: cls.KIND for name, cls in OUTPUT_NODES.items()}

DISPLAY_NAMES: dict[str, str] = {
    "WSImageOutput": "WS Image Output",
    "WSMaskOutput": "WS Mask Output",
    "WSLatentOutput": "WS Latent Output",
    "WSAudioOutput": "WS Audio Output",
    "WSVideoOutput": "WS Video Output",
    "WSTextOutput": "WS Text Output",
    "WSNumberOutput": "WS Number Output",
    "WSFileOutput": "WS File Output",
}
