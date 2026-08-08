"""IMAGE and MASK persistence.

IMAGE is ``[B, H, W, C]`` float 0-1 channels-last; MASK is ``[B, H, W]`` float 0-1. A batch is written as a
numbered sequence, which is also how an image-sequence port feeds the video renderer downstream.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import torch
from PIL import Image

from .base import allocate, register, saved

_IMAGE_FORMATS = ("png", "webp", "jpeg")


def _to_pil(frame: torch.Tensor) -> Image.Image:
    """One ``[H, W, C]`` or ``[H, W]`` float tensor -> PIL, clamped rather than wrapped."""
    array = frame.detach().cpu().float().numpy()
    array = np.clip(array * 255.0, 0, 255).astype(np.uint8)
    if array.ndim == 2:
        return Image.fromarray(array, mode="L")
    if array.shape[-1] == 1:
        return Image.fromarray(array[..., 0], mode="L")
    if array.shape[-1] == 4:
        return Image.fromarray(array, mode="RGBA")
    return Image.fromarray(array[..., :3], mode="RGB")


def _save_pil(img: Image.Image, path: str, fmt: str, opts: dict[str, Any]) -> None:
    quality = int(opts.get("quality", 92))
    if fmt == "png":
        img.save(path, format="PNG", compress_level=int(opts.get("compress_level", 4)))
    elif fmt == "webp":
        img.save(path, format="WEBP", quality=quality, lossless=bool(opts.get("lossless", False)))
    elif fmt == "jpeg":
        # JPEG has no alpha channel; flattening beats an opaque crash.
        img.convert("RGB").save(path, format="JPEG", quality=quality, subsampling=0)
    else:
        raise ValueError(f"Unsupported image format: {fmt}")


class ImageHandler:
    kind = "image"
    formats = _IMAGE_FORMATS
    default_format = "png"

    def save(self, value, directory, subfolder, port_name, fmt, opts):
        if value is None:
            raise ValueError("WSImageOutput received no image")
        batch = value if value.ndim == 4 else value.unsqueeze(0)
        files, width, height = [], 0, 0
        for frame in batch:
            img = _to_pil(frame)
            width, height = img.size
            path, filename, _ = allocate(directory, port_name, fmt)
            _save_pil(img, path, fmt, opts)
            files.append(saved(filename, subfolder))
        meta = {"count": len(files), "width": width, "height": height, "format": fmt}
        return files, meta

    def load(self, path, opts):
        img = Image.open(path)
        img = img.convert("RGBA") if opts.get("keep_alpha") else img.convert("RGB")
        array = np.asarray(img).astype(np.float32) / 255.0
        return torch.from_numpy(array).unsqueeze(0)


class MaskHandler:
    kind = "mask"
    formats = ("png",)
    default_format = "png"

    def save(self, value, directory, subfolder, port_name, fmt, opts):
        if value is None:
            raise ValueError("WSMaskOutput received no mask")
        batch = value if value.ndim == 3 else value.unsqueeze(0)
        files, width, height = [], 0, 0
        for frame in batch:
            img = _to_pil(frame)
            width, height = img.size
            path, filename, _ = allocate(directory, port_name, "png")
            _save_pil(img, path, "png", opts)
            files.append(saved(filename, subfolder))
        return files, {"count": len(files), "width": width, "height": height, "format": "png"}

    def load(self, path, opts):
        # Masks round-trip as luminance. Unlike LoadImage (which inverts an alpha channel) there is nothing
        # to invert here: we wrote the mask values directly, so we read them back directly.
        img = Image.open(path).convert("L")
        array = np.asarray(img).astype(np.float32) / 255.0
        return torch.from_numpy(array).unsqueeze(0)


register(ImageHandler())
register(MaskHandler())
