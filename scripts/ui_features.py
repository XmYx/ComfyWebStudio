#!/usr/bin/env python
"""Browser test for node resize, context menus and the history panel.

Everything here only exists in the browser: a resize handle you can actually drag, menus that appear at the
pointer, and a history list that restores one element without touching the rest.

    .venv/bin/python scripts/ui_features.py [--url http://127.0.0.1:8500]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

FAILURES: list[str] = []


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


def _scratch_project(url: str) -> tuple[str, str, str]:
    """A two-step chain of our own, so the test neither depends on nor disturbs real work."""
    generator = {
        "1": {"class_type": "WSStringInput", "inputs": {"port_name": "caption", "value": "hello"}},
        "2": {"class_type": "EmptyImage", "inputs": {"width": 32, "height": 32, "batch_size": 1}},
        "3": {"class_type": "WSImageOutput",
              "inputs": {"image": ["2", 0], "port_name": "image", "format": "png", "run_key": ""}},
    }
    consumer = {
        "1": {"class_type": "WSImageInput", "inputs": {"port_name": "image", "source": ""}},
        "2": {"class_type": "WSImageOutput",
              "inputs": {"image": ["1", 0], "port_name": "final", "format": "png", "run_key": ""}},
    }

    project = _api(url, "/api/projects", {"name": "Feature Test"}, "POST")
    pid = project["id"]
    gen = _api(url, f"/api/projects/{pid}/workflows", {"name": "Gen", "prompt": generator}, "POST")
    con = _api(url, f"/api/projects/{pid}/workflows", {"name": "Con", "prompt": consumer}, "POST")
    shot = _api(url, f"/api/projects/{pid}/shots", {"name": "Shot"}, "POST")

    a = _api(url, f"/api/projects/{pid}/shots/{shot['id']}/steps",
             {"workflow_id": gen["id"], "ui_pos": {"x": 40, "y": 60}}, "POST")
    b = _api(url, f"/api/projects/{pid}/shots/{shot['id']}/steps",
             {"workflow_id": con["id"], "ui_pos": {"x": 460, "y": 60}}, "POST")
    _api(url, f"/api/projects/{pid}/shots/{shot['id']}/links",
         {"from_step": a["id"], "from_port": "image", "to_step": b["id"], "to_port": "image"}, "POST")
    return pid, shot["id"], a["id"]


def step_size(url: str, project_id: str, step_id: str) -> dict:
    project = _api(url, f"/api/projects/{project_id}")
    step = next(s for s in project["shots"][0]["steps"] if s["id"] == step_id)
    return step["ui_size"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8500")
    parser.add_argument("--shots-dir", default=None)
    args = parser.parse_args()

    shots = Path(args.shots_dir) if args.shots_dir else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    project_id, shot_id, step_id = _scratch_project(args.url)
    print(f"Using scratch project {project_id}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(f"{args.url}/p/{project_id}/shots", wait_until="networkidle")
        page.wait_for_selector(".react-flow__node", timeout=15000)
        page.wait_for_function(
            "() => [...document.querySelectorAll('.react-flow__node')]"
            ".every(n => n.style.visibility !== 'hidden')",
            timeout=15000,
        )
        page.wait_for_timeout(700)

        # -- node resize ---------------------------------------------------------------------------
        print("Node resize")
        node = page.locator(f'[data-id="{step_id}"]')
        node.locator("div").first.click(position={"x": 90, "y": 10})
        page.wait_for_timeout(400)

        handles = page.locator(".react-flow__resize-control.handle")
        check(handles.count() > 0, f"{handles.count()} resize handle(s) appear on selection")

        before = node.bounding_box()
        corner = page.locator(".react-flow__resize-control.handle.bottom.right").first
        box = corner.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        for i in range(1, 9):
            page.mouse.move(box["x"] + 10 * i, box["y"] + 12 * i)
            page.wait_for_timeout(20)
        page.mouse.up()
        page.wait_for_timeout(1500)

        after = node.bounding_box()
        check(after["width"] > before["width"] + 40, f"node widened {before['width']:.0f} → {after['width']:.0f}")
        check(after["height"] > before["height"] + 40, f"node grew taller {before['height']:.0f} → {after['height']:.0f}")

        size = step_size(args.url, project_id, step_id)
        check(size["w"] > 0 and size["h"] > 0, f"size persisted to the server as {size['w']:.0f}×{size['h']:.0f}")

        page.reload(wait_until="networkidle")
        page.wait_for_selector(".react-flow__node", timeout=15000)
        page.wait_for_timeout(1000)
        reloaded = page.locator(f'[data-id="{step_id}"]').bounding_box()
        check(abs(reloaded["width"] - after["width"]) < 12, "size survives a reload")
        if shots:
            page.screenshot(path=str(shots / "resize.png"))

        # -- context menus -------------------------------------------------------------------------
        print("Context menus")
        page.locator(f'[data-id="{step_id}"]').click(button="right", position={"x": 90, "y": 10})
        page.wait_for_timeout(400)
        menu = page.locator('[data-testid="context-menu"]')
        check(menu.count() == 1, "right-clicking a node opens a menu")
        check(menu.locator("text=Run this step").count() > 0, "node menu offers Run")
        check(menu.locator("text=Disable step").count() > 0, "node menu offers Disable")
        check(menu.locator("text=Reset size").count() > 0, "node menu offers Reset size")
        if shots:
            page.screenshot(path=str(shots / "menu-node.png"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        pane = page.locator(".react-flow__pane").bounding_box()
        page.locator(".react-flow__pane").click(
            button="right", position={"x": pane["width"] * 0.6, "y": pane["height"] * 0.6}
        )
        page.wait_for_timeout(400)
        pane_menu = page.locator('[data-testid="context-menu"]')
        check(pane_menu.count() == 1, "right-clicking the canvas opens a menu")
        check(pane_menu.locator("text=Add step").count() > 0, "canvas menu offers Add step")
        pane_menu.locator("text=Add step").first.hover()
        page.wait_for_timeout(400)
        check(page.locator("text=Gen").count() > 0, "the Add step submenu lists workflows")
        if shots:
            page.screenshot(path=str(shots / "menu-canvas.png"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        # A menu opened near the bottom edge must still fit on screen. Measured from the pane rather than
        # hard-coded: the canvas is a resizable dock panel, so its size is not a constant.
        pane = page.locator(".react-flow__pane").bounding_box()
        page.locator(".react-flow__pane").click(
            button="right",
            position={"x": pane["width"] - 30, "y": pane["height"] - 20},
        )
        page.wait_for_timeout(400)
        rect = page.locator('[data-testid="context-menu"]').bounding_box()
        check(rect["y"] + rect["height"] <= 960, "a menu near the bottom edge flips to stay on screen")
        page.keyboard.press("Escape")

        # -- history -------------------------------------------------------------------------------
        print("History")
        page.locator(f'[data-id="{step_id}"]').click(position={"x": 90, "y": 10})
        page.wait_for_selector("text=Parameters", timeout=10000)

        # Make a real, recorded edit through the UI.
        prompt_field = page.locator("textarea").first
        prompt_field.fill("first value")
        page.wait_for_timeout(1200)
        prompt_field.fill("second value")
        page.wait_for_timeout(1500)

        page.locator("button", has_text="History").first.click()
        page.wait_for_timeout(1200)
        entries = page.locator("text=/Set caption on/")
        check(entries.count() >= 2, f"{entries.count()} parameter changes listed in the step's History tab")
        if shots:
            page.screenshot(path=str(shots / "history-step.png"))

        # Restore the older of the two values.
        restore_buttons = page.locator("button", has_text="Restore")
        check(restore_buttons.count() >= 2, "each history entry offers Restore")
        restore_buttons.nth(1).click()
        page.wait_for_timeout(2000)

        project = _api(args.url, f"/api/projects/{project_id}")
        step = next(s for s in project["shots"][0]["steps"] if s["id"] == step_id)
        check(
            step["param_overrides"].get("caption") == "first value",
            f"restoring an element reverted just that value (got {step['param_overrides'].get('caption')!r})",
        )

        # The other step must be untouched by an element restore.
        other = [s for s in project["shots"][0]["steps"] if s["id"] != step_id][0]
        check(other["name"] == "Con", "restoring one step left the other alone")

        page.keyboard.press("Control+h")
        page.wait_for_timeout(1000)
        check(page.locator("text=Project history").count() > 0, "Ctrl+H opens the project history")
        check(
            page.locator("text=/Connected .*→.*/").count() > 0,
            "the log describes the link that was created",
        )
        if shots:
            page.screenshot(path=str(shots / "history-project.png"))
        page.keyboard.press("Escape")

        real = [e for e in errors if "favicon" not in e.lower()]
        check(not real, f"no console errors ({len(real)} found)")
        for error in real[:5]:
            print(f"        {error[:160]}")

        browser.close()

    try:
        _api(args.url, f"/api/projects/{project_id}", method="DELETE")
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not clean up: {exc})")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Feature test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
