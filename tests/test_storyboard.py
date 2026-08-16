"""The storyboard pipeline: premise → frames → stills → descriptions → shots.

The language model is scripted here rather than called. That is not only for speed: a test that depends on
what a 30-billion-parameter model felt like saying today tells you nothing when it fails. What is worth
pinning down is everything *around* the model — that a reply is parsed into frames, that the frames become
runnable steps, that a still becomes an asset, that a frame becomes a shot with its image wired to the
right input — and all of that is deterministic.

The model's own behaviour is checked separately, by hand, against a real Ollama.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from comfywebstudio.llm.provider import REGISTRY, LlmProvider, ModelInfo, Reply
from comfywebstudio.settings import LlmProviderConfig

from .test_api import _png_bytes, import_workflow, make_project, run_and_wait

#: What the scripted model answers with, keyed by what it was asked to do.
SCRIPTED: dict[str, dict] = {
    "frames": {
        "frames": [
            {
                "title": "The Answer",
                "action": "Elara sees a light blink back from the water.",
                "camera": "Wide, static.",
                "image_prompt": "a lighthouse keeper at a window, dark sea beyond",
                "shot_prompt": "she leans closer to the glass",
                "characters": ["Elara"],
            },
            {
                "title": "Into the Dark",
                "action": "She rows out towards it.",
                "camera": "Low, tracking.",
                "image_prompt": "a rowing boat on black water, lantern light",
                "shot_prompt": "the boat rocks forward through the swell",
                "characters": [],
            },
        ]
    },
    "characters": {
        "characters": [
            {"name": "Elara", "description": "The lighthouse keeper", "appearance": "weathered coat"}
        ]
    },
    "describe": {
        "description": "A woman in a heavy coat, lit from one side, sea behind her.",
        "image_prompt": "woman in heavy coat at a rain-streaked window, cold side light",
        "shot_prompt": "slow push in as she turns her head",
    },
}


class ScriptedProvider(LlmProvider):
    """Answers whichever of the questions it was asked, and records exactly how it was asked.

    Dispatch is on the **schema**, not on the wording of the prompt. Prompts are editable now, so a test
    that changes one would silently start getting the wrong canned answer — whereas the shape of the
    answer a stage asks for is a structural fact about that stage.

    It also records the rendered `system` and `prompt`, which is what lets a test assert that an edited
    template genuinely reached the model rather than merely being stored.
    """

    calls: list[dict] = []

    async def models(self) -> list[ModelInfo]:
        return [ModelInfo(name="writer"), ModelInfo(name="looker", vision=True)]

    async def complete(self, prompt, *, model, system="", images=None, json_object=False,
                       schema=None, temperature=0.7) -> Reply:
        ScriptedProvider.calls.append({
            "model": model, "images": len(images or []), "json": json_object, "schema": schema,
            "system": system, "prompt": prompt, "temperature": temperature,
        })
        asked = set((schema or {}).get("properties", {}))

        if "frames" in asked:
            return Reply(text=json.dumps(SCRIPTED["frames"]), model=model)
        if "characters" in asked:
            return Reply(text=json.dumps(SCRIPTED["characters"]), model=model)
        if asked == {"shot_prompt"}:
            # The narrow follow-up the describe stage asks when the motion came back blank. The old
            # prompt-sniffing answered this with the *whole* describe payload, which meant the retry was
            # never really under test.
            return Reply(
                text=json.dumps({"shot_prompt": SCRIPTED["describe"]["shot_prompt"]}), model=model
            )
        if images or "description" in asked:
            return Reply(text=json.dumps(SCRIPTED["describe"]), model=model)
        return Reply(text="{}", model=model)


@pytest.fixture(autouse=True)
def scripted_model(app_state):
    """Register the scripted provider and point the storyboard settings at it."""
    REGISTRY["scripted"] = ScriptedProvider
    ScriptedProvider.calls = []
    app_state.settings.llm_providers = [
        LlmProviderConfig(id="scripted", name="Scripted", kind="scripted")
    ]
    app_state.settings.story.provider_id = "scripted"
    app_state.settings.story.write_model = "writer"
    app_state.settings.story.vision_provider_id = "scripted"
    app_state.settings.story.vision_model = "looker"
    yield
    REGISTRY.pop("scripted", None)


def t2i_prompt(*, seed: bool = False) -> dict:
    prompt: dict = {
        "1": {"class_type": "WSStringInput", "inputs": {"port_name": "prompt", "value": "x"}},
        "2": {"class_type": "EmptyImage",
              "inputs": {"width": 32, "height": 32, "batch_size": 1, "color": 0}},
        "3": {"class_type": "WSImageOutput",
              "inputs": {"image": ["2", 0], "port_name": "image", "format": "png", "run_key": ""}},
    }
    if seed:
        # `WSSeedInput` is what marks a parameter as a seed, which is what a reroll varies.
        prompt["4"] = {"class_type": "WSSeedInput", "inputs": {"port_name": "seed", "value": 1}}
    return prompt


def i2v_prompt() -> dict:
    """One image input, one prompt, one image output — the shape an image-to-video workflow has."""
    return {
        "1": {"class_type": "WSImageInput", "inputs": {"port_name": "start_image", "source": ""}},
        "2": {"class_type": "WSStringInput", "inputs": {"port_name": "motion", "value": "x"}},
        "3": {"class_type": "WSImageOutput",
              "inputs": {"image": ["1", 0], "port_name": "frames", "format": "png", "run_key": ""}},
    }


# -- adding more shots ----------------------------------------------------------------------------------


async def _written(client):
    """A board with the two scripted shots on it."""
    pid, bid, _d, _a = await storyboard_project(client)
    board = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})).json()
    return pid, bid, board


async def test_more_shots_can_be_added_at_the_end(client):
    pid, bid, board = await _written(client)
    original = [f["id"] for f in board["frames"]]

    extended = (
        await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/extend", json={"count": 2, "at": "end"}
        )
    ).json()
    ids = [f["id"] for f in extended["frames"]]
    assert ids[: len(original)] == original, "what was there stayed where it was"
    assert len(ids) == len(original) + 2
    assert [f["order"] for f in extended["frames"]] == list(range(len(ids))), "renumbered in order"


async def test_more_shots_can_be_added_at_the_beginning(client):
    pid, bid, board = await _written(client)
    original = [f["id"] for f in board["frames"]]

    extended = (
        await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/extend", json={"count": 2, "at": "start"}
        )
    ).json()
    ids = [f["id"] for f in extended["frames"]]
    assert ids[-len(original) :] == original, "the original shots were pushed down, not overwritten"
    assert ids[: len(ids) - len(original)] and set(ids[:2]).isdisjoint(original), "the new ones lead"
    assert [f["order"] for f in extended["frames"]] == list(range(len(ids)))


async def test_more_shots_can_be_added_between_two_that_exist(client):
    pid, bid, board = await _written(client)
    first, second = board["frames"][0], board["frames"][1]

    extended = (
        await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/extend",
            json={"count": 2, "at": "after", "after_frame_id": first["id"]},
        )
    ).json()
    ids = [f["id"] for f in extended["frames"]]
    assert ids[0] == first["id"]
    assert ids[-1] == second["id"], "the shot that followed still follows"
    assert len(ids) == 4


async def test_the_model_is_told_what_comes_before_and_after(client):
    """The whole point: shots written for a gap, not the premise restated from the top."""
    pid, bid, board = await _written(client)
    first = board["frames"][0]
    ScriptedProvider.calls.clear()

    await client.post(
        f"/api/projects/{pid}/storyboards/{bid}/extend",
        json={"count": 1, "at": "after", "after_frame_id": first["id"]},
    )
    prompt = ScriptedProvider.calls[-1]["prompt"]
    assert "THE SHOTS THAT COME BEFORE" in prompt
    assert board["frames"][0]["title"] in prompt
    assert "THE SHOTS THAT COME AFTER" in prompt
    assert board["frames"][1]["title"] in prompt
    assert "between the shots above and the shots below" in prompt


async def test_writing_at_the_beginning_does_not_pretend_there_is_anything_before(client):
    """The optional block drops out rather than heading an empty list."""
    pid, bid, _board = await _written(client)
    ScriptedProvider.calls.clear()

    await client.post(
        f"/api/projects/{pid}/storyboards/{bid}/extend", json={"count": 1, "at": "start"}
    )
    prompt = ScriptedProvider.calls[-1]["prompt"]
    assert "THE SHOTS THAT COME BEFORE" not in prompt
    assert "THE SHOTS THAT COME AFTER" in prompt
    assert "at the very beginning" in prompt


async def test_how_many_to_add_is_asked_for_and_bounded(client):
    pid, bid, _board = await _written(client)
    ScriptedProvider.calls.clear()

    await client.post(
        f"/api/projects/{pid}/storyboards/{bid}/extend", json={"count": 500, "at": "end"}
    )
    assert "exactly 60 more shots" in ScriptedProvider.calls[-1]["prompt"], "clamped, not refused"


async def test_adding_after_a_frame_that_is_not_there_is_refused(client):
    pid, bid, _board = await _written(client)
    response = await client.post(
        f"/api/projects/{pid}/storyboards/{bid}/extend",
        json={"count": 1, "at": "after", "after_frame_id": "frame_nope"},
    )
    assert response.status_code == 404


async def test_adding_after_nothing_in_particular_is_refused(client):
    pid, bid, _board = await _written(client)
    response = await client.post(
        f"/api/projects/{pid}/storyboards/{bid}/extend", json={"count": 1, "at": "after"}
    )
    assert response.status_code == 422


async def storyboard_project(client, *, seed: bool = False):
    """A project with both workflows bound, and a written board."""
    project = await make_project(client)
    pid = project["id"]
    draw = await import_workflow(client, pid, "Draw", t2i_prompt(seed=seed))
    animate = await import_workflow(client, pid, "Animate", i2v_prompt())

    board = (
        await client.post(
            f"/api/projects/{pid}/storyboards",
            json={"name": "Board", "premise": "A keeper sees a light at sea.", "style": "16mm"},
        )
    ).json()
    await client.patch(
        f"/api/projects/{pid}/storyboards/{board['id']}",
        json={"binding": {
            "image_workflow_id": draw["id"], "image_prompt_param": "prompt",
            "video_workflow_id": animate["id"], "video_prompt_param": "motion",
            "video_image_port": "start_image",
        }},
    )
    return pid, board["id"], draw, animate


async def draw_and_wait(client, pid: str, bid: str, **body) -> dict:
    """Draw through the storyboard's own endpoint, and wait for the run it started.

    The endpoint builds the steps and queues the run in one request, so there is a run id to wait on
    rather than a shot to run afterwards.
    """
    response = await client.post(f"/api/projects/{pid}/storyboards/{bid}/draw", json=body)
    assert response.status_code == 202, response.text
    result = response.json()

    await asyncio.wait_for(client.app_state.orchestrator.wait(result["run_id"]), timeout=30)
    return result


async def test_a_premise_becomes_frames(client):
    pid, bid, _draw, _animate = await storyboard_project(client)

    board = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})).json()

    assert [f["title"] for f in board["frames"]] == ["The Answer", "Into the Dark"]
    assert board["frames"][0]["order"] == 0 and board["frames"][1]["order"] == 1
    # The three kinds of writing stay apart — that is the whole reason they are separate fields.
    first = board["frames"][0]
    assert first["action"] and first["image_prompt"] and first["shot_prompt"]
    assert first["image_prompt"] != first["shot_prompt"]


async def test_writing_is_constrained_to_a_schema(client):
    """A schema, not merely "some JSON".

    Asked only for JSON, a smaller model will answer with an object where a sentence was wanted, or echo
    a field name back as its own value — both seen from qwen2.5vl. A schema makes those impossible rather
    than something to repair afterwards.
    """
    pid, bid, _d, _a = await storyboard_project(client)
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})

    schema = ScriptedProvider.calls[-1]["schema"]
    assert schema is not None
    frame = schema["properties"]["frames"]["items"]["properties"]
    assert frame["image_prompt"]["type"] == "string"
    assert frame["shot_prompt"]["type"] == "string"


async def test_describing_is_constrained_to_a_schema(client):
    pid, bid, _d, _a = await storyboard_project(client)
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})
    built = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/stills")).json()
    await run_and_wait(client, pid, built["shot_id"])
    board = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()
    frame_id = board["frames"][0]["id"]
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frame_id}/capture")

    await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frame_id}/describe")

    schema = ScriptedProvider.calls[-1]["schema"]
    assert set(schema["required"]) == {"description", "image_prompt", "shot_prompt"}


async def test_characters_are_proposed_but_not_saved(client):
    """Proposing is not deciding; the user accepts them."""
    pid, bid, _d, _a = await storyboard_project(client)

    proposed = (
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/characters/suggest")
    ).json()
    assert [c["name"] for c in proposed] == ["Elara"]

    board = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()
    assert board["characters"] == []


async def test_named_characters_are_linked_to_the_frames_they_appear_in(client):
    pid, bid, _d, _a = await storyboard_project(client)
    character = (
        await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/characters", json={"name": "Elara"}
        )
    ).json()

    board = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})).json()

    assert board["frames"][0]["character_ids"] == [character["id"]]
    assert board["frames"][1]["character_ids"] == []


async def test_frames_become_runnable_steps(client):
    """Drawing is running a workflow with one parameter changed — which is a step."""
    pid, bid, draw, _a = await storyboard_project(client)
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})

    built = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/stills")).json()

    project = (await client.get(f"/api/projects/{pid}")).json()
    stills = next(s for s in project["shots"] if s["id"] == built["shot_id"])
    assert len(stills["steps"]) == 2
    assert all(step["workflow_id"] == draw["id"] for step in stills["steps"])
    # The frame's prompt, with the house style appended, is what the step actually runs.
    prompts = [step["param_overrides"]["prompt"] for step in stills["steps"]]
    assert all("16mm" in prompt for prompt in prompts)
    assert any("lighthouse keeper" in prompt for prompt in prompts)


async def test_the_stills_shot_stays_out_of_the_way(client):
    """It is scaffolding, not work — so it must not appear in the shot list or the timeline."""
    pid, bid, _d, _a = await storyboard_project(client)
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/stills")

    project = (await client.get(f"/api/projects/{pid}")).json()
    stills = next(s for s in project["shots"] if s["name"].endswith("stills"))
    assert stills["template_edit_id"], "a marked shot is one the lists skip"


async def test_building_the_stills_twice_updates_rather_than_duplicates(client):
    pid, bid, _d, _a = await storyboard_project(client)
    board = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})).json()
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/stills")

    frame = board["frames"][0]
    await client.patch(
        f"/api/projects/{pid}/storyboards/{bid}/frames/{frame['id']}",
        json={"image_prompt": "a different picture entirely"},
    )
    built = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/stills")).json()

    project = (await client.get(f"/api/projects/{pid}")).json()
    stills = next(s for s in project["shots"] if s["id"] == built["shot_id"])
    assert len(stills["steps"]) == 2, "editing a prompt must not add a second step for the same frame"
    step = next(s for s in stills["steps"] if s["name"] == frame["id"])
    assert "a different picture entirely" in step["param_overrides"]["prompt"]


async def test_rebinding_to_another_workflow_repoints_the_steps(client):
    """Otherwise the board would keep drawing with the workflow it was told to stop using."""
    pid, bid, draw, _a = await storyboard_project(client)
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})
    built = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/stills")).json()

    other = await import_workflow(client, pid, "Draw differently", t2i_prompt())
    await client.patch(
        f"/api/projects/{pid}/storyboards/{bid}",
        json={"binding": {"image_workflow_id": other["id"], "image_prompt_param": "prompt"}},
    )
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/stills")

    project = (await client.get(f"/api/projects/{pid}")).json()
    stills = next(s for s in project["shots"] if s["id"] == built["shot_id"])
    assert len(stills["steps"]) == 2, "re-binding must not add a second step per frame"
    assert {s["workflow_id"] for s in stills["steps"]} == {other["id"]}
    assert draw["id"] != other["id"]
    # The prompt still reaches it: the parameter is re-applied, not left behind on the old workflow.
    assert all(s["param_overrides"]["prompt"] for s in stills["steps"])


# -- drawing, and drawing again -------------------------------------------------------------------------


async def test_drawing_the_board_builds_the_steps_and_runs_them(client):
    """One request, because "draw the board" is not a thing to do in two halves."""
    pid, bid, _d, _a = await storyboard_project(client)
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})

    result = await draw_and_wait(client, pid, bid)

    assert len(result["steps"]) == 2
    run = (await client.get(f"/api/projects/{pid}/runs/{result['run_id']}")).json()
    assert run["status"] == "success", run.get("error")
    assert {sr["step_id"] for sr in run["step_runs"]} == set(result["steps"].values())


async def test_drawing_one_frame_leaves_the_others_alone(client):
    """The loop a storyboard is actually used in: nine frames land, one is wrong, redraw that one."""
    pid, bid, _d, _a = await storyboard_project(client)
    board = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})).json()
    first, second = board["frames"][0], board["frames"][1]
    await draw_and_wait(client, pid, bid)

    result = await draw_and_wait(client, pid, bid, frame_ids=[second["id"]])

    assert list(result["steps"]) == [second["id"]]
    run = (await client.get(f"/api/projects/{pid}/runs/{result['run_id']}")).json()
    assert [sr["step_id"] for sr in run["step_runs"]] == [result["steps"][second["id"]]]
    assert first["id"] not in result["steps"]


async def test_rerolling_a_frame_varies_its_seed(client):
    """A new seed rather than merely ignoring the cache: the number that drew it is recorded."""
    pid, bid, _d, _a = await storyboard_project(client, seed=True)
    board = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})).json()
    frame = board["frames"][0]
    built = await draw_and_wait(client, pid, bid)

    project = (await client.get(f"/api/projects/{pid}")).json()
    stills = next(s for s in project["shots"] if s["id"] == built["shot_id"])
    before = next(s for s in stills["steps"] if s["name"] == frame["id"])["param_overrides"].get("seed")

    result = await draw_and_wait(client, pid, bid, frame_ids=[frame["id"]], reroll=True)

    assert result["seeded"] is True
    project = (await client.get(f"/api/projects/{pid}")).json()
    stills = next(s for s in project["shots"] if s["id"] == built["shot_id"])
    after = next(s for s in stills["steps"] if s["name"] == frame["id"])["param_overrides"]["seed"]
    assert after != before, "a reroll that draws the same seed draws the same picture"
    # The other frame is untouched, seed and all.
    other = next(s for s in stills["steps"] if s["name"] == board["frames"][1]["id"])
    assert "seed" not in other["param_overrides"]


async def test_a_workflow_with_no_seed_says_a_reroll_cannot_vary(client):
    """Reporting it is the difference between "this workflow cannot" and "the button is broken"."""
    pid, bid, _d, _a = await storyboard_project(client)
    board = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})).json()

    result = await draw_and_wait(
        client, pid, bid, frame_ids=[board["frames"][0]["id"]], reroll=True
    )

    assert result["seeded"] is False


async def test_drawing_nothing_says_so(client):
    pid, bid, _d, _a = await storyboard_project(client)

    response = await client.post(f"/api/projects/{pid}/storyboards/{bid}/draw", json={})
    assert response.status_code == 422
    assert "write the shots" in response.text.lower()


# -- what a frame is showing ----------------------------------------------------------------------------


async def test_a_frame_shows_its_still_without_being_kept(client):
    """The whole point: a run's artifacts are already on disk, so nothing needs capturing to be seen."""
    pid, bid, _d, _a = await storyboard_project(client)
    board = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})).json()
    frame = board["frames"][0]
    built = await draw_and_wait(client, pid, bid)

    state = (await client.get(f"/api/projects/{pid}/storyboards/{bid}/stills")).json()

    assert state["shot_id"] == built["shot_id"]
    shown = state["frames"][frame["id"]]
    assert shown["image"], "a drawn frame has a picture before anyone presses Keep"
    assert shown["source"] == "still"
    assert shown["kept"] is False
    # The step id is what a live progress event is keyed by, so it matters as much as the image.
    assert shown["step_id"] == built["steps"][frame["id"]]
    assert shown["status"] in {"success", "cached"}

    project = (await client.get(f"/api/projects/{pid}")).json()
    assert project["assets"] == {}, "showing a still must not quietly fill the asset library"


async def test_a_dropped_in_image_wins_until_the_frame_is_drawn_again(client):
    """Both directions: drop a plate onto a drawn frame, then reroll it. The newer picture is shown.

    Neither source wins by rule, which is what makes both moves work without undoing the other first.
    """
    pid, bid, _d, _a = await storyboard_project(client, seed=True)
    board = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})).json()
    frame = board["frames"][0]
    await draw_and_wait(client, pid, bid)

    plate = (
        await client.post(
            f"/api/projects/{pid}/assets",
            files={"file": ("plate.png", io.BytesIO(_png_bytes()), "image/png")},
        )
    ).json()
    await client.patch(
        f"/api/projects/{pid}/storyboards/{bid}/frames/{frame['id']}",
        json={"asset_id": plate["id"]},
    )

    state = (await client.get(f"/api/projects/{pid}/storyboards/{bid}/stills")).json()
    shown = state["frames"][frame["id"]]
    assert shown["source"] == "asset", "the picture the user chose is the frame's picture"
    assert shown["image"] == plate["path"]

    await draw_and_wait(client, pid, bid, frame_ids=[frame["id"]], reroll=True)

    state = (await client.get(f"/api/projects/{pid}/storyboards/{bid}/stills")).json()
    shown = state["frames"][frame["id"]]
    assert shown["source"] == "still", "the reroll is newer, so it is what the frame shows now"
    assert shown["image"] != plate["path"]


async def test_a_drawn_frame_becomes_an_asset(client):
    pid, bid, _d, _a = await storyboard_project(client)
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})
    built = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/stills")).json()
    await run_and_wait(client, pid, built["shot_id"])

    board = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()
    frame = board["frames"][0]
    captured = (
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frame['id']}/capture")
    ).json()

    assert captured["frame"]["status"] == "imaged"
    project = (await client.get(f"/api/projects/{pid}")).json()
    assert captured["asset_id"] in project["assets"]
    assert project["assets"][captured["asset_id"]]["kind"] == "image"


async def test_capturing_before_drawing_says_so(client):
    pid, bid, _d, _a = await storyboard_project(client)
    board = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})).json()

    response = await client.post(
        f"/api/projects/{pid}/storyboards/{bid}/frames/{board['frames'][0]['id']}/capture"
    )
    assert response.status_code == 422
    assert "not been drawn" in response.text


async def test_looking_at_the_still_rewrites_the_prompts(client):
    """The point of the vision pass: the motion prompt should describe the picture that exists."""
    pid, bid, _d, _a = await storyboard_project(client)
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})
    built = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/stills")).json()
    await run_and_wait(client, pid, built["shot_id"])

    board = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()
    frame_id = board["frames"][0]["id"]
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frame_id}/capture")

    described = (
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frame_id}/describe")
    ).json()

    assert described["status"] == "described"
    assert described["shot_prompt"] == SCRIPTED["describe"]["shot_prompt"]
    assert described["notes"] == SCRIPTED["describe"]["description"]
    # It was actually shown the image, rather than asked to imagine one.
    assert ScriptedProvider.calls[-1]["images"] == 1
    assert ScriptedProvider.calls[-1]["model"] == "looker"


async def test_a_frame_becomes_a_shot_with_its_still_wired_in(client):
    pid, bid, _draw, animate = await storyboard_project(client)
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})
    built = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/stills")).json()
    await run_and_wait(client, pid, built["shot_id"])

    board = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()
    frame = board["frames"][0]
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frame['id']}/capture")

    shot = (
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frame['id']}/shot")
    ).json()

    assert shot["name"] == frame["title"]
    step = shot["steps"][0]
    assert step["workflow_id"] == animate["id"]

    # Both of the frame's contributions arrive the same way: as nodes on the canvas wired into the
    # workflow's inputs, rather than as values buried in the step's parameters.
    nodes = {n["kind"]: n for n in shot["nodes"]}
    assert set(nodes) == {"media", "string"}
    assert nodes["string"]["value"] == frame["shot_prompt"]

    wired = {link["to_port"]: link["from_step"] for link in shot["links"]}
    assert wired["start_image"] == nodes["media"]["id"]
    assert wired["motion"] == nodes["string"]["id"]
    assert step["param_overrides"] == {}


async def test_the_built_shot_runs(client):
    """End to end: what the storyboard produced is ordinary work that executes."""
    pid, bid, _d, _a = await storyboard_project(client)
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})
    built = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/stills")).json()
    await run_and_wait(client, pid, built["shot_id"])

    board = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()
    frame = board["frames"][0]
    await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frame['id']}/capture")
    shot = (
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frame['id']}/shot")
    ).json()

    run = await run_and_wait(client, pid, shot["id"])
    assert run["status"] == "success", run.get("error")


async def test_building_a_shot_from_a_still_nobody_kept_keeps_it(client):
    """Keeping the still is bookkeeping, not a decision — so it is not something to refuse over.

    The picture is on screen the moment the step finishes, so requiring a press of *Keep* first was a dead
    end: the user has already seen the frame they are asking to animate.
    """
    pid, bid, _draw, animate = await storyboard_project(client)
    board = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})).json()
    frame = board["frames"][0]
    await draw_and_wait(client, pid, bid)

    shot = (
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frame['id']}/shot")
    ).json()

    assert shot["steps"][0]["workflow_id"] == animate["id"]
    # The still was kept on the way through, because a shot needs an asset to wire in.
    project = (await client.get(f"/api/projects/{pid}")).json()
    kept = project["assets"][shot["nodes"][0]["asset_id"]]
    assert kept["kind"] == "image"
    board = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()
    assert board["frames"][0]["asset_id"] == kept["id"]


async def test_a_shot_is_made_from_the_picture_the_frame_is_showing(client):
    """Draw, keep, reroll, animate: the fourth attempt is the one that moves, not the one kept earlier."""
    pid, bid, _d, _a = await storyboard_project(client, seed=True)
    board = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})).json()
    frame = board["frames"][0]
    await draw_and_wait(client, pid, bid)
    stale = (
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frame['id']}/capture")
    ).json()["asset_id"]

    await draw_and_wait(client, pid, bid, frame_ids=[frame["id"]], reroll=True)
    shot = (
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frame['id']}/shot")
    ).json()

    wired = shot["nodes"][0]["asset_id"]
    assert wired != stale, "a shot built after a reroll must animate the reroll"
    project = (await client.get(f"/api/projects/{pid}")).json()
    assert project["assets"][wired]["sha256"] != project["assets"][stale]["sha256"]


async def test_building_a_shot_before_drawing_says_so(client):
    pid, bid, _d, _a = await storyboard_project(client)
    board = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})).json()

    response = await client.post(
        f"/api/projects/{pid}/storyboards/{bid}/frames/{board['frames'][0]['id']}/shot"
    )
    assert response.status_code == 422
    assert "not been drawn" in response.text.lower()


async def test_drawing_without_saying_which_parameter_is_the_prompt_is_refused(client):
    """Guessing would run happily and ignore every prompt, which is worse than stopping."""
    project = await make_project(client)
    pid = project["id"]
    draw = await import_workflow(client, pid, "Draw", t2i_prompt())
    board = (
        await client.post(f"/api/projects/{pid}/storyboards", json={"premise": "x"})
    ).json()
    await client.patch(
        f"/api/projects/{pid}/storyboards/{board['id']}",
        json={"binding": {"image_workflow_id": draw["id"], "image_prompt_param": ""}},
    )

    response = await client.post(f"/api/projects/{pid}/storyboards/{board['id']}/stills")
    assert response.status_code == 422
    assert "prompt" in response.text.lower()


class TestFlatteningWhatTheModelSays:
    """Asked for a sentence, a model will happily answer with an object.

    Left alone, `str()` on that puts a Python dict repr — `{'subject': 'a lighthouse', ...}` — straight
    into the prompt that reaches ComfyUI: useless, and hard to spot because it *looks* like it worked.
    This is a real answer qwen2.5vl gave when asked to describe a frame.
    """

    def test_a_plain_string_is_left_alone(self):
        from comfywebstudio.llm.storywriter import as_text

        assert as_text("  a lighthouse at dusk  ") == "a lighthouse at dusk"

    def test_an_object_becomes_its_values_as_prose(self):
        from comfywebstudio.llm.storywriter import as_text

        assert as_text({"camera": "Wide shot", "movement": "Static"}) == "Wide shot. Static"

    def test_nesting_is_flattened_all_the_way_down(self):
        from comfywebstudio.llm.storywriter import as_text

        said = {"subject": "A lighthouse", "light": {"key": "a white beam", "fill": "moonlight"}}
        assert as_text(said) == "A lighthouse. a white beam. moonlight"

    def test_a_list_is_joined(self):
        from comfywebstudio.llm.storywriter import as_text

        assert as_text(["cold", "blue", "still"]) == "cold, blue, still"

    def test_nothing_becomes_an_empty_string_not_the_word_none(self):
        from comfywebstudio.llm.storywriter import as_text

        assert as_text(None) == ""

    def test_no_python_repr_survives_into_a_prompt(self):
        from comfywebstudio.llm.storywriter import as_text

        flattened = as_text({"subject": "a boat", "mood": "still"})
        assert "{" not in flattened and "'" not in flattened
