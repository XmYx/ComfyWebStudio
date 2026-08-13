"""Writing down what a stage actually did.

Thin over :class:`ProjectStore`, and its whole job is the caps. A transcript that can grow without bound
is a transcript that eventually costs someone their disk, and one that stores a megabyte of base64 reply
is one nobody can open. Both are bounded here rather than hoped about.

Recording never fails a stage. If the disk is full or the directory has gone, that is worth a log line and
nothing more — the same posture ``ProjectStore.save`` takes towards its version log.
"""

from __future__ import annotations

import logging

from ..core.pipeline import StageRun

logger = logging.getLogger(__name__)

#: Per text field. Long enough for any real prompt, short enough that a runaway reply cannot fill a file.
TEXT_CAP = 8_192
#: The parsed answer, serialised. Higher, because a written storyboard legitimately is a few kilobytes.
PAYLOAD_CAP = 32_768


def clamp(record: StageRun) -> StageRun:
    """Cut anything oversized down, and say that it was cut."""
    for field in ("system", "prompt", "reply"):
        text = getattr(record, field)
        if len(text) > TEXT_CAP:
            setattr(record, field, text[:TEXT_CAP] + "\n… (truncated)")
            record.truncated = True

    if record.payload is not None:
        import json

        try:
            encoded = json.dumps(record.payload)
        except (TypeError, ValueError):
            record.payload = {"_": "this answer could not be stored"}
            record.truncated = True
        else:
            if len(encoded) > PAYLOAD_CAP:
                record.payload = {"_truncated": encoded[:PAYLOAD_CAP]}
                record.truncated = True
    return record


def record(store, project_id: str, stage_run: StageRun) -> None:
    """Persist one stage run, and prune the oldest away."""
    try:
        store.save_stage_run(project_id, clamp(stage_run))
    except Exception as exc:  # noqa: BLE001 - a transcript is never worth failing the work over
        logger.warning("Could not record stage run %s: %s", stage_run.id, exc)
        return
    try:
        store.prune_stage_runs(project_id, stage_run.board_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not prune stage runs: %s", exc)


def preview(stage_run: StageRun, *, chars: int = 300) -> dict:
    """Enough of a record for a list, without shipping every prompt to the browser at once."""
    return {
        **stage_run.model_dump(mode="json", exclude={"system", "prompt", "reply", "payload"}),
        "prompt_preview": stage_run.prompt[:chars],
        "reply_preview": stage_run.reply[:chars],
    }
