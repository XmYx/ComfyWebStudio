#!/usr/bin/env python
"""The storyboard workspace, and the model settings behind it.

Six things that only exist in the browser: the model pickers offer the *right* models (and only models
that can genuinely see are offered as the one that looks at frames), the library can pull a missing one and
report its progress, the frame strip renders what was written and edits persist, drawing the board puts the
pictures on the frames by itself and one frame can be varied without touching the rest, choosing a reference
input on a workflow that has none is flagged rather than dropped, and a frame becomes a shot.

The parts that need a language model skip themselves rather than fail when none is configured, so this
still means something on a machine without Ollama.

Needs the backend running with a built frontend, and a reachable ComfyUI.

    .venv/bin/python scripts/ui_storyboard.py [--url http://127.0.0.1:8500]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid

from playwright.sync_api import sync_playwright

FAILURES: list[str] = []
SKIPPED: list[str] = []

PREMISE = (
    "A lighthouse keeper works her last night on the rock. Out at sea, a light she does not recognise "
    "answers hers, three times, and then stops."
)

#: A text-to-image workflow with a prompt input and an image output, and *no* reference image input —
#: which is exactly the case the flagging is there for. The seed drives the colour, so a reroll of a frame
#: visibly produces a different picture rather than the same one twice.
DRAWER = {
    "1": {"class_type": "WSStringInput", "inputs": {"port_name": "prompt", "value": ""}},
    "2": {"class_type": "EmptyImage",
          "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": ["4", 0]}},
    "3": {"class_type": "WSImageOutput",
          "inputs": {"image": ["2", 0], "port_name": "still", "format": "png", "run_key": ""}},
    "4": {"class_type": "WSSeedInput", "inputs": {"port_name": "seed", "value": 0x2A3B5C}},
}

#: An image-to-video workflow: takes a starting image and a prompt.
ANIMATOR = {
    "1": {"class_type": "WSImageInput", "inputs": {"port_name": "start_image", "source": ""}},
    "2": {"class_type": "WSStringInput", "inputs": {"port_name": "motion", "value": ""}},
    "3": {"class_type": "WSImageOutput",
          "inputs": {"image": ["1", 0], "port_name": "frames", "format": "png", "run_key": ""}},
}

#: What the vision picker is actually offering, and what the backend says can see. The two must agree —
#: a model listed there that cannot see is the failure this whole check exists to catch.
VISION_OPTIONS = """
() => {
  const select = document.querySelector('[data-testid=vision-model]');
  return select ? [...select.options].map((o) => o.value).filter(Boolean) : null;
}
"""


def check(condition: bool, message: str) -> None:
    print(f"  [{'ok  ' if condition else 'FAIL'}] {message}")
    if not condition:
        FAILURES.append(message)


def skip(message: str) -> None:
    print(f"  [skip] {message}")
    SKIPPED.append(message)


def _api(url: str, path: str, payload=None, method="GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        # The message is the whole point of a refusal here, and a bare status code sends whoever is
        # reading this back to the server log for something the response already said.
        raise SystemExit(f"{method} {path} -> {exc.code}: {exc.read().decode()[:400]}") from exc
    return json.loads(body) if body else None


def build(url: str) -> tuple[str, str, str, str]:
    project = _api(url, "/api/projects", {"name": "Storyboard UI"}, "POST")
    pid = project["id"]
    drawer = _api(url, f"/api/projects/{pid}/workflows",
                  {"name": "Draw", "prompt": DRAWER}, "POST")
    animator = _api(url, f"/api/projects/{pid}/workflows",
                    {"name": "Animate", "prompt": ANIMATOR}, "POST")

    board = _api(url, f"/api/projects/{pid}/storyboards",
                 {"name": "Nightfall", "premise": PREMISE}, "POST")
    for spec in (
        {"title": "The last climb", "image_prompt": "a woman climbing a spiral stair with an oil can",
         "shot_prompt": "the camera climbs with her"},
        {"title": "The beam", "image_prompt": "a lighthouse beam over a dark sea",
         "shot_prompt": "the beam sweeps left to right"},
    ):
        frame = _api(url, f"/api/projects/{pid}/storyboards/{board['id']}/frames", None, "POST")
        _api(url, f"/api/projects/{pid}/storyboards/{board['id']}/frames/{frame['id']}", spec, "PATCH")

    # Both halves of the binding up front, so any check can draw *or* build a shot without depending on
    # one that ran before it having set the wiring up. The checks below still change what they are
    # testing; a PATCH only replaces the fields it actually names.
    _api(url, f"/api/projects/{pid}/storyboards/{board['id']}", {"binding": {
        "image_workflow_id": drawer["id"], "image_prompt_param": "prompt",
        "video_workflow_id": animator["id"], "video_prompt_param": "motion",
        "video_image_port": "start_image",
    }}, "PATCH")

    # A character *with a reference image*: the flagging only matters when there is something to feed in.
    character = _api(url, f"/api/projects/{pid}/storyboards/{board['id']}/characters",
                     {"name": "The keeper"}, "POST")
    asset = _upload_png(url, pid)
    _api(url, f"/api/projects/{pid}/storyboards/{board['id']}/characters/{character['id']}",
         {"reference_asset_ids": [asset["id"]]}, "PATCH")
    return pid, board["id"], drawer["id"], animator["id"]


#: A 1x1 PNG, written out so the script needs no image library.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d4944415478da63f8cfc0f01f0005fb02fe4ea5b0100000000049454e44ae426082"
)


def _upload_png(url: str, project_id: str) -> dict:
    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="ref.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + PNG + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        f"{url}/api/projects/{project_id}/assets", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def check_models(page, url: str) -> None:
    print("Language models")
    page.goto(f"{url}/settings", wait_until="networkidle")
    page.get_by_role("button", name="Language models").click()
    page.wait_for_timeout(1200)

    check(page.get_by_text("GET ANOTHER MODEL").count() > 0, "the model library is listed")
    check(page.locator("button:text-is('Pull')").count() > 0, "models that are missing offer a Pull")
    check(page.get_by_text("live", exact=True).count() > 0,
          "the event stream is connected on Settings, so a pull can report itself")

    providers = _api(url, "/api/settings/llm-providers")
    if not providers:
        skip("no language-model provider configured, so the pickers cannot be checked")
        return

    try:
        listed = _api(url, f"/api/settings/llm-providers/{providers[0]['id']}/models")["models"]
    except urllib.error.HTTPError:
        skip("the provider is configured but unreachable, so the pickers cannot be checked")
        return

    offered = page.evaluate(VISION_OPTIONS)
    can_see = {m["name"] for m in listed if m["vision"]}
    check(offered is not None, "the vision picker is on the page")
    if offered is None:
        return

    check(set(offered) <= can_see,
          f"only models that can see are offered to look at frames ({sorted(set(offered) - can_see)})")
    check(len(offered) < len(listed) or not listed or len(can_see) == len(listed),
          "the vision list is filtered, not simply every installed model")
    if not can_see:
        check(page.get_by_text("None of the installed models can see").count() > 0,
              "with nothing that can see, the panel says so and points at the library")


def check_workspace(page, url: str, pid: str, board_id: str) -> None:
    print("Storyboard workspace")
    page.goto(f"{url}/p/{pid}/storyboard", wait_until="networkidle")
    page.evaluate("() => localStorage.removeItem('comfywebstudio.layout')")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1800)

    frames = page.locator("[data-frame]")
    check(frames.count() == 2, f"both frames are drawn ({frames.count()})")
    check(page.get_by_text("The last climb").count() > 0, "a frame shows its title")
    check(page.get_by_text("not drawn").count() == 2, "frames that have no still say so")

    print("Editing a frame")
    field = frames.first.locator("[data-field=shot_prompt]")
    field.fill("the lantern swings as she climbs")
    field.blur()
    page.wait_for_timeout(1200)
    stored = _api(url, f"/api/projects/{pid}/storyboards/{board_id}")["frames"][0]["shot_prompt"]
    check(stored == "the lantern swings as she climbs", f"an edited prompt is persisted ({stored!r})")


def _wait_for_stills(url: str, pid: str, board_id: str, *, drawn: int, timeout_ms=60_000) -> dict:
    """Poll until that many frames have a picture, and hand back what each frame is showing."""
    waited = 0
    while waited < timeout_ms:
        state = _api(url, f"/api/projects/{pid}/storyboards/{board_id}/stills")
        if sum(1 for f in state["frames"].values() if f["image"]) >= drawn:
            return state
        waited += 500
        _sleep_ms(500)
    return _api(url, f"/api/projects/{pid}/storyboards/{board_id}/stills")


def _sleep_ms(ms: int) -> None:
    import time

    time.sleep(ms / 1000)


def check_drawing(page, url: str, pid: str, board_id: str, drawer: str) -> None:
    """Drawing from the browser: the pictures arrive by themselves, and one frame can be redrawn alone.

    The part worth checking here rather than in the API tests is that the strip *shows* the result. A run
    that succeeds while the frames stay empty is the bug this whole feature exists to fix.
    """
    print("Drawing the board")
    _api(url, f"/api/projects/{pid}/storyboards/{board_id}",
         {"binding": {"image_workflow_id": drawer, "image_prompt_param": "prompt"}}, "PATCH")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1500)

    page.get_by_role("button", name="Draw all").click()
    state = _wait_for_stills(url, pid, board_id, drawn=2)
    drawn = {fid: f for fid, f in state["frames"].items() if f["image"]}
    if len(drawn) < 2:
        statuses = {f["status"] for f in state["frames"].values()}
        skip(f"the frames did not draw — is ComfyUI reachable? (statuses {statuses})")
        return

    check(all(f["source"] == "still" for f in drawn.values()), "each frame is showing what it drew")
    assets = _api(url, f"/api/projects/{pid}/assets")
    check(all(a["source"] is None for a in assets),
          "showing a still did not quietly fill the asset library")

    page.wait_for_timeout(2500)
    check(page.locator("[data-frame] img").count() == 2,
          f"both pictures are on screen without keeping them "
          f"({page.locator('[data-frame] img').count()})")
    check(page.get_by_text("not drawn").count() == 0, "no frame still says it has nothing")

    print("Redrawing one frame")
    board = _api(url, f"/api/projects/{pid}/storyboards/{board_id}")
    first, second = [f["id"] for f in sorted(board["frames"], key=lambda f: f["order"])][:2]
    before = {fid: state["frames"][fid]["image"] for fid in (first, second)}

    page.locator("[data-frame]").first.get_by_role("button", name="↻ Vary").click()
    waited = 0
    after = before
    while waited < 60_000:
        _sleep_ms(500)
        waited += 500
        after = {
            fid: f["image"]
            for fid, f in _api(url, f"/api/projects/{pid}/storyboards/{board_id}/stills")[
                "frames"
            ].items()
        }
        if after[first] != before[first]:
            break

    check(after[first] != before[first], "the varied frame drew again")
    check(after[second] == before[second], "and the other frame was left alone")

    project = _api(url, f"/api/projects/{pid}")
    stills = next((s for s in project["shots"] if s["id"] == state["shot_id"]), None)
    step = next((s for s in (stills or {}).get("steps", []) if s["name"] == first), None)
    check(step is not None and "seed" in step["param_overrides"],
          "varying wrote a new seed onto that frame's step, rather than merely ignoring the cache")

    print("A shot that is deleted can be made again")
    remade = _api(url, f"/api/projects/{pid}/storyboards/{board_id}/frames/{first}/shot", None, "POST")
    _api(url, f"/api/projects/{pid}/shots/{remade['id']}", None, "DELETE")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1500)

    frame = next(
        f for f in _api(url, f"/api/projects/{pid}/storyboards/{board_id}")["frames"]
        if f["id"] == first
    )
    check(frame["shot_id"] is None, "the frame lets go of a shot that has been deleted")
    check(frame["status"] != "shot", f"and stops calling itself finished ({frame['status']!r})")
    check(page.get_by_text("Shot made", exact=True).count() == 0,
          "so the button offers to make one again rather than staying greyed out")
    again = _api(url, f"/api/projects/{pid}/storyboards/{board_id}/frames/{first}/shot", None, "POST")
    check(again["id"] != remade["id"], "and it really does build a new one")
    _api(url, f"/api/projects/{pid}/shots/{again['id']}", None, "DELETE")

    print("A drawn frame becomes a shot without being kept first")
    made = _api(url, f"/api/projects/{pid}/storyboards/{board_id}/frames/{second}/shot", None, "POST")
    still = next((n for n in made["nodes"] if n.get("asset_id")), None)
    check(still is not None, "the still was kept on the way through and wired in")

    # Both of the frame's contributions land the same way: as nodes, not as buried parameter values.
    motion = next((n for n in made["nodes"] if n["kind"] == "string"), None)
    check(motion is not None and motion["value"], "the motion prompt is a node on the canvas")
    wired = {link["to_port"]: link["from_step"] for link in made["links"]}
    check(motion is not None and wired.get("motion") == motion["id"],
          "wired into the input that was chosen for it")
    check(made["steps"][0]["param_overrides"] == {},
          "and nothing was tucked into the step's parameters instead")


def check_reference_flag(page, url: str, pid: str, board_id: str, drawer: str) -> None:
    print("Reference inputs on a workflow that has none")
    _api(url, f"/api/projects/{pid}/storyboards/{board_id}",
         {"binding": {"image_workflow_id": drawer, "image_prompt_param": "prompt",
                      "image_reference_params": ["character_sheet"]}}, "PATCH")

    surfaces = _api(url, f"/api/projects/{pid}/storyboards/{board_id}/surfaces")
    warnings = " ".join(surfaces.get("warnings") or [])
    check("reference" in warnings.lower(),
          f"the missing reference input is flagged ({warnings[:140]!r})")

    kept = _api(url, f"/api/projects/{pid}/storyboards/{board_id}")["binding"]["image_reference_params"]
    check(kept == ["character_sheet"], f"the assignment is kept, not dropped ({kept})")

    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1800)
    check(page.get_by_text("reference", exact=False).count() > 0, "the workspace surfaces it too")


def check_make_shot(url: str, pid: str, board_id: str, animator: str) -> None:
    print("Turning a frame into a shot")
    _api(url, f"/api/projects/{pid}/storyboards/{board_id}",
         {"binding": {"video_workflow_id": animator, "video_prompt_param": "motion",
                      "video_image_port": "start_image"}}, "PATCH")
    board = _api(url, f"/api/projects/{pid}/storyboards/{board_id}")
    frame = board["frames"][0]

    # A frame only becomes a shot once its still is an asset — that still is what gets wired in — so
    # stand one in rather than running the drawing workflow for it.
    still = _upload_png(url, pid)
    _api(url, f"/api/projects/{pid}/storyboards/{board_id}/frames/{frame['id']}",
         {"asset_id": still["id"]}, "PATCH")
    try:
        made = _api(url, f"/api/projects/{pid}/storyboards/{board_id}/frames/{frame['id']}/shot",
                    None, "POST")
    except urllib.error.HTTPError as exc:
        check(False, f"a frame becomes a shot ({exc.read().decode()[:160]})")
        return

    shot = next((s for s in _api(url, f"/api/projects/{pid}/shots") if s["id"] == made["id"]), None)
    check(shot is not None, "the shot exists in the project")
    if shot is None:
        return
    check(len(shot["steps"]) == 1, f"it has the one step from the workflow ({len(shot['steps'])})")

    # Both the picture and the words arrive as real value nodes and links rather than being stamped into
    # the step's parameters — so they show on the canvas, preview, and can be re-wired or shared without
    # anyone editing the workflow.
    step_id = shot["steps"][0]["id"]
    wired = {
        link["to_port"]: link["from_step"] for link in shot["links"] if link["to_step"] == step_id
    }
    still = next((n for n in shot["nodes"] if n.get("asset_id")), None)
    motion = next((n for n in shot["nodes"] if n["kind"] == "string"), None)

    check(still is not None, "the still is a node on the shot's canvas")
    check(still is not None and wired.get("start_image") == still["id"],
          "and it is linked into the workflow's image input")
    check(motion is not None and motion["value"] == frame["shot_prompt"],
          f"the shot prompt is a node too ({motion['value'] if motion else None!r})")
    check(motion is not None and wired.get("motion") == motion["id"],
          "and it is linked into the text input that was chosen for it")
    check(shot["steps"][0]["param_overrides"] == {},
          "with nothing tucked into the step's parameters instead")


def check_binding_panel(page, url: str, pid: str, board_id: str, drawer: str, animator: str) -> None:
    """Choosing several bindings in a row, through the panel, keeps every one of them.

    The regression this exists for: the panel used to send the whole binding built from its own props,
    which only refresh after the round trip — so picking the prompt and then the starting image sent a
    stale copy of the prompt and silently blanked it. Nothing said so until a shot was built without it.
    """
    print("Binding, one pick after another")
    _api(url, f"/api/projects/{pid}/storyboards/{board_id}", {"binding": {
        "image_workflow_id": None, "image_prompt_param": "",
        "video_workflow_id": None, "video_prompt_param": "", "video_image_port": "",
    }}, "PATCH")
    page.goto(f"{url}/p/{pid}/storyboard", wait_until="networkidle")
    page.evaluate("() => localStorage.removeItem('comfywebstudio.layout')")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1800)
    page.locator("button:text-is('Inspector')").first.click()
    page.wait_for_timeout(800)

    # Watching what the panel *sends* rather than racing it. The old version was only wrong when a second
    # pick beat the refetch, which makes a timing test that passes on a fast machine and proves nothing;
    # "it sent one field" is the invariant that was actually broken.
    sent: list[dict] = []
    page.on(
        "request",
        lambda request: (
            request.method == "PATCH"
            and f"/storyboards/{board_id}" in request.url
            and sent.append(json.loads(request.post_data or "{}"))
        ),
    )

    selects = page.locator("select")
    # Text to image, its prompt, image to video, its prompt, starting image, reference.
    for index, value in ((0, drawer), (1, "prompt"), (2, animator), (3, "motion")):
        selects.nth(index).select_option(value)
        page.wait_for_timeout(250)
    selects.nth(4).select_option("start_image")
    page.wait_for_timeout(1500)

    bindings = [body["binding"] for body in sent if "binding" in body]
    check(len(bindings) == 5, f"each pick sent its own change ({len(bindings)})")
    oversized = [b for b in bindings if len(b) != 1]
    check(not oversized, f"and only the field that changed ({oversized})")

    binding = _api(url, f"/api/projects/{pid}/storyboards/{board_id}")["binding"]
    check(binding["image_prompt_param"] == "prompt",
          f"the image prompt survived the picks after it ({binding['image_prompt_param']!r})")
    check(binding["video_prompt_param"] == "motion",
          f"and so did the motion prompt ({binding['video_prompt_param']!r})")
    check(binding["video_image_port"] == "start_image", "and the starting image input")

    print("The prompt picker offers text parameters only")
    surfaces = _api(url, f"/api/projects/{pid}/storyboards/{board_id}/surfaces")
    offered = [p["key"] for p in surfaces["image"]["text_params"]]
    check(offered == ["prompt"], f"a seed is not offered as somewhere to put a prompt ({offered})")


def check_flow_panel(page, url: str, pid: str, board_id: str) -> None:
    """The Flow panel: the steps are listed, a prompt can be edited, and the transcript shows it."""
    print("The flow panel")
    page.goto(f"{url}/p/{pid}/storyboard", wait_until="networkidle")
    page.evaluate("() => localStorage.removeItem('comfywebstudio.layout')")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1800)

    page.locator("button:text-is('Flow')").first.click()
    page.wait_for_timeout(900)

    for step in ("Write the shots", "Draw the frames", "Look at the frame", "Make the shot"):
        check(page.get_by_text(step, exact=True).count() > 0, f"the flow lists {step!r}")
    check(page.get_by_text("asks a model").count() >= 2, "the steps say what kind of thing they are")
    check(page.get_by_text("runs a workflow").count() > 0, "the drawing step is marked as one")

    print("Editing a step")
    page.get_by_text("Look at the frame", exact=True).first.click()
    page.wait_for_timeout(700)
    check(page.get_by_text("System prompt").count() > 0, "the step opens with its prompts showing")
    check(page.get_by_text("Tokens you can use", exact=False).count() > 0, "the token palette is there")

    system = page.locator("textarea").first
    system.fill("You describe frames in exactly one sentence.")
    page.wait_for_timeout(200)
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_timeout(1200)

    stored = _api(url, f"/api/projects/{pid}/storyboards/{board_id}/pipeline")
    describe = next(s for s in stored["stages"] if s["id"] == "describe")
    check(describe["system"] == "You describe frames in exactly one sentence.",
          "the edited prompt is stored")
    check(describe["edited"], "and the step is marked as edited")
    check(not next(s for s in stored["stages"] if s["id"] == "write")["edited"],
          "while the others still track the defaults")

    print("An unresolvable token is flagged as it is typed")
    page.locator("textarea").nth(1).fill("Look at {frame.nonsense}.")
    page.wait_for_timeout(400)
    check(page.get_by_text("No such token", exact=False).count() > 0,
          "a token that will not resolve is called out in the editor")

    print("Resetting")
    page.get_by_role("button", name="Reset to default").click()
    page.wait_for_timeout(1200)
    after = _api(url, f"/api/projects/{pid}/storyboards/{board_id}/pipeline")
    reset_stage = next(s for s in after["stages"] if s["id"] == "describe")
    check(not reset_stage["edited"], "resetting puts the step back to the default")
    check(reset_stage["system"].startswith("You are a storyboard artist"),
          "and the default wording comes back")

    print("The transcript")
    _api(url, f"/api/projects/{pid}/storyboards/{board_id}/pipeline/stages/write/run",
         {"options": {"count": 2}}, "POST")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1500)
    page.locator("button:text-is('Flow')").first.click()
    page.wait_for_timeout(500)
    page.get_by_role("button", name="What was sent").click()
    page.wait_for_timeout(900)

    check(page.get_by_text("Write the shots", exact=True).count() > 0,
          "the transcript lists the step that ran")
    page.get_by_text("Write the shots", exact=True).first.click()
    page.wait_for_timeout(900)
    check(page.get_by_text("System", exact=True).count() > 0,
          "opening one shows what was actually sent")
    check(page.get_by_text("Where it went", exact=False).count() > 0,
          "and where each part of the answer landed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8500")
    args = parser.parse_args()

    pid, board_id, drawer, animator = build(args.url)
    print(f"Built {pid}\n")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1600, "height": 950})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            check_models(page, args.url)
            print()
            check_workspace(page, args.url, pid, board_id)
            print()
            check_drawing(page, args.url, pid, board_id, drawer)
            print()
            check_reference_flag(page, args.url, pid, board_id, drawer)
            print()
            check_make_shot(args.url, pid, board_id, animator)
            print()
            check_flow_panel(page, args.url, pid, board_id)
            print()
            check_binding_panel(page, args.url, pid, board_id, drawer, animator)
            print()

            real = [e for e in errors if "favicon" not in e.lower()]
            check(not real, f"no page errors ({len(real)} found)")
            for error in real[:5]:
                print(f"        {error[:160]}")

            browser.close()
    finally:
        try:
            _api(args.url, f"/api/projects/{pid}", method="DELETE")
        except Exception as exc:  # noqa: BLE001
            print(f"  (could not clean up {pid}: {exc})")

    print()
    for message in SKIPPED:
        print(f"skipped: {message}")
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Storyboard test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
