"""Normalized ComfyUI event protocol.

ComfyUI's websocket carries a dozen message types with inconsistent shapes. Everything above this module
sees the typed events defined here instead, so a ComfyUI change is a one-file fix.

Event names and payloads verified against ComfyUI 0.24.1:
``execution.py:425,487,527,565,685,698,727,755,805``, ``main.py:339,405``, ``server.py:275,1233``,
``comfy_execution/progress.py:183``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EventType = Literal[
    "status",
    "execution_start",
    "execution_cached",
    "executing",
    "progress",
    "progress_state",
    "executed",
    "execution_success",
    "execution_error",
    "execution_interrupted",
    "prompt_done",
    "preview",
    "unknown",
]


@dataclass(slots=True)
class ComfyEvent:
    """One normalized event. ``prompt_id`` is how callers demultiplex concurrent work."""

    type: EventType
    prompt_id: str | None = None
    node_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    raw_type: str = ""

    @property
    def is_terminal(self) -> bool:
        """True for the events that end a prompt's lifecycle.

        ``prompt_done`` is the one to wait on for results: ``execution_success`` fires at
        ``execution.py:805``, *before* history is written at ``main.py:332``, so acting on it races.
        """
        return self.type in {"prompt_done", "execution_error", "execution_interrupted"}

    @property
    def is_failure(self) -> bool:
        return self.type in {"execution_error", "execution_interrupted"}


@dataclass(slots=True)
class NodeProgress:
    node_id: str
    value: float
    max: float
    state: str = "running"

    @property
    def fraction(self) -> float:
        return min(1.0, self.value / self.max) if self.max else 0.0


def parse_event(message: dict[str, Any]) -> ComfyEvent:
    """Turn one raw websocket JSON frame into a :class:`ComfyEvent`."""
    raw_type = str(message.get("type", ""))
    data = message.get("data") or {}
    prompt_id = data.get("prompt_id")

    if raw_type == "status":
        info = (data.get("status") or {}).get("exec_info") or {}
        return ComfyEvent(
            "status",
            data={"queue_remaining": info.get("queue_remaining", 0), "sid": data.get("sid")},
            raw_type=raw_type,
        )

    if raw_type == "executing":
        node = data.get("node")
        if node is None:
            # ComfyUI sends this after task_done, i.e. once history exists (main.py:339). It is the only
            # reliable "this prompt is finished and its results are readable" signal.
            return ComfyEvent("prompt_done", prompt_id=prompt_id, raw_type=raw_type)
        return ComfyEvent(
            "executing",
            prompt_id=prompt_id,
            node_id=str(node),
            data={"display_node": data.get("display_node")},
            raw_type=raw_type,
        )

    if raw_type == "progress":
        return ComfyEvent(
            "progress",
            prompt_id=prompt_id,
            node_id=str(data["node"]) if data.get("node") is not None else None,
            data={"value": data.get("value", 0), "max": data.get("max", 0)},
            raw_type=raw_type,
        )

    if raw_type == "progress_state":
        nodes = {
            str(node_id): NodeProgress(
                node_id=str(node_id),
                value=float(info.get("value", 0) or 0),
                max=float(info.get("max", 0) or 0),
                state=str(info.get("state", "running")),
            )
            for node_id, info in (data.get("nodes") or {}).items()
        }
        return ComfyEvent("progress_state", prompt_id=prompt_id, data={"nodes": nodes}, raw_type=raw_type)

    if raw_type == "executed":
        return ComfyEvent(
            "executed",
            prompt_id=prompt_id,
            node_id=str(data.get("node")) if data.get("node") is not None else None,
            data={"output": data.get("output") or {}, "display_node": data.get("display_node")},
            raw_type=raw_type,
        )

    if raw_type == "execution_cached":
        return ComfyEvent(
            "execution_cached",
            prompt_id=prompt_id,
            data={"nodes": [str(n) for n in (data.get("nodes") or [])]},
            raw_type=raw_type,
        )

    if raw_type == "execution_error":
        return ComfyEvent(
            "execution_error",
            prompt_id=prompt_id,
            node_id=str(data.get("node_id")) if data.get("node_id") is not None else None,
            data={
                "node_type": data.get("node_type"),
                "message": data.get("exception_message") or "execution failed",
                "exception_type": data.get("exception_type"),
                "traceback": data.get("traceback") or [],
            },
            raw_type=raw_type,
        )

    if raw_type == "execution_interrupted":
        return ComfyEvent(
            "execution_interrupted",
            prompt_id=prompt_id,
            node_id=str(data.get("node_id")) if data.get("node_id") is not None else None,
            data={"node_type": data.get("node_type"), "message": "interrupted"},
            raw_type=raw_type,
        )

    if raw_type in {"execution_start", "execution_success"}:
        return ComfyEvent(raw_type, prompt_id=prompt_id, data=dict(data), raw_type=raw_type)  # type: ignore[arg-type]

    return ComfyEvent("unknown", prompt_id=prompt_id, data=dict(data), raw_type=raw_type)


# -- binary frames -------------------------------------------------------------------------------------

#: ``protocol.py:BinaryEventTypes`` in ComfyUI.
BINARY_PREVIEW_IMAGE = 1
BINARY_UNENCODED_PREVIEW_IMAGE = 2
BINARY_TEXT = 3
BINARY_PREVIEW_IMAGE_WITH_METADATA = 4

_IMAGE_MIME = {1: "image/jpeg", 2: "image/png"}


def parse_binary(frame: bytes) -> ComfyEvent | None:
    """Decode a binary websocket frame into a preview event, or None when we do not handle it.

    Layouts from ``server.py:1147-1206``:
      * type 1: ``[u32 type][u32 image_format][bytes]``
      * type 3: ``[u32 type][u32 node_id_len][node_id][text]``
      * type 4: ``[u32 type][u32 metadata_len][metadata json][bytes]``
    """
    if len(frame) < 8:
        return None

    event_type = int.from_bytes(frame[0:4], "big")

    if event_type == BINARY_PREVIEW_IMAGE:
        image_format = int.from_bytes(frame[4:8], "big")
        return ComfyEvent(
            "preview",
            data={"image": frame[8:], "mime": _IMAGE_MIME.get(image_format, "image/png")},
            raw_type="binary:1",
        )

    if event_type == BINARY_PREVIEW_IMAGE_WITH_METADATA:
        import json

        length = int.from_bytes(frame[4:8], "big")
        try:
            meta = json.loads(frame[8 : 8 + length].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return ComfyEvent(
            "preview",
            prompt_id=meta.get("prompt_id"),
            node_id=str(meta.get("node_id")) if meta.get("node_id") is not None else None,
            data={"image": frame[8 + length :], "mime": meta.get("image_type", "image/png")},
            raw_type="binary:4",
        )

    if event_type == BINARY_TEXT:
        length = int.from_bytes(frame[4:8], "big")
        try:
            node_id = frame[8 : 8 + length].decode("utf-8")
            text = frame[8 + length :].decode("utf-8")
        except UnicodeDecodeError:
            return None
        return ComfyEvent("progress", node_id=node_id, data={"text": text}, raw_type="binary:3")

    return None
