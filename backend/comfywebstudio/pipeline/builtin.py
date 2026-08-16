"""The pipeline you get before you have edited anything.

These five stages are the flow that used to be spread across `llm/storywriter.py`'s constants and
`api/storyboard.py`'s handlers. Nothing about them is privileged any more: they are ordinary stage
records, and a user's edits replace them one at a time.

The prompts are transcribed **verbatim** from the versions that were tuned against real models — the
whitespace, the em dashes, the odd insistent sentence. `core/prompting.py` was designed around that
requirement (lowercase-only tokens, so the literal JSON in these prompts needs no escaping), and
`tests/test_pipeline_builtin.py` holds them to it.

Bump :data:`BUILTIN_REVISION` when a prompt changes. A board that edited a stage records the revision it
diverged at, which is how the panel can say the default has moved on since.
"""

from __future__ import annotations

from ..core.pipeline import OutputField, Pipeline, Stage, StageModel, StageRetry

#: Bumped when any built-in stage below changes. A board that edited a stage records the revision it
#: diverged at, which is how the panel can say the default has moved on since.
BUILTIN_REVISION = 2

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

WRITE_PROMPT = """Break this into exactly {count} shots.

PREMISE:
{board.premise}

[[HOUSE STYLE (apply to every image_prompt): {board.style}]]
[[ASPECT: {board.aspect}]]
[[CHARACTERS (use these names in the action, and their appearance in the image prompts):
{characters}]]

Answer with this exact shape:
{"frames": [{"title": "...", "action": "...", "camera": "...", "image_prompt": "...",
  "shot_prompt": "...", "characters": ["name", ...]}]}

"characters" lists the names of any listed characters who appear in that shot; use [] when none do."""

CHARACTERS_PROMPT = """Who appears in this? Name each person or creature that recurs.

PREMISE:
{board.premise}

[[These are already known. Do not list them again, and do not list them under another name:
{character_names}]]

Answer with:
{"characters": [{"name": "...", "description": "who they are",
  "appearance": "what they look like, written for an image generator"}]}

Answer with an empty list if there is nobody new."""

DESCRIBE_PROMPT = """This is the still for one shot of a storyboard.

THE STORY SO FAR:
{board.premise_brief}

WHAT THIS SHOT IS MEANT TO BE:
{frame.intent}
[[Camera: {frame.camera}]]

Answer three things about it:

- description: what is actually in this frame, concretely.
- image_prompt: a prompt that would reproduce this frame.
- shot_prompt: how this frame should move. Motion only — of the subject, of the camera, or both. Never
  empty, and never a repeat of the description."""

MOTION_RETRY_PROMPT = """Look at this frame again. How should it move?

Motion only — what the subject does, what the camera does, or both. One sentence. Do not describe what
is in the frame; that has already been written down as: {prev.description}"""

#: What actually goes to the drawing workflow. The house style is appended to every frame's prompt, and
#: the renderer's strip means an empty half never leaves a stray space behind.
DRAW_PROMPT = "{frame.image_prompt} {board.style}"

#: The motion prompt, falling back to the image prompt when nobody has written one.
SHOT_PROMPT = "{frame.motion}"

#: What a character's reference picture is drawn from. Their appearance is written for an image generator
#: already — that is what the field is for — so this is mostly the house style catching up with it.
PORTRAIT_PROMPT = "{character.appearance} {board.style}"

EXTEND_PROMPT = """Write exactly {count} more shots for this sequence, to go {position}.

PREMISE:
{board.premise}

[[HOUSE STYLE (apply to every image_prompt): {board.style}]]
[[ASPECT: {board.aspect}]]
[[CHARACTERS (use these names in the action, and their appearance in the image prompts):
{characters}]]

[[THE SHOTS THAT COME BEFORE:
{story.before}]]

[[THE SHOTS THAT COME AFTER:
{story.after}]]

Your shots have to fit that gap and carry it. Pick up from where the shots before leave off, and end
somewhere the shots after can follow on from without a jump. Keep the same people, place and tone.

Do not rewrite or repeat a shot that is already listed, and do not re-tell the story from the beginning.
Write only the {count} new shots, in the order they should play.

Answer with this exact shape:
{"frames": [{"title": "...", "action": "...", "camera": "...", "image_prompt": "...",
  "shot_prompt": "...", "characters": ["name", ...]}]}

"characters" lists the names of any listed characters who appear in that shot; use [] when none do."""


def _frame_fields() -> OutputField:
    """The shape a written shot has. Shared by `write` and `extend` — two stages producing frames that
    disagreed about what a frame is would be a bug waiting to be found in a render."""
    return OutputField(
        key="frames",
        type="object_list",
        writes="board.frames",
        fields=[
            OutputField(key="title", description="a few words naming the shot"),
            OutputField(key="action", description="what happens, in prose"),
            OutputField(key="camera", description="framing and movement"),
            OutputField(key="image_prompt", description="what a single still looks like"),
            OutputField(key="shot_prompt", description="how it moves; motion only"),
            OutputField(key="characters", type="string_list", required=False),
        ],
    )


def builtin_pipeline() -> Pipeline:
    """A fresh copy of the default flow. Never share one — callers layer edits over it."""
    return Pipeline(
        id="storyboard",
        name="Storyboard",
        revision=BUILTIN_REVISION,
        stages=[
            Stage(
                id="write",
                kind="llm",
                scope="board",
                name="Write the shots",
                description="Break the premise into a sequence of frames.",
                system=WRITER_SYSTEM,
                prompt=WRITE_PROMPT,
                model=StageModel(role="write"),
                outputs=[_frame_fields()],
                builtin_id="write",
                builtin_revision=BUILTIN_REVISION,
            ),
            Stage(
                id="extend",
                kind="llm",
                scope="board",
                name="Add more shots",
                description=(
                    "Write further shots into a sequence that already exists — at the top, at the "
                    "bottom, or between two of them."
                ),
                system=WRITER_SYSTEM,
                prompt=EXTEND_PROMPT,
                model=StageModel(role="write"),
                # Off in the whole-pipeline run: extending is something a person asks for at a
                # particular place, and a flow that silently grew the board every time it was run would
                # be its own kind of surprise.
                enabled=False,
                outputs=[_frame_fields()],
                builtin_id="extend",
                builtin_revision=BUILTIN_REVISION,
            ),
            Stage(
                id="suggest_characters",
                kind="llm",
                scope="board",
                name="Find the characters",
                description="Read the premise and name whoever recurs, so they can be given references.",
                system=WRITER_SYSTEM,
                prompt=CHARACTERS_PROMPT,
                model=StageModel(role="write"),
                outputs=[
                    OutputField(
                        key="characters",
                        type="object_list",
                        # Where they *would* go. Whether they actually do is the caller's call: the
                        # suggest route runs this stage in propose-only mode, because proposing is not
                        # deciding and a character list is short enough to accept by hand.
                        writes="board.characters",
                        fields=[
                            OutputField(key="name", description="their name"),
                            OutputField(key="description", description="who they are"),
                            OutputField(
                                key="appearance",
                                description="what they look like, for an image generator",
                            ),
                        ],
                    ),
                ],
                builtin_id="suggest_characters",
                builtin_revision=BUILTIN_REVISION,
            ),
            Stage(
                id="draw",
                kind="comfy",
                scope="frame",
                name="Draw the frames",
                description="Run the text-to-image workflow to make each frame's still.",
                prompt=DRAW_PROMPT,
                slot="image",
                reroll_seed=True,
                builtin_id="draw",
                builtin_revision=BUILTIN_REVISION,
            ),
            Stage(
                id="capture",
                kind="capture",
                scope="frame",
                name="Keep the still",
                description="Promote the drawn picture to a project asset, so it survives the run.",
                only_if_missing=True,
                sets_status="imaged",
                builtin_id="capture",
                builtin_revision=BUILTIN_REVISION,
            ),
            Stage(
                id="describe",
                kind="llm",
                scope="frame",
                name="Look at the frame",
                description="Rewrite the prompts from the picture that was actually generated.",
                system=VISION_SYSTEM,
                prompt=DESCRIBE_PROMPT,
                model=StageModel(role="vision", attach_image=True),
                outputs=[
                    OutputField(
                        key="description",
                        description="what is actually in the frame",
                        writes="frame.notes",
                    ),
                    OutputField(
                        key="image_prompt",
                        description="a prompt that would reproduce this frame",
                        writes="frame.image_prompt",
                    ),
                    OutputField(
                        key="shot_prompt",
                        description="how this frame should move; motion only",
                        writes="frame.shot_prompt",
                    ),
                ],
                retry=StageRetry(
                    when_empty=["shot_prompt"],
                    prompt=MOTION_RETRY_PROMPT,
                    temperature_delta=0.2,
                ),
                sets_status="described",
                builtin_id="describe",
                builtin_revision=BUILTIN_REVISION,
            ),
            Stage(
                id="shot",
                kind="shot",
                scope="frame",
                name="Make the shot",
                description="Turn the frame into a real shot, with its still wired into the workflow.",
                prompt=SHOT_PROMPT,
                slot="video",
                sets_status="shot",
                builtin_id="shot",
                builtin_revision=BUILTIN_REVISION,
            ),
        ],
    )
