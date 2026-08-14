"""Editing the flow, and being able to see what it did.

The point of making the pipeline data was that a user can change what gets asked and then check what was
actually sent. So these tests go the whole way round: edit a stage over HTTP, run it, and read the
transcript back to confirm the edit is what reached the model — not merely that it was stored.

The scripted provider records every call, which is what makes "did my prompt arrive" a question with an
answer rather than a hope.
"""

from __future__ import annotations

import asyncio

from tests.test_api import run_and_wait
from tests.test_storyboard import (  # noqa: F401 - the autouse fixture travels with them
    ScriptedProvider,
    scripted_model,
    storyboard_project,
)


async def board_pipeline(client, pid: str, bid: str) -> dict:
    response = await client.get(f"/api/projects/{pid}/storyboards/{bid}/pipeline")
    assert response.status_code == 200, response.text
    return response.json()


def stage_of(payload: dict, stage_id: str) -> dict:
    return next(s for s in payload["stages"] if s["id"] == stage_id)


class TestSeeingTheFlow:
    async def test_the_whole_flow_is_listed_in_order(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        payload = await board_pipeline(client, pid, bid)
        assert [s["id"] for s in payload["stages"]] == [
            "write", "suggest_characters", "draw", "capture", "describe", "shot",
        ]

    async def test_a_stage_says_what_it_is_and_what_it_may_write_to(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        describe = stage_of(await board_pipeline(client, pid, bid), "describe")
        assert describe["kind"] == "llm" and describe["scope"] == "frame"
        assert describe["model"]["role"] == "vision"
        assert "frame.image_prompt" in describe["writable"]
        assert not describe["edited"] and not describe["stale"]

    async def test_the_tokens_a_template_may_use_come_with_it(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        tokens = (await board_pipeline(client, pid, bid))["tokens"]
        # Rendered rather than merely named, so the palette shows what a token is worth.
        assert tokens["board.premise"] == "A keeper sees a light at sea."
        assert tokens["board.style"] == "16mm"

    async def test_the_frame_tokens_appear_once_there_is_a_frame(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        assert "frame.image_prompt" not in (await board_pipeline(client, pid, bid))["tokens"]

        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={"frames": 2})
        tokens = (await board_pipeline(client, pid, bid))["tokens"]
        assert tokens["frame.image_prompt"] == "a lighthouse keeper at a window, dark sea beyond"

    async def test_the_builtin_is_readable_so_a_reset_can_be_previewed(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        response = await client.get(f"/api/projects/{pid}/storyboards/{bid}/pipeline/builtin")
        assert response.status_code == 200
        assert [s["id"] for s in response.json()["stages"]][0] == "write"


class TestEditingAStage:
    async def test_an_edited_system_prompt_is_what_reaches_the_model(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        stage = stage_of(await board_pipeline(client, pid, bid), "write")
        stage["system"] = "You write storyboards in limerick form."

        saved = await client.put(
            f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/write", json=stage
        )
        assert saved.status_code == 200, saved.text

        ScriptedProvider.calls = []
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={"frames": 2})
        assert ScriptedProvider.calls[-1]["system"] == "You write storyboards in limerick form."

    async def test_an_edited_user_prompt_is_rendered_with_the_board_in_it(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        stage = stage_of(await board_pipeline(client, pid, bid), "write")
        stage["prompt"] = "Premise: {board.premise}. Give me {count}."
        await client.put(f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/write", json=stage)

        ScriptedProvider.calls = []
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={"frames": 4})
        assert ScriptedProvider.calls[-1]["prompt"] == (
            "Premise: A keeper sees a light at sea.. Give me 4."
        )

    async def test_an_edited_stage_is_marked_as_edited(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        stage = stage_of(await board_pipeline(client, pid, bid), "describe")
        stage["system"] = "Look closely."
        payload = (
            await client.put(
                f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/describe", json=stage
            )
        ).json()

        assert stage_of(payload, "describe")["edited"]
        # And only that one: the rest still track the defaults, which is the whole point of an overlay.
        assert not stage_of(payload, "write")["edited"]

    async def test_a_stage_may_pin_its_own_temperature(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        stage = stage_of(await board_pipeline(client, pid, bid), "write")
        stage["model"]["temperature"] = 0.05
        await client.put(f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/write", json=stage)

        ScriptedProvider.calls = []
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})
        assert ScriptedProvider.calls[-1]["temperature"] == 0.05

    async def test_a_stage_with_no_temperature_of_its_own_follows_the_setting(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        client.app_state.settings.story.temperature = 0.33

        ScriptedProvider.calls = []
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})
        assert ScriptedProvider.calls[-1]["temperature"] == 0.33

    async def test_describing_now_honours_the_setting_too(self, client):
        # It used to quietly use its own hardcoded 0.4 while everything else read the setting.
        pid, bid, _d, _a = await storyboard_project(client)
        client.app_state.settings.story.temperature = 0.21
        await _draw_first_frame(client, pid, bid)

        frames = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]
        ScriptedProvider.calls = []
        await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/frames/{frames[0]['id']}/describe"
        )
        assert ScriptedProvider.calls[0]["temperature"] == 0.21

    async def test_a_stage_is_refused_if_it_writes_somewhere_it_cannot(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        stage = stage_of(await board_pipeline(client, pid, bid), "describe")
        stage["outputs"][0]["writes"] = "frame.asset_id"

        response = await client.put(
            f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/describe", json=stage
        )
        assert response.status_code == 422
        assert "not writable" in response.text

    async def test_a_board_stage_cannot_write_to_a_frame(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        stage = stage_of(await board_pipeline(client, pid, bid), "write")
        stage["outputs"][0]["writes"] = "frame.image_prompt"

        response = await client.put(
            f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/write", json=stage
        )
        assert response.status_code == 422
        assert "runs once for the whole board" in response.text

    async def test_saving_a_stage_under_the_wrong_name_is_refused(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        stage = stage_of(await board_pipeline(client, pid, bid), "write")
        response = await client.put(
            f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/describe", json=stage
        )
        assert response.status_code == 422


class TestResetting:
    async def test_resetting_one_stage_restores_the_default(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        original = stage_of(await board_pipeline(client, pid, bid), "write")["system"]

        stage = dict(original=original, **stage_of(await board_pipeline(client, pid, bid), "write"))
        stage.pop("original")
        stage["system"] = "Something else entirely."
        await client.put(f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/write", json=stage)

        payload = (
            await client.delete(f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/write")
        ).json()
        assert stage_of(payload, "write")["system"] == original
        assert not stage_of(payload, "write")["edited"]

    async def test_resetting_the_whole_pipeline_drops_every_edit(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        for stage_id in ("write", "describe"):
            stage = stage_of(await board_pipeline(client, pid, bid), stage_id)
            stage["system"] = "mine"
            await client.put(
                f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/{stage_id}", json=stage
            )

        payload = (await client.delete(f"/api/projects/{pid}/storyboards/{bid}/pipeline")).json()
        assert not any(s["edited"] for s in payload["stages"])


class TestTheAppDefaults:
    async def test_an_app_edit_reaches_a_board_that_never_edited_it(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        stage = stage_of(await board_pipeline(client, pid, bid), "write")
        stage["system"] = "House style."
        assert (await client.put("/api/settings/pipeline/stages/write", json=stage)).status_code == 200

        assert stage_of(await board_pipeline(client, pid, bid), "write")["system"] == "House style."

    async def test_a_board_edit_wins_over_the_app_one(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        stage = stage_of(await board_pipeline(client, pid, bid), "write")

        await client.put("/api/settings/pipeline/stages/write", json={**stage, "system": "app"})
        await client.put(
            f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/write",
            json={**stage, "system": "board"},
        )

        ScriptedProvider.calls = []
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})
        assert ScriptedProvider.calls[-1]["system"] == "board"

    async def test_patching_other_settings_does_not_wipe_the_pipeline(self, client):
        # `PATCH /api/settings` replaces whole sub-models, so a client changing a model name would
        # otherwise take every prompt edit on the install with it.
        pid, bid, _d, _a = await storyboard_project(client)
        stage = stage_of(await board_pipeline(client, pid, bid), "write")
        await client.put("/api/settings/pipeline/stages/write", json={**stage, "system": "keep me"})

        story = (await client.get("/api/settings")).json()["story"]
        story.pop("pipeline", None)
        story["temperature"] = 0.9
        assert (await client.patch("/api/settings", json={"story": story})).status_code == 200

        app = (await client.get("/api/settings/pipeline")).json()
        assert next(s for s in app["stages"] if s["id"] == "write")["system"] == "keep me"


class TestTheTranscript:
    async def test_running_a_stage_records_what_was_sent(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={"frames": 2})

        entries = (
            await client.get(f"/api/projects/{pid}/storyboards/{bid}/stage-runs")
        ).json()
        assert entries and entries[0]["stage_id"] == "write"
        assert entries[0]["status"] == "success"
        assert entries[0]["model"] == "writer"
        assert entries[0]["prompt_preview"]

    async def test_the_full_record_holds_the_prompt_and_the_reply(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})

        listed = (await client.get(f"/api/projects/{pid}/storyboards/{bid}/stage-runs")).json()
        full = (
            await client.get(
                f"/api/projects/{pid}/storyboards/{bid}/stage-runs/{listed[0]['id']}"
            )
        ).json()

        assert "A keeper sees a light at sea." in full["prompt"]
        assert full["system"].startswith("You are a storyboard artist")
        assert "The Answer" in full["reply"]
        assert full["schema_sent"]["properties"]["frames"]["type"] == "array"

    async def test_it_says_where_each_answer_went(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await _draw_first_frame(client, pid, bid)
        frames = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frames[0]['id']}/describe")

        listed = (
            await client.get(
                f"/api/projects/{pid}/storyboards/{bid}/stage-runs", params={"stage_id": "describe"}
            )
        ).json()
        full = (
            await client.get(
                f"/api/projects/{pid}/storyboards/{bid}/stage-runs/{listed[0]['id']}"
            )
        ).json()

        went = {w["target"]: w for w in full["writes"]}
        assert went["frame.image_prompt"]["applied"]
        assert went["frame.image_prompt"]["before"] != went["frame.image_prompt"]["after"]

    async def test_a_failed_stage_is_recorded_with_its_reason(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        client.app_state.settings.story.write_model = ""

        response = await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})
        assert response.status_code == 422

        # The provider is chosen before a transcript entry could be opened, so nothing is recorded — the
        # failure is the message, and there was no exchange to show.
        entries = (await client.get(f"/api/projects/{pid}/storyboards/{bid}/stage-runs")).json()
        assert all(e["status"] != "success" for e in entries)

    async def test_it_can_be_narrowed_to_one_frame(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await _draw_first_frame(client, pid, bid)
        frames = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frames[0]['id']}/describe")

        mine = (
            await client.get(
                f"/api/projects/{pid}/storyboards/{bid}/stage-runs",
                params={"frame_id": frames[0]["id"]},
            )
        ).json()
        assert mine and all(e["frame_id"] == frames[0]["id"] for e in mine)

    async def test_it_can_be_cleared(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={})
        assert (
            await client.delete(f"/api/projects/{pid}/storyboards/{bid}/stage-runs")
        ).status_code == 204
        assert (await client.get(f"/api/projects/{pid}/storyboards/{bid}/stage-runs")).json() == []

    async def test_drawing_records_the_prompts_it_sent_to_the_workflow(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await _draw_first_frame(client, pid, bid)

        listed = (
            await client.get(
                f"/api/projects/{pid}/storyboards/{bid}/stage-runs", params={"stage_id": "draw"}
            )
        ).json()
        assert listed and listed[0]["kind"] == "comfy"
        assert listed[0]["run_id"]
        assert listed[0]["prompt_preview"]


class TestRunningOneStage:
    async def test_a_stage_can_be_run_on_its_own(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        response = await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/write/run",
            json={"options": {"count": 2}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "success"
        assert len((await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]) == 2

    async def test_running_a_stage_that_does_not_exist_says_so(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        response = await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/nonsense/run", json={}
        )
        assert response.status_code == 404


async def _draw_first_frame(client, pid: str, bid: str) -> None:
    """Write the board and draw it, so there is a picture for the looking stages to look at."""
    from tests.test_storyboard import draw_and_wait

    await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={"frames": 2})
    await draw_and_wait(client, pid, bid)


class TestRunningTheWholeFlow:
    async def test_it_drives_the_stages_in_order(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={"frames": 2})

        run = await _run_pipeline(client, pid, bid, stage_ids=["draw", "capture", "describe"])
        assert run["status"] == "success", run.get("error")
        assert run["done"] == ["draw", "capture", "describe"]

    async def test_the_looking_stage_sees_the_picture_the_drawing_stage_made(self, client):
        # The whole reason the driver waits for the run rather than firing and forgetting.
        pid, bid, _d, _a = await storyboard_project(client)
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={"frames": 1})

        run = await _run_pipeline(client, pid, bid, stage_ids=["draw", "describe"])
        assert run["status"] == "success", run.get("error")

        described = (
            await client.get(
                f"/api/projects/{pid}/storyboards/{bid}/stage-runs", params={"stage_id": "describe"}
            )
        ).json()
        assert described and described[0]["status"] == "success"
        assert described[0]["image_count"] == 1

        frames = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]
        assert frames[0]["status"] == "described"
        assert frames[0]["notes"].startswith("A woman in a heavy coat")

    async def test_a_stage_that_fails_stops_the_rest(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={"frames": 1})

        # Nothing has been drawn, so capturing cannot work — and describe must not run afterwards.
        run = await _run_pipeline(client, pid, bid, stage_ids=["capture", "describe"])
        assert run["status"] == "error"
        assert "not been drawn" in run["error"]
        assert run["done"] == []

    async def test_a_failure_is_on_the_transcript_too(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={"frames": 1})
        await _run_pipeline(client, pid, bid, stage_ids=["capture"])

        entries = (
            await client.get(
                f"/api/projects/{pid}/storyboards/{bid}/stage-runs", params={"stage_id": "capture"}
            )
        ).json()
        assert entries and entries[0]["status"] == "error"
        assert "not been drawn" in entries[0]["error"]

    async def test_only_the_chosen_frames_are_worked_on(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={"frames": 2})
        frames = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]

        run = await _run_pipeline(
            client, pid, bid, stage_ids=["draw", "capture"], frame_ids=[frames[1]["id"]]
        )
        assert run["status"] == "success", run.get("error")

        after = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]
        assert after[0]["asset_id"] is None
        assert after[1]["asset_id"]

    async def test_a_second_run_on_the_same_board_is_refused(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={"frames": 2})

        first = await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/pipeline/run", json={"stage_ids": ["draw"]}
        )
        assert first.status_code == 202
        second = await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/pipeline/run", json={"stage_ids": ["draw"]}
        )
        assert second.status_code == 409
        assert "already running" in second.text
        await _await_pipeline(client, pid, bid, first.json()["id"])

    async def test_what_it_is_doing_can_be_asked_for(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={"frames": 1})
        started = (
            await client.post(
                f"/api/projects/{pid}/storyboards/{bid}/pipeline/run", json={"stage_ids": ["draw"]}
            )
        ).json()

        active = (await client.get(f"/api/projects/{pid}/storyboards/{bid}/pipeline/run")).json()
        assert active is not None and active["id"] == started["id"]

        await _await_pipeline(client, pid, bid, started["id"])
        assert (await client.get(f"/api/projects/{pid}/storyboards/{bid}/pipeline/run")).json() is None

    async def test_running_no_steps_at_all_says_so(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        response = await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/pipeline/run", json={"stage_ids": ["nonsense"]}
        )
        assert response.status_code == 422
        assert "None of those steps are on this storyboard." in response.text


async def _run_pipeline(client, pid: str, bid: str, **body) -> dict:
    response = await client.post(
        f"/api/projects/{pid}/storyboards/{bid}/pipeline/run",
        json={k: v for k, v in body.items() if v is not None},
    )
    assert response.status_code == 202, response.text
    return await _await_pipeline(client, pid, bid, response.json()["id"])


async def _await_pipeline(client, pid: str, bid: str, run_id: str) -> dict:
    import asyncio

    task = client.app_state.pipelines._tasks.get(run_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=60)
    return client.app_state.pipelines.get(run_id).model_dump(mode="json")


class TestChangingTheBinding:
    async def test_changing_the_drawing_half_leaves_the_animating_half_alone(self, client):
        # Two independent halves. A caller that rebinds the drawing workflow must not silently make the
        # board forget how to build a shot — which is exactly what a whole-model replace used to do.
        pid, bid, drawer, _a = await storyboard_project(client)
        await client.patch(
            f"/api/projects/{pid}/storyboards/{bid}",
            json={"binding": {"image_workflow_id": drawer["id"], "image_prompt_param": "prompt"}},
        )
        binding = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["binding"]
        assert binding["video_workflow_id"] is not None
        assert binding["video_image_port"] == "start_image"

    async def test_a_field_can_still_be_cleared_on_purpose(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await client.patch(
            f"/api/projects/{pid}/storyboards/{bid}",
            json={"binding": {"video_image_port": ""}},
        )
        binding = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["binding"]
        assert binding["video_image_port"] == ""
        assert binding["video_prompt_param"] == "motion"


class TestSomewhereOfMyOwn:
    async def test_a_new_output_lands_in_a_custom_field(self, client):
        # The claim "you can edit the output shape" is only true if a new field has somewhere to go.
        pid, bid, _d, _a = await storyboard_project(client)
        stage = stage_of(await board_pipeline(client, pid, bid), "write")
        stage["outputs"][0]["fields"].append({
            "key": "wardrobe", "type": "string", "description": "what they are wearing",
            "required": False, "fields": [], "writes": "",
        })
        assert (
            await client.put(
                f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/write", json=stage
            )
        ).status_code == 200

        # It reaches the model as part of the schema…
        ScriptedProvider.calls = []
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/write", json={"frames": 2})
        asked = ScriptedProvider.calls[-1]["schema"]["properties"]["frames"]["items"]["properties"]
        assert "wardrobe" in asked

        # …and a per-frame stage can write one of its own onto the frame.
        describe = stage_of(await board_pipeline(client, pid, bid), "describe")
        describe["outputs"].append({
            "key": "mood", "type": "string", "description": "the mood",
            "required": False, "fields": [], "writes": "frame.fields.mood",
        })
        assert (
            await client.put(
                f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/describe", json=describe
            )
        ).status_code == 200

    async def test_a_custom_field_is_written_and_becomes_a_token(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await _draw_first_frame(client, pid, bid)

        describe = stage_of(await board_pipeline(client, pid, bid), "describe")
        describe["outputs"].append({
            "key": "description", "type": "string", "description": "the mood",
            "required": True, "fields": [], "writes": "frame.fields.mood",
        })
        await client.put(
            f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/describe", json=describe
        )

        frames = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frames[0]['id']}/describe")

        after = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"][0]
        assert after["fields"]["mood"].startswith("A woman in a heavy coat")
        # And it is immediately usable in another step's template.
        tokens = (await board_pipeline(client, pid, bid))["tokens"]
        assert tokens["frame.fields.mood"] == after["fields"]["mood"]

    async def test_a_field_name_that_would_not_work_as_a_token_is_refused(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        describe = stage_of(await board_pipeline(client, pid, bid), "describe")
        describe["outputs"][0]["writes"] = "frame.fields.Not A Name"

        response = await client.put(
            f"/api/projects/{pid}/storyboards/{bid}/pipeline/stages/describe", json=describe
        )
        assert response.status_code == 422
        assert "lowercase letters" in response.text


class TestTheMotionPromptReachesTheWorkflow:
    async def test_it_arrives_as_a_node_wired_into_the_chosen_input(self, client):
        # On the canvas next to the still it belongs to, rather than hidden in the step's parameters
        # where it cannot be seen, shared, or re-wired without being retyped.
        pid, bid, _d, _a = await storyboard_project(client)
        await _draw_first_frame(client, pid, bid)
        frames = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]

        made = (
            await client.post(
                f"/api/projects/{pid}/storyboards/{bid}/frames/{frames[0]['id']}/shot"
            )
        ).json()

        text = next(n for n in made["nodes"] if n["kind"] == "string")
        assert text["value"] == frames[0]["shot_prompt"]
        assert any(
            link["from_step"] == text["id"] and link["to_port"] == "motion"
            for link in made["links"]
        )
        assert made["steps"][0]["param_overrides"] == {}

    async def test_the_wired_prompt_is_what_the_step_actually_runs(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await _draw_first_frame(client, pid, bid)
        frames = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]
        made = (
            await client.post(
                f"/api/projects/{pid}/storyboards/{bid}/frames/{frames[0]['id']}/shot"
            )
        ).json()

        # A node on the canvas is only better than a parameter if it genuinely reaches the workflow.
        run = await run_and_wait(client, pid, made["id"])
        assert run["status"] == "success", run.get("error")

    async def test_a_prompt_with_nowhere_to_go_is_refused_rather_than_dropped(self, client):
        # The shot would otherwise be built and returned as a success, animating the still while ignoring
        # every word written about how it moves.
        pid, bid, _d, _a = await storyboard_project(client)
        await client.patch(
            f"/api/projects/{pid}/storyboards/{bid}", json={"binding": {"video_prompt_param": ""}}
        )
        await _draw_first_frame(client, pid, bid)
        frames = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]

        response = await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/frames/{frames[0]['id']}/shot"
        )
        assert response.status_code == 422
        assert "no parameter of 'Animate' is set to receive it" in response.text

    async def test_a_frame_with_no_motion_prompt_still_builds(self, client):
        # Nothing to lose, so nothing to refuse — a workflow that takes only a starting image is fine.
        pid, bid, _d, _a = await storyboard_project(client)
        await client.patch(
            f"/api/projects/{pid}/storyboards/{bid}", json={"binding": {"video_prompt_param": ""}}
        )
        await _draw_first_frame(client, pid, bid)
        frames = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]
        await client.patch(
            f"/api/projects/{pid}/storyboards/{bid}/frames/{frames[0]['id']}",
            json={"shot_prompt": "", "image_prompt": ""},
        )

        response = await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/frames/{frames[0]['id']}/shot"
        )
        assert response.status_code == 201, response.text

    async def test_the_setup_panel_is_warned_before_it_gets_that_far(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await client.patch(
            f"/api/projects/{pid}/storyboards/{bid}", json={"binding": {"video_prompt_param": ""}}
        )
        warnings = " ".join(
            (await client.get(f"/api/projects/{pid}/storyboards/{bid}/surfaces")).json()["warnings"]
        )
        assert "receive the motion prompt" in warnings

    async def test_the_prompt_picker_offers_text_parameters_only(self, client):
        # It used to list every parameter, so a seed or a width was one careless click away from being
        # handed a sentence.
        pid, bid, _d, _a = await storyboard_project(client, seed=True)
        surfaces = (await client.get(f"/api/projects/{pid}/storyboards/{bid}/surfaces")).json()
        offered = {p["key"] for p in surfaces["image"]["text_params"]}
        assert offered == {"prompt"}
        assert "seed" not in offered


class TestRebuildingAShot:
    async def test_deleting_the_shot_lets_the_frame_make_another(self, client):
        # The frame remembers its shot so it cannot quietly build a second one. Deleting the shot has to
        # undo that memory, or the frame is stuck saying it is finished and refusing to do anything.
        pid, bid, _d, _a = await storyboard_project(client)
        await _draw_first_frame(client, pid, bid)
        frames = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]
        fid = frames[0]["id"]

        made = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{fid}/shot")).json()
        after = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"][0]
        assert after["shot_id"] == made["id"] and after["status"] == "shot"

        assert (await client.delete(f"/api/projects/{pid}/shots/{made['id']}")).status_code == 204

        freed = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"][0]
        assert freed["shot_id"] is None
        assert freed["status"] != "shot"

        again = await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{fid}/shot")
        assert again.status_code == 201, again.text
        assert again.json()["id"] != made["id"]

    async def test_a_frame_that_still_has_its_shot_is_left_alone(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await _draw_first_frame(client, pid, bid)
        frames = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]

        keep = (
            await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frames[0]['id']}/shot")
        ).json()
        go = (
            await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{frames[1]['id']}/shot")
        ).json()
        await client.delete(f"/api/projects/{pid}/shots/{go['id']}")

        after = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]
        assert after[0]["shot_id"] == keep["id"]
        assert after[1]["shot_id"] is None

    async def test_the_status_goes_back_to_what_the_frame_can_still_show(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await _draw_first_frame(client, pid, bid)
        frames = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]
        fid = frames[0]["id"]

        # Described first, so there is something better than "drawn" to fall back to.
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{fid}/describe")
        made = (await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{fid}/shot")).json()
        await client.delete(f"/api/projects/{pid}/shots/{made['id']}")

        after = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"][0]
        assert after["status"] == "described"

    async def test_rebuilding_is_still_refused_while_the_shot_is_there(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await _draw_first_frame(client, pid, bid)
        frames = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()["frames"]
        fid = frames[0]["id"]

        await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{fid}/shot")
        again = await client.post(f"/api/projects/{pid}/storyboards/{bid}/frames/{fid}/shot")
        assert again.status_code == 422
        assert "Delete that shot to rebuild it" in again.text


class TestFreeingTheGraphicsCard:
    """The flow alternates between a language model and ComfyUI, and they want the same memory."""

    @staticmethod
    def _holding(monkeypatch, models):
        """Make the scripted provider report — and release — some resident models."""
        from comfywebstudio.llm.provider import LoadedModel

        held = list(models)

        async def loaded(self):
            return [LoadedModel(name=n, vram=v) for n, v in held]

        async def unload(self, model=None):
            gone = [n for n, _ in held if model is None or n == model]
            held[:] = [(n, v) for n, v in held if n not in gone]
            return gone

        monkeypatch.setattr(ScriptedProvider, "loaded", loaded, raising=False)
        monkeypatch.setattr(ScriptedProvider, "unload", unload, raising=False)
        return held

    async def test_it_reports_what_is_in_memory(self, client, monkeypatch):
        self._holding(monkeypatch, [("writer", 4_000_000_000), ("looker", 3_300_000_000)])

        body = (await client.get("/api/settings/llm-loaded")).json()
        assert [m["name"] for m in body["models"]] == ["writer", "looker"]
        assert body["vram"] == 7_300_000_000

    async def test_it_releases_everything_and_says_what_went(self, client, monkeypatch):
        held = self._holding(monkeypatch, [("writer", 4_000_000_000)])

        freed = (await client.post("/api/settings/llm-unload", json={})).json()
        assert freed["unloaded"] == ["writer"]
        assert held == []
        assert (await client.get("/api/settings/llm-loaded")).json()["models"] == []

    async def test_one_model_can_be_released_on_its_own(self, client, monkeypatch):
        self._holding(monkeypatch, [("writer", 1), ("looker", 2)])

        freed = (await client.post("/api/settings/llm-unload", json={"model": "looker"})).json()
        assert freed["unloaded"] == ["looker"]
        assert [m["name"] for m in (await client.get("/api/settings/llm-loaded")).json()["models"]] == [
            "writer"
        ]

    async def test_nothing_loaded_is_not_an_error(self, client, monkeypatch):
        self._holding(monkeypatch, [])
        assert (await client.post("/api/settings/llm-unload", json={})).json()["unloaded"] == []

    async def test_a_provider_that_cannot_free_its_own_memory_says_so(self, client):
        # The base implementation refuses, which is what a remote OpenAI-compatible server should do:
        # its memory is not ours to manage.
        response = await client.post("/api/settings/llm-unload", json={})
        assert response.status_code == 422
        assert "not ours to free" in response.text

    async def test_asking_what_is_loaded_never_fails(self, client):
        # A provider that cannot answer reports nothing rather than breaking the panel it is drawn in.
        assert (await client.get("/api/settings/llm-loaded")).json() == {"models": [], "vram": 0}


class TestFindingCharactersTwice:
    async def test_somebody_already_on_the_board_is_not_offered_again(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        first = (
            await client.post(f"/api/projects/{pid}/storyboards/{bid}/characters/suggest")
        ).json()
        assert [c["name"] for c in first] == ["Elara"]

        await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/characters", json={"name": "Elara"}
        )
        again = (
            await client.post(f"/api/projects/{pid}/storyboards/{bid}/characters/suggest")
        ).json()
        assert again == []

    async def test_matching_ignores_case_and_spacing(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/characters", json={"name": "  elara  "}
        )
        assert (
            await client.post(f"/api/projects/{pid}/storyboards/{bid}/characters/suggest")
        ).json() == []

    async def test_the_same_name_twice_in_one_answer_is_one_character(self, client, monkeypatch):
        # A model listing someone twice used to make two rows: the batch was only ever compared with the
        # board, never with itself.
        import json as _json

        from comfywebstudio.llm.provider import Reply

        async def doubled(self, prompt, *, model, system="", images=None, json_object=False,
                          schema=None, temperature=0.7):
            return Reply(
                text=_json.dumps({"characters": [
                    {"name": "Elara", "description": "the keeper", "appearance": "grey coat"},
                    {"name": "elara", "description": "her again", "appearance": "grey coat"},
                    {"name": "The light", "description": "out at sea", "appearance": "a glow"},
                ]}),
                model=model,
            )

        monkeypatch.setattr(ScriptedProvider, "complete", doubled)
        pid, bid, _d, _a = await storyboard_project(client)
        found = (
            await client.post(f"/api/projects/{pid}/storyboards/{bid}/characters/suggest")
        ).json()
        assert [c["name"] for c in found] == ["Elara", "The light"]

    async def test_the_model_is_told_who_is_already_known(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/characters", json={"name": "Elara"}
        )

        ScriptedProvider.calls = []
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/characters/suggest")
        sent = ScriptedProvider.calls[-1]["prompt"]
        assert "Do not list them again" in sent and "Elara" in sent

    async def test_with_nobody_known_the_prompt_does_not_mention_it(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        ScriptedProvider.calls = []
        await client.post(f"/api/projects/{pid}/storyboards/{bid}/characters/suggest")
        assert "Do not list them again" not in ScriptedProvider.calls[-1]["prompt"]


class TestDrawingACharacter:
    async def test_it_draws_them_and_keeps_the_picture_as_their_reference(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        character = (
            await client.post(
                f"/api/projects/{pid}/storyboards/{bid}/characters",
                json={"name": "Elara", "appearance": "a keeper in a grey coat"},
            )
        ).json()

        started = await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/characters/{character['id']}/portrait"
        )
        assert started.status_code == 202, started.text
        await asyncio.wait_for(
            client.app_state.orchestrator.wait(started.json()["run_id"]), timeout=30
        )
        # Keeping it happens in a task of its own once the picture exists.
        for _ in range(50):
            await asyncio.sleep(0.05)
            board = (await client.get(f"/api/projects/{pid}/storyboards/{bid}")).json()
            if board["characters"][0]["reference_asset_ids"]:
                break

        assert board["characters"][0]["reference_asset_ids"], "the portrait was never kept"
        project = (await client.get(f"/api/projects/{pid}")).json()
        asset_id = board["characters"][0]["reference_asset_ids"][0]
        assert project["assets"][asset_id]["kind"] == "image"
        assert "Elara" in project["assets"][asset_id]["name"]

    async def test_the_prompt_is_what_they_look_like_plus_the_house_style(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        character = (
            await client.post(
                f"/api/projects/{pid}/storyboards/{bid}/characters",
                json={"name": "Elara", "appearance": "a keeper in a grey coat"},
            )
        ).json()
        started = (
            await client.post(
                f"/api/projects/{pid}/storyboards/{bid}/characters/{character['id']}/portrait"
            )
        ).json()

        project = (await client.get(f"/api/projects/{pid}")).json()
        shot = next(s for s in project["shots"] if s["id"] == started["shot_id"])
        step = next(s for s in shot["steps"] if s["id"] == started["step_id"])
        # The board's style is "16mm"; the character's appearance is theirs.
        assert step["param_overrides"]["prompt"] == "a keeper in a grey coat 16mm"
        assert step["name"] == character["id"]

    async def test_drawing_again_reuses_the_same_step(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        character = (
            await client.post(
                f"/api/projects/{pid}/storyboards/{bid}/characters",
                json={"name": "Elara", "appearance": "grey coat"},
            )
        ).json()
        one = (await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/characters/{character['id']}/portrait")).json()
        two = (await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/characters/{character['id']}/portrait")).json()
        assert one["step_id"] == two["step_id"]

    async def test_somebody_with_no_appearance_written_is_refused(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        character = (
            await client.post(
                f"/api/projects/{pid}/storyboards/{bid}/characters", json={"name": "Nobody"}
            )
        ).json()

        response = await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/characters/{character['id']}/portrait"
        )
        assert response.status_code == 422
        assert "no appearance written" in response.text

    async def test_drawing_a_character_that_is_not_there_says_so(self, client):
        pid, bid, _d, _a = await storyboard_project(client)
        response = await client.post(
            f"/api/projects/{pid}/storyboards/{bid}/characters/char_nope/portrait"
        )
        assert response.status_code == 404
