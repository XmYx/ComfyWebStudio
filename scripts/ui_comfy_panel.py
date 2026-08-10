#!/usr/bin/env python
"""ComfyUI, embedded in a panel, and workflows opening into it.

Three claims: the panel really loads ComfyUI (not a blocked frame), it can fill the workspace and give the
layout back, and "Open in ComfyUI" points the panel at the workflow instead of opening another browser tab.

Needs the backend running with a built frontend, and a reachable ComfyUI.

    .venv/bin/python scripts/ui_comfy_panel.py [--url http://127.0.0.1:8500]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

FAILURES: list[str] = []

PROMPT = {
    "1": {"class_type": "WSStringInput", "inputs": {"port_name": "caption", "value": "hello"}},
    "2": {"class_type": "EmptyImage",
          "inputs": {"width": 32, "height": 32, "batch_size": 1, "color": 0}},
    "3": {"class_type": "WSImageOutput",
          "inputs": {"image": ["2", 0], "port_name": "image", "format": "png", "run_key": ""}},
}

MAXIMIZED = "() => JSON.parse(localStorage.getItem('comfywebstudio.layout')).state.maximized"

#: Read inside the frame: what ComfyUI has open, and whether the canvas really holds that file's graph.
SAVED_STATE = """() => {
  const store = window.app?.extensionManager?.workflow
  const wf = store?.activeWorkflow
  const wanted = wf?.activeState?.nodes ?? []
  const graph = window.app?.rootGraph ?? window.app?.graph
  const have = new Set((graph?._nodes ?? []).map((n) => String(n.id)))
  return {
    active: wf?.path,
    modified: !!wf?.isModified,
    nodes: have.size,
    wanted: wanted.length,
    matchesFile: wanted.length > 0 && have.size === wanted.length
      && wanted.every((n) => have.has(String(n.id))),
  }
}"""


def check(condition: bool, message: str) -> None:
    print(f"  [{'ok  ' if condition else 'FAIL'}] {message}")
    if not condition:
        FAILURES.append(message)


def _api(url: str, path: str, payload=None, method="GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request) as response:
        body = response.read()
    return json.loads(body) if body else None


def show_comfy_panel(page) -> None:
    """Window ▸ Panels ▸ ComfyUI. The submenu opens on hover, so the pointer has to stay on it."""
    page.locator("button:text-is('Window')").click()
    page.wait_for_timeout(250)
    page.get_by_text("Panels", exact=True).first.hover()
    page.wait_for_timeout(400)
    page.get_by_role("button", name="ComfyUI", exact=True).last.click()
    page.wait_for_timeout(500)


def comfy_frame_urls(page) -> list[str]:
    return [f.url for f in page.frames if "8188" in f.url or "/?ws_open=" in f.url]


def comfy_frame(page):
    """The frame currently inside the panel.

    Taken from the element rather than by scanning `page.frames` for a URL: a reload leaves the old,
    detached frame in that list, and evaluating against it reports an empty page rather than an error.
    """
    handle = page.locator("iframe[title='ComfyUI']").element_handle()
    return handle.content_frame() if handle else None


def settled_state(page, timeout_ms: int = 30000):
    """Poll the frame until the open has finished, or give up and report whatever it last had.

    "Finished" means both the right graph *and* a clean modified flag: loading a graph marks the workflow
    dirty and the bridge clears it a moment later, so reading the instant the nodes appear catches a
    transient rather than the resting state.
    """
    waited = 0
    state = None
    while waited < timeout_ms:
        frame = comfy_frame(page)
        try:
            state = frame.evaluate(SAVED_STATE) if frame else None
        except Exception:  # noqa: BLE001 - a frame mid-navigation cannot be evaluated against
            state = None
        if state and state["matchesFile"] and not state["modified"]:
            return state
        page.wait_for_timeout(1000)
        waited += 1000
    return state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8500")
    parser.add_argument("--shots-dir", default=None)
    parser.add_argument(
        "--saved", default="image_krea2_turbo_t2i.json",
        help="a workflow ComfyUI already has, used to check the *file's* graph is what opens",
    )
    args = parser.parse_args()

    shots = Path(args.shots_dir) if args.shots_dir else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    backends = _api(args.url, "/api/settings/backends")
    if not backends:
        print("No ComfyUI backend configured; nothing to embed.")
        return 1
    base = backends[0]["base_url"]

    project = _api(args.url, "/api/projects", {"name": "Comfy Panel"}, "POST")
    pid = project["id"]
    workflow = _api(args.url, f"/api/projects/{pid}/workflows",
                    {"name": "Embedded", "prompt": PROMPT}, "POST")
    shot = _api(args.url, f"/api/projects/{pid}/shots", {"name": "Shot"}, "POST")
    _api(args.url, f"/api/projects/{pid}/shots/{shot['id']}/steps",
         {"workflow_id": workflow["id"], "ui_pos": {"x": 60, "y": 60}}, "POST")
    print(f"Using scratch project {pid}, ComfyUI at {base}")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1600, "height": 950})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            page.goto(f"{args.url}/p/{pid}/shots", wait_until="networkidle")
            page.evaluate("() => localStorage.removeItem('comfywebstudio.layout')")
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1500)

            print("The panel")
            check(
                page.locator("iframe[title='ComfyUI']").count() == 0,
                "it is off until asked for — it loads a whole second application",
            )
            show_comfy_panel(page)
            frame = page.locator("iframe[title='ComfyUI']")
            check(frame.count() == 1, "showing it puts an embedded ComfyUI on screen")
            page.wait_for_timeout(9000)
            check(bool(comfy_frame_urls(page)), f"which really loaded ComfyUI {comfy_frame_urls(page)}")
            check(
                page.frame_locator("iframe[title='ComfyUI']").locator("canvas").count() > 0,
                "and drew its canvas, so the frame is not blocked",
            )
            if shots:
                page.screenshot(path=str(shots / "comfy-panel.png"))

            print("Maximize and restore")
            # The panel's own header button, not whichever dock group happens to come first.
            page.locator("button[title='Fill the workspace with ComfyUI']").click()
            page.wait_for_timeout(1200)
            check(page.evaluate(MAXIMIZED) == "comfy", "maximising records which panel is filling it")
            box = page.locator("[data-dock-group]:has(iframe[title='ComfyUI'])").bounding_box()
            check(
                box is not None and box["width"] > 1400 and box["height"] > 700,
                f"and it covers the workspace ({box and (round(box['width']), round(box['height']))})",
            )
            # The frame must survive maximising: it is a whole application, and remounting it would
            # reload ComfyUI and lose whatever was on the canvas.
            check(
                page.frame_locator("iframe[title='ComfyUI']").locator("canvas").count() > 0,
                "without reloading the frame — ComfyUI is still up",
            )
            if shots:
                page.screenshot(path=str(shots / "comfy-maximized.png"))

            page.locator("button[title='Restore the layout']").first.click()
            page.wait_for_timeout(900)
            check(page.evaluate(MAXIMIZED) is None, "restoring gives the layout back")
            check(page.locator("[data-dock-group]").count() > 1, "with the other panels returned")

            print("Opening a workflow lands in the panel")
            page.locator("button:text-is('Workflows')").first.click()
            page.wait_for_timeout(600)
            page.get_by_role("button", name="Open in ComfyUI").first.click()
            page.wait_for_timeout(9000)

            check(len(page.context.pages) == 1, "no second browser tab was opened")
            # The deep link is not checked in the URL: the bridge strips ws_open the moment it reads it,
            # so a link that worked leaves no trace there. The badge is the honest signal — it only says
            # "Linked" once the extension has fetched that step and bound this tab to it.
            badge = page.frame_locator("iframe[title='ComfyUI']").locator("#comfywebstudio-badge")
            check(badge.count() == 1, "the bridge extension is running inside the embedded copy")
            text = badge.inner_text() if badge.count() else ""
            check("Linked" in text, f"and it picked up the workflow we asked for ({text!r})")
            if shots:
                page.screenshot(path=str(shots / "comfy-open-workflow.png"))

            print("A workflow ComfyUI already has opens as its own saved file")
            saved = _api(args.url, f"/api/comfy/projects/{pid}/import", {"path": args.saved}, "POST")
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1200)
            page.locator("button:text-is('Workflows')").first.click()
            page.wait_for_timeout(600)
            page.get_by_role("button", name="Open in ComfyUI").last.click()

            state = settled_state(page)
            check(bool(state), "the frame is there to inspect")
            if state:
                check(
                    state["active"] == saved["comfy_userdata_path"],
                    f"the tab is that saved workflow ({state['active']})",
                )
                # The heart of it: right name is not enough. `openWorkflow` can switch the active workflow
                # while leaving the previous graph on the canvas, which looks perfect and is wrong. The
                # node ids on the canvas have to be the ones in the file ComfyUI read.
                check(
                    state["matchesFile"],
                    f"and the graph on screen is that file's, not whatever was there "
                    f"({state['nodes']} nodes vs {state['wanted']} in the file)",
                )
                check(not state["modified"], "opened clean, with no phantom unsaved changes")
            if shots:
                page.screenshot(path=str(shots / "comfy-saved-workflow.png"))

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
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("ComfyUI panel test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
