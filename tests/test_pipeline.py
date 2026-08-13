"""The pieces the editable pipeline is built from: filling in a template, and generating a schema.

Nothing here touches a model, a project or the network. That is deliberate — these are the parts that
have to be right before anything downstream can be trusted, so they are tested where the failure is
unambiguous.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from comfywebstudio.core.pipeline import (
    OutputField,
    Pipeline,
    PipelineOverlay,
    Stage,
    StageRun,
)
from comfywebstudio.core.prompting import json_schema, render, token_names, unknown_tokens


class TestFillingInATemplate:
    def test_a_token_is_replaced_by_its_value(self):
        text, unknown = render("Break this into {count} shots.", {"count": "6"})
        assert text == "Break this into 6 shots."
        assert unknown == []

    def test_a_dotted_token_is_one_key_not_an_attribute_walk(self):
        text, _ = render("{frame.title}: {frame.action}",
                         {"frame.title": "The beam", "frame.action": "It sweeps."})
        assert text == "The beam: It sweeps."

    def test_there_is_no_attribute_traversal_to_abuse(self):
        # The whole security argument in one test. Nothing is *blocked*: these are simply keys nobody put
        # in the dict, so they come back untouched — and are named as the nonsense they are.
        text, unknown = render("{board.__class__} {x.__dict__}", {"board.premise": "..."})
        assert text == "{board.__class__} {x.__dict__}"
        assert unknown == ["board.__class__", "x.__dict__"]

    def test_literal_json_in_a_prompt_is_left_alone(self):
        # This is what lets the built-in prompts be moved into templates verbatim rather than escaped.
        template = 'Answer with:\n{"frames": [{"title": "...", "action": "..."}]}'
        text, unknown = render(template, {})
        assert text == template
        assert unknown == []

    def test_an_unknown_token_survives_verbatim_and_is_reported(self):
        text, unknown = render("Style: {board.styel}", {"board.style": "muted"})
        assert text == "Style: {board.styel}"       # visible, not silently deleted
        assert unknown == ["board.styel"]

    def test_an_unknown_token_is_reported_once_however_often_it_appears(self):
        _, unknown = render("{a.b} {a.b} {a.b}", {})
        assert unknown == ["a.b"]

    def test_a_known_but_empty_token_resolves_to_nothing(self):
        text, unknown = render("Style: {board.style}.", {"board.style": ""})
        assert text == "Style: ."
        assert unknown == []


class TestOptionalBlocks:
    def test_a_block_drops_when_its_token_is_empty(self):
        text, _ = render("A\n[[ASPECT: {board.aspect}]]\nB", {"board.aspect": ""})
        assert text == "A\n\nB"

    def test_a_block_survives_when_its_token_has_a_value(self):
        text, _ = render("A\n[[ASPECT: {board.aspect}]]\nB", {"board.aspect": "16:9"})
        assert text == "A\nASPECT: 16:9\nB"

    def test_one_resolved_token_keeps_the_whole_block(self):
        text, _ = render("[[{a} and {b}]]", {"a": "", "b": "this"})
        assert text == "and this"

    def test_a_block_with_no_tokens_at_all_is_kept(self):
        # Brackets mark text conditional on something. Conditional on nothing is just text.
        text, _ = render("[[always]]", {})
        assert text == "always"

    def test_a_dropped_block_does_not_leave_a_hole(self):
        text, _ = render("A\n\n[[X: {gone}]]\n\nB", {"gone": ""})
        assert text == "A\n\nB"

    def test_an_unknown_token_inside_a_block_keeps_it(self):
        # An unresolved token must stay visible, so the block cannot silently vanish around it.
        text, unknown = render("[[X: {nope}]]", {})
        assert text == "X: {nope}"
        assert unknown == ["nope"]


class TestTheTokenPalette:
    def test_it_offers_every_key_sorted(self):
        assert token_names({"count": "3", "board.premise": "x"}) == ["board.premise", "count"]

    def test_it_names_the_tokens_a_template_would_not_resolve(self):
        assert unknown_tokens("{a} {b.c} {a}", {"a": "1"}) == ["b.c"]


class TestGeneratingTheSchema:
    def test_a_string_field_becomes_a_described_string(self):
        assert json_schema([OutputField(key="title", description="a few words naming the shot")]) == {
            "type": "object",
            "properties": {"title": {"type": "string", "description": "a few words naming the shot"}},
            "required": ["title"],
        }

    def test_a_field_with_no_description_carries_none(self):
        # The guard that keeps generated schemas identical to the hand-written ones they replace.
        assert json_schema([OutputField(key="characters", type="string_list", description="")]) == {
            "type": "object",
            "properties": {"characters": {"type": "array", "items": {"type": "string"}}},
            "required": ["characters"],
        }

    def test_an_optional_field_is_left_out_of_required(self):
        schema = json_schema([
            OutputField(key="a"),
            OutputField(key="b", required=False),
        ])
        assert schema["required"] == ["a"]

    def test_a_list_of_objects_nests_its_own_shape(self):
        schema = json_schema([
            OutputField(key="frames", type="object_list", fields=[
                OutputField(key="title", description="a few words naming the shot"),
                OutputField(key="characters", type="string_list", required=False),
            ]),
        ])
        assert schema["properties"]["frames"] == {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "a few words naming the shot"},
                    "characters": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title"],
            },
        }

    def test_text_is_a_string_because_only_the_editor_tells_them_apart(self):
        assert json_schema([OutputField(key="a", type="text")])["properties"]["a"]["type"] == "string"

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [("integer", "integer"), ("number", "number"), ("boolean", "boolean")],
    )
    def test_the_other_scalars_keep_their_type(self, kind, expected):
        assert json_schema([OutputField(key="a", type=kind)])["properties"]["a"]["type"] == expected


class TestOutputNames:
    @pytest.mark.parametrize("key", ["image_prompt", "a", "a1", "a_b_c"])
    def test_a_usable_name_is_accepted(self, key):
        assert OutputField(key=key).key == key

    @pytest.mark.parametrize("key", ["Image", "1a", "a-b", "a.b", "", "a" * 33, "a b"])
    def test_a_name_that_would_not_work_as_a_token_is_refused(self, key):
        # It has to serve as a JSON key *and* a {token}, so it is held to the stricter of the two.
        with pytest.raises(ValidationError):
            OutputField(key=key)

    def test_the_reason_says_what_to_do_about_it(self):
        with pytest.raises(ValidationError, match="lowercase letters, digits and underscores"):
            OutputField(key="Image Prompt")


class TestTheModelsThemselves:
    def test_a_stage_defaults_to_a_writing_step_that_runs_once(self):
        stage = Stage()
        assert (stage.kind, stage.scope, stage.model.role) == ("llm", "board", "write")
        assert stage.enabled and stage.retry is None

    def test_a_stage_gets_an_id_when_it_is_not_a_builtin(self):
        assert Stage().id.startswith("stage")

    def test_an_empty_overlay_knows_it_is_empty(self):
        assert PipelineOverlay().is_empty()
        assert not PipelineOverlay(stages={"write": Stage(id="write")}).is_empty()
        assert not PipelineOverlay(order=["write"]).is_empty()
        assert not PipelineOverlay(removed=["write"]).is_empty()

    def test_a_pipeline_finds_a_stage_by_id(self):
        pipeline = Pipeline(stages=[Stage(id="write"), Stage(id="draw")])
        assert pipeline.stage("draw").id == "draw"
        assert pipeline.stage("nope") is None

    def test_a_stage_run_starts_running_and_unfinished(self):
        run = StageRun(board_id="b", stage_id="write")
        assert run.status == "running"
        assert run.finished is None and not run.truncated
