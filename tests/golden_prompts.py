"""The storyboard prompts and schemas exactly as they were before they became editable stages.

Frozen copies, transcribed from `llm/storywriter.py` as it stood, and kept independently of the code so
that `test_pipeline_builtin.py` is comparing the built-ins against *something else* rather than against
themselves. They were verified equal to the live originals before those were deleted; from here on their
job is to catch an accidental edit to a prompt that was tuned deliberately.

Changing one of these is a real decision. If a built-in prompt is improved on purpose, update the copy
here in the same commit and bump `BUILTIN_REVISION` — that pairing is what tells a user whose board edited
the stage that the default has moved on.
"""

from __future__ import annotations

from typing import Any

WRITER_SYSTEM = """You are a storyboard artist working with a director.

You break an idea into a sequence of shots that could actually be filmed, and for each shot you write
three separate things:

- "action": what happens, in plain prose. One or two sentences. No camera language.
- "camera": the framing and any movement. Short. "Wide, static." "Close on her hands, slow push in."
- "image_prompt": what a single still of this shot looks like, written for an image generator. Concrete
  nouns, light, setting, composition. Describe one frozen moment, never a sequence, and never motion.
- "shot_prompt": how that still should come to life, written for an image-to-video generator. Describe
  movement only — of the subject and of the camera. Do not re-describe the scene.

You answer with JSON only. No commentary, no code fences."""

VISION_SYSTEM = """You are a storyboard artist describing a frame you have been handed.

You look at the image and describe what is actually in it — not what you assume was intended. Be concrete
and specific about the subject, the setting, the light and the composition.

You also say how the frame should move. That is a different question from what is in it, and it is never
left blank: something always moves, even if it is only the camera. Write it as motion alone.

Good motion: "slow push in as the beam sweeps left"; "the boat rocks; the camera holds"; "she turns her
head towards the window".

You answer with JSON only. No commentary, no code fences."""


def writer_prompt(board, count: int) -> str:
    """`llm.storywriter._writer_prompt`, verbatim."""
    characters = "\n".join(
        f"- {c.name}: {c.description or 'no description'}"
        + (f" Appearance: {c.appearance}" if c.appearance else "")
        for c in board.characters
    )
    return f"""Break this into exactly {count} shots.

PREMISE:
{board.premise.strip()}

{f'HOUSE STYLE (apply to every image_prompt): {board.style.strip()}' if board.style.strip() else ''}
{f'ASPECT: {board.aspect}' if board.aspect else ''}
{f'CHARACTERS (use these names in the action, and their appearance in the image prompts):{chr(10)}{characters}' if characters else ''}

Answer with this exact shape:
{{"frames": [{{"title": "...", "action": "...", "camera": "...", "image_prompt": "...",
  "shot_prompt": "...", "characters": ["name", ...]}}]}}

"characters" lists the names of any listed characters who appear in that shot; use [] when none do."""


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


FRAMES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "frames": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": _string("a few words naming the shot"),
                    "action": _string("what happens, in prose"),
                    "camera": _string("framing and movement"),
                    "image_prompt": _string("what a single still looks like"),
                    "shot_prompt": _string("how it moves; motion only"),
                    "characters": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "action", "camera", "image_prompt", "shot_prompt"],
            },
        }
    },
    "required": ["frames"],
}

DESCRIBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": _string("what is actually in the frame"),
        "image_prompt": _string("a prompt that would reproduce this frame"),
        "shot_prompt": _string("how this frame should move; motion only"),
    },
    "required": ["description", "image_prompt", "shot_prompt"],
}

CHARACTERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": _string("their name"),
                    "description": _string("who they are"),
                    "appearance": _string("what they look like, for an image generator"),
                },
                "required": ["name", "description", "appearance"],
            },
        }
    },
    "required": ["characters"],
}
