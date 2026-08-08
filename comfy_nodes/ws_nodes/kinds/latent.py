"""LATENT persistence.

Written in exactly the ``.latent`` safetensors layout ComfyUI's own ``SaveLatent``/``LoadLatent`` use
(``nodes.py:478-566``) — ``latent_tensor`` plus the ``latent_format_version_0`` marker — so our artifacts
can be dropped straight into a stock LoadLatent node and vice versa.
"""

from __future__ import annotations

from typing import Any

import torch

from .base import allocate, register, saved


class LatentHandler:
    kind = "latent"
    formats = ("latent",)
    default_format = "latent"

    def save(self, value, directory, subfolder, port_name, fmt, opts):
        if value is None or "samples" not in value:
            raise ValueError("WSLatentOutput received no latent")

        samples: torch.Tensor = value["samples"]
        path, filename, _ = allocate(directory, port_name, "latent")

        payload = {
            "latent_tensor": samples.contiguous().cpu(),
            # Marker telling LoadLatent not to apply the legacy 1/0.18215 scale factor.
            "latent_format_version_0": torch.tensor([]),
        }

        import comfy.utils  # imported lazily so the module imports outside a ComfyUI process

        comfy.utils.save_torch_file(payload, path, metadata=None)

        meta = {
            "count": 1,
            "shape": list(samples.shape),
            "batch": int(samples.shape[0]),
            "format": "latent",
        }
        return [saved(filename, subfolder)], meta

    def load(self, path: str, opts: dict[str, Any]):
        import safetensors.torch

        data = safetensors.torch.load_file(path, device="cpu")
        multiplier = 1.0 if "latent_format_version_0" in data else 1.0 / 0.18215
        return {"samples": data["latent_tensor"].float() * multiplier}


register(LatentHandler())
