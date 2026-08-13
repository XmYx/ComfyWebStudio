"""The built-in stages must say exactly what the hardcoded versions said.

These prompts were tuned against real models — the wording, the insistence, the examples. Moving them into
templates is a refactor, and a refactor that quietly changes what gets sent to a model is not a refactor,
it is a regression nobody will notice until the output is worse.

`tests/golden_prompts.py` holds frozen copies of the originals, transcribed before they were deleted and
verified equal to them at the time. Comparing against those rather than against the code is what keeps
this a test instead of a tautology.
"""

from __future__ import annotations

import pytest

from comfywebstudio.core.pipeline import Pipeline, PipelineOverlay, Stage, StageModel
from comfywebstudio.core.prompting import GAP, json_schema, render
from comfywebstudio.core.storyboard import Storyboard, StoryboardCharacter, StoryboardFrame
from comfywebstudio.pipeline.builtin import BUILTIN_REVISION, builtin_pipeline
from comfywebstudio.pipeline.context import build_context
from comfywebstudio.pipeline.resolve import (
    apply_overlay,
    overlay_with,
    overlay_without,
    resolve,
    stage_is_stale,
)
from tests import golden_prompts as original


def tidy(text: str) -> str:
    """What the renderer does to whitespace, so the f-string original can be compared like for like.

    The originals interpolate conditionals into the middle of a template, which leaves a run of blank
    lines behind whenever one is empty. The renderer drops the whole line instead. Normalising both is
    what makes the comparison about the *words*, which is the part that matters.
    """
    return GAP.sub("\n\n", text).strip()


@pytest.fixture
def full_board() -> Storyboard:
    return Storyboard(
        name="Nightfall",
        premise="A lighthouse keeper works her last night on the rock.",
        style="muted, hand-painted, cool light",
        aspect="16:9",
        characters=[
            StoryboardCharacter(name="Elara", description="the keeper", appearance="grey coat, forties"),
            StoryboardCharacter(name="The light", description="", appearance=""),
        ],
    )


@pytest.fixture
def bare_board() -> Storyboard:
    return Storyboard(premise="Something happens.", style="", aspect="")


class TestTheSystemPromptsAreUnchanged:
    def test_the_writer_still_says_what_it_said(self):
        assert builtin_pipeline().stage("write").system == original.WRITER_SYSTEM

    def test_the_looker_still_says_what_it_said(self):
        assert builtin_pipeline().stage("describe").system == original.VISION_SYSTEM

    def test_finding_characters_uses_the_writer_voice(self):
        # It always did — it is a writing job, not a looking one.
        assert builtin_pipeline().stage("suggest_characters").system == original.WRITER_SYSTEM


class TestTheWritePromptIsUnchanged:
    def test_with_everything_filled_in(self, full_board):
        stage = builtin_pipeline().stage("write")
        rendered, unknown = render(stage.prompt, build_context(None, full_board, count=6))
        assert rendered == tidy(original.writer_prompt(full_board, 6))
        assert unknown == []

    def test_with_nothing_optional_set(self, bare_board):
        stage = builtin_pipeline().stage("write")
        rendered, unknown = render(stage.prompt, build_context(None, bare_board, count=3))
        assert rendered == tidy(original.writer_prompt(bare_board, 3))
        assert unknown == []

    def test_the_optional_blocks_really_do_drop(self, bare_board):
        rendered, _ = render(
            builtin_pipeline().stage("write").prompt, build_context(None, bare_board, count=3)
        )
        assert "HOUSE STYLE" not in rendered
        assert "CHARACTERS" not in rendered

    def test_the_literal_json_shape_survives(self, full_board):
        rendered, _ = render(
            builtin_pipeline().stage("write").prompt, build_context(None, full_board, count=6)
        )
        assert '{"frames": [{"title": "..."' in rendered


class TestTheOtherPromptsAreUnchanged:
    def test_finding_characters(self, full_board):
        stage = builtin_pipeline().stage("suggest_characters")
        rendered, unknown = render(stage.prompt, build_context(None, full_board))
        assert "Who appears in this? Name each person or creature that recurs." in rendered
        assert full_board.premise in rendered
        assert unknown == []

    def test_looking_at_a_frame(self, full_board):
        full_board.frames.append(
            StoryboardFrame(title="The beam", action="The light turns.", camera="Wide, static.")
        )
        stage = builtin_pipeline().stage("describe")
        rendered, unknown = render(stage.prompt, build_context(None, full_board, full_board.frames[0]))
        assert "This is the still for one shot of a storyboard." in rendered
        assert "The light turns." in rendered
        assert "Camera: Wide, static." in rendered
        assert unknown == []

    def test_looking_at_a_frame_with_no_camera_note(self, bare_board):
        bare_board.frames.append(StoryboardFrame(title="Untitled", action="", camera=""))
        rendered, _ = render(
            builtin_pipeline().stage("describe").prompt,
            build_context(None, bare_board, bare_board.frames[0]),
        )
        assert "Camera:" not in rendered
        # The old code fell back through action -> title -> "unspecified"; so does the context.
        assert "Untitled" in rendered

    def test_a_frame_with_nothing_at_all_says_unspecified(self, bare_board):
        bare_board.frames.append(StoryboardFrame())
        rendered, _ = render(
            builtin_pipeline().stage("describe").prompt,
            build_context(None, bare_board, bare_board.frames[0]),
        )
        assert "unspecified" in rendered


class TestTheSchemasAreUnchanged:
    def test_the_frames_schema(self):
        stage = builtin_pipeline().stage("write")
        assert json_schema(stage.outputs)["properties"]["frames"] == (
            original.FRAMES_SCHEMA["properties"]["frames"]
        )

    def test_the_describe_schema(self):
        stage = builtin_pipeline().stage("describe")
        assert json_schema(stage.outputs) == original.DESCRIBE_SCHEMA

    def test_the_characters_schema(self):
        stage = builtin_pipeline().stage("suggest_characters")
        assert json_schema(stage.outputs)["properties"]["characters"] == (
            original.CHARACTERS_SCHEMA["properties"]["characters"]
        )


class TestTheDrawingAndShotPrompts:
    def test_drawing_appends_the_house_style(self, full_board):
        frame = StoryboardFrame(image_prompt="a lighthouse at night")
        full_board.frames.append(frame)
        rendered, _ = render(
            builtin_pipeline().stage("draw").prompt, build_context(None, full_board, frame)
        )
        # Exactly what `" ".join(part for part in (frame.image_prompt, board.style) if part.strip())` did.
        assert rendered == "a lighthouse at night muted, hand-painted, cool light"

    def test_drawing_with_no_house_style_leaves_no_stray_space(self, bare_board):
        frame = StoryboardFrame(image_prompt="a lighthouse at night")
        bare_board.frames.append(frame)
        rendered, _ = render(
            builtin_pipeline().stage("draw").prompt, build_context(None, bare_board, frame)
        )
        assert rendered == "a lighthouse at night"

    def test_the_shot_prompt_falls_back_to_the_image_prompt(self, bare_board):
        frame = StoryboardFrame(image_prompt="a lighthouse", shot_prompt="")
        bare_board.frames.append(frame)
        rendered, _ = render(
            builtin_pipeline().stage("shot").prompt, build_context(None, bare_board, frame)
        )
        assert rendered == "a lighthouse"


class TestTheShapeOfTheDefaultPipeline:
    def test_it_runs_in_the_order_the_flow_actually_goes(self):
        assert [s.id for s in builtin_pipeline().stages] == [
            "write", "suggest_characters", "draw", "capture", "describe", "shot",
        ]

    def test_every_stage_knows_which_builtin_it_is(self):
        for stage in builtin_pipeline().stages:
            assert stage.builtin_id == stage.id
            assert stage.builtin_revision == BUILTIN_REVISION

    def test_the_per_frame_stages_are_the_ones_that_touch_a_frame(self):
        by_id = {s.id: s for s in builtin_pipeline().stages}
        assert by_id["write"].scope == "board"
        assert by_id["suggest_characters"].scope == "board"
        for stage_id in ("draw", "capture", "describe", "shot"):
            assert by_id[stage_id].scope == "frame"

    def test_only_the_looking_stage_asks_for_the_picture(self):
        for stage in builtin_pipeline().stages:
            assert stage.model.attach_image == (stage.id == "describe")

    def test_characters_say_where_they_would_go(self):
        # The destination is a property of the field; whether it is *applied* is the caller's decision,
        # which is what keeps "propose" and "accept" one stage rather than two.
        stage = builtin_pipeline().stage("suggest_characters")
        assert [f.writes for f in stage.outputs] == ["board.characters"]

    def test_the_frames_the_writer_produces_replace_the_board(self):
        assert [f.writes for f in builtin_pipeline().stage("write").outputs] == ["board.frames"]

    def test_looking_at_a_frame_rewrites_its_own_fields(self):
        assert [f.writes for f in builtin_pipeline().stage("describe").outputs] == [
            "frame.notes", "frame.image_prompt", "frame.shot_prompt",
        ]

    def test_the_motion_retry_is_visible_rather_than_buried(self):
        retry = builtin_pipeline().stage("describe").retry
        assert retry is not None
        assert retry.when_empty == ["shot_prompt"]
        assert "Motion only" in retry.prompt

    def test_each_call_gets_its_own_copy(self):
        builtin_pipeline().stage("write").system = "vandalised"
        assert builtin_pipeline().stage("write").system == original.WRITER_SYSTEM


class TestLayeringEditsOverIt:
    def test_no_overlay_changes_nothing(self):
        base = builtin_pipeline()
        assert apply_overlay(base, None).stages == base.stages
        assert apply_overlay(base, PipelineOverlay()).stages == base.stages

    def test_one_edited_stage_replaces_only_itself(self):
        edited = builtin_pipeline().stage("describe").model_copy(update={"system": "Look harder."})
        result = apply_overlay(builtin_pipeline(), PipelineOverlay(stages={"describe": edited}))
        assert result.stage("describe").system == "Look harder."
        assert result.stage("write").system == original.WRITER_SYSTEM
        assert len(result.stages) == 6

    def test_a_new_stage_is_appended(self):
        added = Stage(id="mood", name="Mood", kind="llm", scope="frame")
        result = apply_overlay(builtin_pipeline(), PipelineOverlay(stages={"mood": added}))
        assert [s.id for s in result.stages][-1] == "mood"

    def test_a_removed_stage_is_gone(self):
        result = apply_overlay(builtin_pipeline(), PipelineOverlay(removed=["capture"]))
        assert "capture" not in [s.id for s in result.stages]

    def test_a_stored_order_is_honoured(self):
        result = apply_overlay(builtin_pipeline(), PipelineOverlay(order=["describe", "write"]))
        assert [s.id for s in result.stages][:2] == ["describe", "write"]

    def test_a_stage_the_stored_order_never_heard_of_is_kept(self):
        # A board saved by an older build must not lose a stage a newer one added.
        result = apply_overlay(builtin_pipeline(), PipelineOverlay(order=["write", "draw"]))
        assert {s.id for s in result.stages} == {
            "write", "suggest_characters", "draw", "capture", "describe", "shot",
        }

    def test_the_board_wins_over_the_app_which_wins_over_the_builtin(self):
        app = PipelineOverlay(
            stages={"describe": builtin_pipeline().stage("describe").model_copy(
                update={"system": "app"})},
        )
        board_overlay = PipelineOverlay(
            stages={"describe": builtin_pipeline().stage("describe").model_copy(
                update={"system": "board"})},
        )
        settings = type("S", (), {"story": type("T", (), {"pipeline": app})()})()
        board = Storyboard(pipeline=board_overlay)

        assert resolve(settings, board).stage("describe").system == "board"
        assert resolve(settings, None).stage("describe").system == "app"
        assert resolve(type("S", (), {"story": type("T", (), {"pipeline": None})()})(),
                       None).stage("describe").system == original.VISION_SYSTEM

    def test_a_board_with_no_overlay_follows_the_app(self):
        app = PipelineOverlay(
            stages={"write": builtin_pipeline().stage("write").model_copy(update={"system": "app"})},
        )
        settings = type("S", (), {"story": type("T", (), {"pipeline": app})()})()
        assert resolve(settings, Storyboard()).stage("write").system == "app"


class TestSavingAndResetting:
    def test_saving_a_stage_records_only_that_one(self):
        stage = builtin_pipeline().stage("write").model_copy(update={"system": "mine"})
        overlay = overlay_with(None, stage)
        assert set(overlay.stages) == {"write"}

    def test_resetting_a_stage_drops_it(self):
        overlay = overlay_with(None, builtin_pipeline().stage("write").model_copy(
            update={"system": "mine"}))
        overlay = overlay_with(overlay, builtin_pipeline().stage("draw").model_copy(
            update={"prompt": "mine"}))
        assert overlay_without(overlay, "write").stages.keys() == {"draw"}

    def test_resetting_the_last_edit_leaves_no_overlay_at_all(self):
        # Back to tracking the layer below, rather than storing a copy that happens to match it.
        overlay = overlay_with(None, builtin_pipeline().stage("write"))
        assert overlay_without(overlay, "write") is None

    def test_resetting_something_never_edited_is_harmless(self):
        assert overlay_without(None, "write") is None

    def test_saving_a_stage_undoes_its_removal(self):
        overlay = overlay_with(PipelineOverlay(removed=["capture"]), Stage(id="capture", kind="capture"))
        assert "capture" not in overlay.removed


class TestStaleness:
    def test_a_stage_edited_against_the_current_builtin_is_fresh(self):
        assert not stage_is_stale(builtin_pipeline().stage("write"))

    def test_a_stage_edited_against_an_older_builtin_is_stale(self):
        old = builtin_pipeline().stage("write").model_copy(update={"builtin_revision": 0})
        assert stage_is_stale(old)

    def test_a_stage_that_was_never_a_builtin_is_never_stale(self):
        assert not stage_is_stale(Stage(id="mine", model=StageModel()))


def test_a_pipeline_round_trips_through_json():
    # It is stored inside project.json and settings.json, so this is not academic.
    original_pipeline = builtin_pipeline()
    assert Pipeline.model_validate(original_pipeline.model_dump(mode="json")) == original_pipeline
