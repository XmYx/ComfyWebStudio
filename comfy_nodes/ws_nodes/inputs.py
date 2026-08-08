"""Input nodes — the framework-settable entry points of a workflow.

Each node declares a named port. The framework discovers these by scanning the workflow JSON for our
``class_type``s, so ports and editable parameters appear the moment a workflow is imported, with no need to
execute anything. At run time the framework writes into the ``value`` / ``source`` widget of the API-format
prompt before submitting it.

Every node outputs a native ComfyUI type, so it wires straight into an existing graph.
"""

from __future__ import annotations

import hashlib
import os

from . import kinds
from .constants import CATEGORY_INPUT
from .paths import resolve_input_path

#: Widgets every input node carries so the framework can lay out a sensible form.
_META_INPUTS = {
    "label": ("STRING", {"default": "", "tooltip": "Human-readable name shown in ComfyWebStudio."}),
    "group": ("STRING", {"default": "", "tooltip": "Groups related parameters in the form."}),
    "order": ("INT", {"default": 0, "min": -999, "max": 999, "tooltip": "Sort order within the group."}),
}

_PORT_NAME = (
    "STRING",
    {"default": "", "tooltip": "Unique port name within this workflow. This is what the framework binds to."},
)


class _InputBase:
    CATEGORY = CATEGORY_INPUT
    FUNCTION = "resolve"


# -- scalar inputs -------------------------------------------------------------------------------------


class WSStringInput(_InputBase):
    DESCRIPTION = "Exposes a text parameter to ComfyWebStudio."
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("value",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "port_name": _PORT_NAME,
                "value": ("STRING", {"default": "", "multiline": True}),
            },
            "optional": dict(_META_INPUTS),
        }

    def resolve(self, port_name, value, **_meta):
        return (value,)


class WSIntInput(_InputBase):
    DESCRIPTION = "Exposes an integer parameter to ComfyWebStudio."
    RETURN_TYPES = ("INT",)
    RETURN_NAMES = ("value",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "port_name": _PORT_NAME,
                "value": ("INT", {"default": 0, "min": -(2**31), "max": 2**31 - 1}),
            },
            "optional": {
                # Bounds are metadata: the framework renders a slider from them. ComfyUI itself does not
                # enforce them, which is deliberate — the framework is the authority on its own UI.
                "min": ("INT", {"default": 0, "min": -(2**31), "max": 2**31 - 1}),
                "max": ("INT", {"default": 100, "min": -(2**31), "max": 2**31 - 1}),
                "step": ("INT", {"default": 1, "min": 1, "max": 2**16}),
                **_META_INPUTS,
            },
        }

    def resolve(self, port_name, value, **_meta):
        return (int(value),)


class WSFloatInput(_InputBase):
    DESCRIPTION = "Exposes a float parameter to ComfyWebStudio."
    RETURN_TYPES = ("FLOAT",)
    RETURN_NAMES = ("value",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "port_name": _PORT_NAME,
                "value": ("FLOAT", {"default": 0.0, "min": -1e9, "max": 1e9, "step": 0.01}),
            },
            "optional": {
                "min": ("FLOAT", {"default": 0.0, "min": -1e9, "max": 1e9, "step": 0.01}),
                "max": ("FLOAT", {"default": 1.0, "min": -1e9, "max": 1e9, "step": 0.01}),
                "step": ("FLOAT", {"default": 0.01, "min": 1e-6, "max": 1e6, "step": 0.001}),
                **_META_INPUTS,
            },
        }

    def resolve(self, port_name, value, **_meta):
        return (float(value),)


class WSBooleanInput(_InputBase):
    DESCRIPTION = "Exposes a toggle to ComfyWebStudio."
    RETURN_TYPES = ("BOOLEAN",)
    RETURN_NAMES = ("value",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"port_name": _PORT_NAME, "value": ("BOOLEAN", {"default": False})},
            "optional": dict(_META_INPUTS),
        }

    def resolve(self, port_name, value, **_meta):
        return (bool(value),)


class WSSeedInput(_InputBase):
    DESCRIPTION = "Exposes a seed to ComfyWebStudio, which can randomise or increment it per run."
    RETURN_TYPES = ("INT",)
    RETURN_NAMES = ("seed",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "port_name": _PORT_NAME,
                "value": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
            },
            "optional": dict(_META_INPUTS),
        }

    def resolve(self, port_name, value, **_meta):
        return (int(value),)


# -- media inputs --------------------------------------------------------------------------------------


class _MediaInputBase(_InputBase):
    """Loads a file the framework staged for us.

    ``source`` is written by the framework when a link supplies this port. Left empty, the node fails with a
    message naming the port rather than a generic loader error — the common case is a user pressing Queue in
    ComfyUI on a workflow whose inputs only the framework fills in.
    """

    KIND = ""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "port_name": _PORT_NAME,
                "source": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "Filled in by ComfyWebStudio. A name in input/, a relative path, or an "
                        "absolute path when ComfyUI shares a filesystem with the framework.",
                    },
                ),
            },
            "optional": dict(_META_INPUTS),
        }

    def resolve(self, port_name, source, **_meta):
        if not str(source or "").strip():
            raise ValueError(
                f"WebStudio input port {port_name or '<unnamed>'!r} has no source. "
                "Run this workflow from ComfyWebStudio, or type a filename to test it standalone."
            )
        path = resolve_input_path(source)
        return (kinds.get(self.KIND).load(path, {}),)

    @classmethod
    def IS_CHANGED(cls, port_name, source, **_meta):
        """Hash the staged file so ComfyUI re-executes when a chained upstream produces new content.

        Without this, two runs whose ``source`` string happens to match would reuse a stale cached tensor.
        """
        try:
            path = resolve_input_path(source)
        except (ValueError, FileNotFoundError):
            return str(source)
        digest = hashlib.sha256()
        digest.update(str(os.path.getmtime(path)).encode())
        with open(path, "rb") as handle:
            digest.update(handle.read(1 << 20))  # first MiB is plenty to detect a changed render
        return digest.hexdigest()


class WSImageInput(_MediaInputBase):
    DESCRIPTION = "Receives an image from ComfyWebStudio (chained from another workflow, or uploaded)."
    KIND = "image"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)


class WSMaskInput(_MediaInputBase):
    DESCRIPTION = "Receives a mask from ComfyWebStudio."
    KIND = "mask"
    RETURN_TYPES = ("MASK",)
    RETURN_NAMES = ("mask",)


class WSLatentInput(_MediaInputBase):
    DESCRIPTION = "Receives a latent from ComfyWebStudio."
    KIND = "latent"
    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("latent",)


class WSAudioInput(_MediaInputBase):
    DESCRIPTION = "Receives audio from ComfyWebStudio."
    KIND = "audio"
    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("audio",)


class WSVideoInput(_MediaInputBase):
    DESCRIPTION = "Receives a video from ComfyWebStudio."
    KIND = "video"
    RETURN_TYPES = ("VIDEO",)
    RETURN_NAMES = ("video",)


class WSFileInput(_MediaInputBase):
    DESCRIPTION = "Receives an arbitrary file path from ComfyWebStudio."
    KIND = "file"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("path",)


#: ``class name -> port kind``. The framework reads this through ``GET /webstudio/manifest``.
INPUT_NODES: dict[str, type] = {
    "WSStringInput": WSStringInput,
    "WSIntInput": WSIntInput,
    "WSFloatInput": WSFloatInput,
    "WSBooleanInput": WSBooleanInput,
    "WSSeedInput": WSSeedInput,
    "WSImageInput": WSImageInput,
    "WSMaskInput": WSMaskInput,
    "WSLatentInput": WSLatentInput,
    "WSAudioInput": WSAudioInput,
    "WSVideoInput": WSVideoInput,
    "WSFileInput": WSFileInput,
}

INPUT_KINDS: dict[str, str] = {
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

DISPLAY_NAMES: dict[str, str] = {
    "WSStringInput": "WS Text Input",
    "WSIntInput": "WS Integer Input",
    "WSFloatInput": "WS Float Input",
    "WSBooleanInput": "WS Toggle Input",
    "WSSeedInput": "WS Seed Input",
    "WSImageInput": "WS Image Input",
    "WSMaskInput": "WS Mask Input",
    "WSLatentInput": "WS Latent Input",
    "WSAudioInput": "WS Audio Input",
    "WSVideoInput": "WS Video Input",
    "WSFileInput": "WS File Input",
}
