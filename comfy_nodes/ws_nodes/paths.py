"""Where artifacts go, and how we keep callers from escaping the output directory.

``run_key`` arrives from the framework as ``"<run_id>/<step_id>"``. It is attacker-controllable in the sense
that it comes over HTTP, so every component is sanitised before it touches the filesystem.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import folder_paths

from .constants import OUTPUT_ROOT

#: Anything outside this set is replaced with ``_``. Deliberately strict: no dots, so ``..`` cannot survive.
_UNSAFE = re.compile(r"[^A-Za-z0-9_\-]")

#: Cap each path component so a pathological name cannot blow past filesystem limits.
_MAX_COMPONENT = 64


def sanitize_component(value: Any, fallback: str = "unnamed") -> str:
    """Reduce one path component to a safe token."""
    text = str(value or "").strip()
    cleaned = _UNSAFE.sub("_", text)[:_MAX_COMPONENT].strip("_")
    return cleaned or fallback


def sanitize_run_key(run_key: Any) -> str:
    """Normalise ``run_key`` to ``a/b`` form, generating one when the framework did not supply it.

    A node run by hand in the ComfyUI UI has no run key; it still needs somewhere to write, so we fall back
    to a timestamped ``manual/`` bucket rather than failing.
    """
    parts = [sanitize_component(p, "") for p in str(run_key or "").split("/")]
    parts = [p for p in parts if p]
    if not parts:
        return f"manual/{time.strftime('%Y%m%d-%H%M%S')}"
    return "/".join(parts[:4])


def output_location(run_key: Any, port_name: Any) -> tuple[str, str]:
    """Return ``(absolute_dir, comfy_subfolder)`` for one port's artifacts, creating the directory.

    ``comfy_subfolder`` is what goes into the ``{"filename", "subfolder", "type"}`` triples the ComfyUI
    frontend and our framework both use to fetch bytes back via ``GET /view``.
    """
    subfolder = "/".join([OUTPUT_ROOT, sanitize_run_key(run_key), sanitize_component(port_name, "port")])
    base = os.path.realpath(folder_paths.get_output_directory())
    full = os.path.realpath(os.path.join(base, *subfolder.split("/")))

    # Belt and braces: even with sanitising, never write outside the output directory.
    if os.path.commonpath([base, full]) != base:
        raise ValueError(f"Refusing to write outside the output directory: {subfolder}")

    os.makedirs(full, exist_ok=True)
    return full, subfolder


def resolve_input_path(source: str) -> str:
    """Resolve an input reference to an absolute path.

    Accepts, in order of preference:
      * a ComfyUI annotated name (``"cat.png [input]"``) or a plain name inside ``input/``,
      * a path relative to ``input/`` (``"webstudio/run/img_00001_.png"``),
      * an absolute path — only meaningful when the framework shares a filesystem with ComfyUI.
    """
    text = str(source or "").strip()
    if not text:
        raise ValueError("WebStudio input: empty source")

    if os.path.isabs(text):
        if not os.path.isfile(text):
            raise FileNotFoundError(f"WebStudio input: no such file: {text}")
        return text

    try:
        candidate = folder_paths.get_annotated_filepath(text)
    except Exception:  # noqa: BLE001 - older ComfyUI raises assorted types here
        candidate = None
    if candidate and os.path.isfile(candidate):
        return candidate

    for directory in (
        folder_paths.get_input_directory(),
        folder_paths.get_output_directory(),
        folder_paths.get_temp_directory(),
    ):
        guess = os.path.join(directory, *text.split("/"))
        if os.path.isfile(guess):
            return guess

    raise FileNotFoundError(f"WebStudio input: could not resolve {text!r}")


def next_index(directory: str, stem: str, suffix: str) -> int:
    """Next free counter for ``<stem>_00001_<suffix>`` inside ``directory``.

    Reruns of the same step write into the same directory; numbering rather than overwriting keeps a batch
    of images from clobbering each other and keeps prior results inspectable.
    """
    pattern = re.compile(rf"^{re.escape(stem)}_(\d+)_{re.escape(suffix)}$")
    highest = 0
    try:
        for name in os.listdir(directory):
            match = pattern.match(name)
            if match:
                highest = max(highest, int(match.group(1)))
    except OSError:
        return 1
    return highest + 1
