"""Where a stage's answer is allowed to go.

An output field names its destination as a string — ``frame.image_prompt``, ``board.fields.mood`` — and
this is what turns that string into a write. Two rules shape it.

**Only prose goes in.** Every destination here holds text a person reads or a workflow is given. The ids
that hold the project together — ``frame.asset_id``, ``frame.shot_id``, ``board.id`` — are not writable by
a prompt, and not because a model would abuse them, but because a model that hallucinated one would break
a frame's link to its picture in a way nobody would think to look for.

**A blank answer never clobbers.** Models return "" under a schema that demanded a string. Writing that
over a prompt someone spent ten minutes on is the single worst thing this could do, so it declines and
says why — which is what ``describe`` has always done, made general.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ValidationFailed
from ..core.pipeline import KEY, MAX_CUSTOM_FIELDS, WriteRecord
from ..core.storyboard import Storyboard, StoryboardCharacter, StoryboardFrame
from ..llm.storywriter import as_text

#: Frame fields a stage may write. Everything else on a frame is structure, not writing.
FRAME_FIELDS = {"title", "action", "camera", "image_prompt", "shot_prompt", "notes"}

#: Board fields a stage may write.
BOARD_FIELDS = {"name", "premise", "style", "aspect"}

#: Destinations that are not a plain field: they rebuild part of the board.
COLLECTIONS = {"board.frames", "board.characters"}


def destinations(*, scope: str) -> list[str]:
    """Everything a stage of this scope may write to — what the editor offers in its dropdown."""
    out = [f"board.{name}" for name in sorted(BOARD_FIELDS)]
    out += ["board.fields.*", "board.frames", "board.characters"]
    if scope == "frame":
        out = [f"frame.{name}" for name in sorted(FRAME_FIELDS)] + ["frame.fields.*"] + out
    return out


def check(target: str, *, scope: str) -> None:
    """Refuse a destination that cannot work, at edit time rather than at run time."""
    if not target:
        return
    if target in COLLECTIONS:
        return

    head, _, rest = target.partition(".")
    if head not in {"frame", "board"}:
        raise ValidationFailed(
            f"{target!r} is not somewhere a stage can write. Try one of: "
            f"{', '.join(destinations(scope=scope))}."
        )
    if head == "frame" and scope != "frame":
        raise ValidationFailed(
            f"{target!r} writes to a frame, but this stage runs once for the whole board. "
            "Set the stage to run per frame, or write somewhere on the board."
        )

    allowed = FRAME_FIELDS if head == "frame" else BOARD_FIELDS
    if rest in allowed:
        return

    prefix, _, key = rest.partition(".")
    if prefix == "fields":
        if not KEY.match(key):
            raise ValidationFailed(
                f"{key!r} will not work as a field name. Use lowercase letters, digits and underscores."
            )
        return

    raise ValidationFailed(
        f"{target!r} is not writable. Writable here: {', '.join(destinations(scope=scope))}."
    )


def write(
    project,
    board: Storyboard,
    frame: StoryboardFrame | None,
    target: str,
    value: Any,
    *,
    apply: bool = True,
) -> WriteRecord:
    """Put one answer where its field says it goes, and record what happened either way."""
    if not target:
        return WriteRecord(
            target="", frame_id=frame.id if frame else None,
            after=as_text(value), applied=False, reason="proposed only",
        )

    text = as_text(value)
    record = WriteRecord(target=target, frame_id=frame.id if frame else None, after=text)

    if target in COLLECTIONS:
        record.applied = False
        record.reason = "rebuilt by the stage itself"
        return record

    head, _, rest = target.partition(".")
    owner = frame if head == "frame" else board
    if owner is None:
        record.applied = False
        record.reason = "no frame to write to"
        return record

    prefix, _, key = rest.partition(".")
    if prefix == "fields":
        record.before = owner.fields.get(key, "")
        if not text:
            record.applied = False
            record.reason = "the model returned nothing; kept what was there"
            return record
        if key not in owner.fields and len(owner.fields) >= MAX_CUSTOM_FIELDS:
            record.applied = False
            record.reason = f"already holding {MAX_CUSTOM_FIELDS} custom fields"
            return record
        if apply:
            owner.fields[key] = text
        record.applied = apply
        return record

    allowed = FRAME_FIELDS if head == "frame" else BOARD_FIELDS
    if rest not in allowed:
        record.applied = False
        record.reason = f"{target!r} is not writable"
        return record

    record.before = getattr(owner, rest, "") or ""
    if not text:
        # The reason this rule exists at all: a schema can demand a string and still be handed "".
        record.applied = False
        record.reason = "the model returned nothing; kept what was there"
        return record
    if apply:
        setattr(owner, rest, text)
    record.applied = apply
    return record


def frames_from(payload: Any, board: Storyboard) -> list[StoryboardFrame]:
    """Turn a ``board.frames`` answer into frames, resolving character names back to ids."""
    if not isinstance(payload, list):
        raise ValidationFailed("The model returned no frames. Try again, or a larger model.")

    by_name = {c.name.strip().lower(): c.id for c in board.characters if c.name.strip()}
    frames: list[StoryboardFrame] = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            continue
        named = [
            by_name[str(name).strip().lower()]
            for name in (entry.get("characters") or [])
            if str(name).strip().lower() in by_name
        ]
        frames.append(
            StoryboardFrame(
                order=index,
                title=as_text(entry.get("title")) or f"Shot {index + 1}",
                action=as_text(entry.get("action")),
                camera=as_text(entry.get("camera")),
                image_prompt=as_text(entry.get("image_prompt")),
                shot_prompt=as_text(entry.get("shot_prompt")),
                character_ids=named,
            )
        )

    if not frames:
        raise ValidationFailed("The model's answer had no usable frames in it.")
    return frames


def characters_from(payload: Any) -> list[StoryboardCharacter]:
    """Turn a ``board.characters`` answer into characters, dropping any without a name."""
    return [
        StoryboardCharacter(
            name=as_text(entry.get("name")),
            description=as_text(entry.get("description")),
            appearance=as_text(entry.get("appearance")),
        )
        for entry in (payload or [])
        if isinstance(entry, dict) and as_text(entry.get("name"))
    ]
