"""Identifier and slug helpers.

Ids are short, URL-safe and prefixed by entity type so a stray id in a log or an error message is
immediately identifiable.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
import uuid

_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_UNSAFE_PATH = re.compile(r"[^A-Za-z0-9_\-]")


def new_id(prefix: str, length: int = 10) -> str:
    """A new id such as ``shot_k3f9x2a1qd``."""
    body = "".join(secrets.choice(_ALPHABET) for _ in range(length))
    return f"{prefix}_{body}"


def new_uuid() -> str:
    """A plain UUID4 string.

    Used where something outside the framework dictates the format — ComfyUI validates the caller-supplied
    ``prompt_id`` as a real UUID (it rejects anything else with "prompt_id must be a valid UUID"), so our
    own prefixed ids are not usable there.
    """
    return str(uuid.uuid4())


def slugify(value: str, *, fallback: str = "untitled", max_length: int = 64) -> str:
    """Filesystem- and URL-safe slug, with accents folded rather than dropped."""
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", ascii_only).strip("-")[:max_length].strip("-")
    return slug or fallback


def safe_component(value: object, fallback: str = "unnamed", max_length: int = 64) -> str:
    """One path component with everything risky replaced.

    Dots are stripped too, so ``..`` cannot survive — the same rule the node pack applies on its side.
    """
    cleaned = _UNSAFE_PATH.sub("_", str(value or ""))[:max_length].strip("_")
    return cleaned or fallback


def safe_relative_key(value: object, *, max_parts: int = 4) -> str:
    """Sanitise a ``a/b`` style key (``run_id/step_id``) for use as a relative directory path."""
    parts = [safe_component(p, "") for p in str(value or "").split("/")]
    parts = [p for p in parts if p]
    return "/".join(parts[:max_parts]) or "manual"
