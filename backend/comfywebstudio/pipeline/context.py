"""Everything a prompt template is allowed to know about, flattened into strings.

This is the other half of the renderer's safety argument. The renderer does no traversal at all; it looks
up exact keys. So this module is where the walking happens, once, over a fixed list of things — and a
token that is not built here simply does not exist, whatever it is named.

Flattening is also where the awkward bits of the old prompts get their home: the fallback chain behind
`frame.intent`, the 1500-character premise for the vision model, the pre-rendered character block. Those
were expressions embedded in f-strings; here they are named values a user can put in a template.
"""

from __future__ import annotations

from typing import Any

from ..core.storyboard import Storyboard, StoryboardCharacter, StoryboardFrame
from ..llm.storywriter import as_text

#: How much premise the vision model is given. It only needs enough to know what it is looking at, and the
#: rest is budget spent on a picture it can already see.
PREMISE_BRIEF = 1500


def character_block(board: Storyboard, only: list[str] | None = None) -> str:
    """The characters, in the shape the writing prompt has always wanted them."""
    chosen = [c for c in board.characters if only is None or c.id in only]
    return "\n".join(
        f"- {c.name}: {c.description or 'no description'}"
        + (f" Appearance: {c.appearance}" if c.appearance else "")
        for c in chosen
    )


def frame_block(frames: list[StoryboardFrame]) -> str:
    """The shots, as a numbered outline a model can read the story from.

    Enough to know what happens and how it is framed, and no more: handing over every image prompt as
    well would spend most of the budget on descriptions of pictures the model is not being asked to
    write, and push the shots it *is* being asked about out of the useful part of its attention.
    """
    lines = []
    for frame in frames:
        title = frame.title or f"Shot {frame.order + 1}"
        action = frame.action or frame.image_prompt or ""
        camera = f" ({frame.camera})" if frame.camera else ""
        lines.append(f"{frame.order + 1}. {title} — {action}{camera}".rstrip(" —"))
    return "\n".join(lines)


def build_context(
    project: Any,
    board: Storyboard,
    frame: StoryboardFrame | None = None,
    *,
    count: int | None = None,
    outputs: dict[str, dict[str, Any]] | None = None,
    previous: str | None = None,
    character: StoryboardCharacter | None = None,
    before: list[StoryboardFrame] | None = None,
    after: list[StoryboardFrame] | None = None,
    position: str = "",
) -> dict[str, str]:
    """The tokens available to a stage's templates.

    `outputs` carries what earlier stages in this pipeline run returned, keyed by stage id, reachable as
    ``{stage.<id>.<key>}``; `previous` names the stage whose outputs are also reachable as ``{prev.<key>}``.
    `character` is set when the work is about one person rather than one frame — drawing their reference.

    `before` and `after` are the shots either side of a point new ones are being written into, and
    `position` names that point in words. They are what lets a stage write shots that *join up*: told
    only how many to write, a model restates the premise from the top every time.
    """
    characters = character_block(board)
    context: dict[str, str] = {
        "board.name": board.name,
        "board.premise": board.premise.strip(),
        "board.premise_brief": board.premise.strip()[:PREMISE_BRIEF],
        "board.style": board.style.strip(),
        "board.aspect": board.aspect,
        "board.frame_count": str(len(board.frames)),
        "characters": characters,
        "character_names": ", ".join(c.name for c in board.characters if c.name),
        "count": str(count if count is not None else len(board.frames)),
        "project.name": getattr(project, "name", "") or "",
    }
    # Empty when there is nothing on that side, which is exactly what an `[[optional block]]` in a
    # template needs to drop itself — writing at the very beginning should not carry a heading over
    # nothing.
    context["story.before"] = frame_block(before or [])
    context["story.after"] = frame_block(after or [])
    context["story.all"] = frame_block(board.frames)
    context["position"] = position

    for key, value in board.fields.items():
        context[f"board.fields.{key}"] = value

    if frame is not None:
        named = character_block(board, frame.character_ids)
        context.update({
            "frame.id": frame.id,
            "frame.title": frame.title,
            "frame.action": frame.action,
            "frame.camera": frame.camera,
            "frame.image_prompt": frame.image_prompt,
            "frame.shot_prompt": frame.shot_prompt,
            "frame.notes": frame.notes,
            "frame.status": frame.status,
            "frame.order": str(frame.order),
            # One-based, because that is what the frame is called everywhere a person can see it.
            "frame.number": str(frame.order + 1),
            # What the old describe prompt fell back through, named so a template can use it too.
            "frame.intent": frame.action or frame.title or "unspecified",
            # The motion prompt, or the still's prompt when nobody has written one yet.
            "frame.motion": frame.shot_prompt or frame.image_prompt,
            "frame.characters": named,
            "frame.character_names": ", ".join(
                c.name for c in board.characters if c.id in frame.character_ids and c.name
            ),
        })
        for key, value in frame.fields.items():
            context[f"frame.fields.{key}"] = value

    if character is not None:
        context.update({
            "character.id": character.id,
            "character.name": character.name,
            "character.description": character.description,
            "character.appearance": character.appearance,
        })

    for stage_id, payload in (outputs or {}).items():
        for key, value in payload.items():
            text = as_text(value)
            context[f"stage.{stage_id}.{key}"] = text
            if stage_id == previous:
                context[f"prev.{key}"] = text

    return context
