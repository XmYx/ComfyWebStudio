#!/usr/bin/env python
"""Browser test for menu dismissal.

The subtle case this exists for: several surfaces call ``stopPropagation`` on mousedown — React Flow's
pane, the timeline's clip drag, the buttons in a node header — so a bubble-phase listener never hears a
click on them and the menu stays open. These checks click on exactly those surfaces.

    .venv/bin/python scripts/ui_dismiss.py [--url http://127.0.0.1:8500]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

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


def _scratch(url: str) -> tuple[str, str]:
    """Two steps plus a timeline clip, so every menu surface is reachable."""
    prompt = {
        "1": {"class_type": "WSStringInput", "inputs": {"port_name": "caption", "value": "x"}},
        "2": {"class_type": "EmptyImage",
              "inputs": {"width": 32, "height": 32, "batch_size": 1, "color": 0x22AA55}},
        "3": {"class_type": "WSImageOutput",
              "inputs": {"image": ["2", 0], "port_name": "image", "format": "png",
                         "run_key": "", "quality": 92, "lossless": False}},
    }
    project = _api(url, "/api/projects", {"name": "Dismiss Test"}, "POST")
    pid = project["id"]
    wf = _api(url, f"/api/projects/{pid}/workflows", {"name": "Probe", "prompt": prompt}, "POST")
    shot = _api(url, f"/api/projects/{pid}/shots", {"name": "Shot"}, "POST")
    for index in range(2):
        _api(url, f"/api/projects/{pid}/shots/{shot['id']}/steps",
             {"workflow_id": wf["id"], "ui_pos": {"x": 60 + index * 360, "y": 80}}, "POST")

    run = _api(url, f"/api/projects/{pid}/shots/{shot['id']}/run", {"mode": "shot"}, "POST")
    for _ in range(300):
        time.sleep(0.4)
        run = _api(url, f"/api/projects/{pid}/runs/{run['id']}")
        if run["status"] in {"success", "error", "cancelled"}:
            break
    if run["status"] != "success":
        raise SystemExit(f"The scratch run did not succeed ({run['status']}): {run.get('error')}")
    _api(url, f"/api/projects/{pid}/timeline/from-shots", None, "POST")
    return pid, shot["id"]


def menu_open(page) -> bool:
    return page.locator('[data-testid="context-menu"]').count() > 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8500")
    args = parser.parse_args()

    project_id, _shot_id = _scratch(args.url)
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

        nodes = page.locator(".react-flow__node")

        print("Canvas")
        nodes.first.click(button="right", position={"x": 90, "y": 10})
        page.wait_for_timeout(300)
        check(menu_open(page), "node menu opens")

        # React Flow's pane stops mousedown propagating — the case that was broken.
        page.locator(".react-flow__pane").click(position={"x": 800, "y": 700})
        page.wait_for_timeout(300)
        check(not menu_open(page), "left-clicking the canvas closes it")

        nodes.first.click(button="right", position={"x": 90, "y": 10})
        page.wait_for_timeout(300)
        nodes.nth(1).click(position={"x": 90, "y": 10})
        page.wait_for_timeout(300)
        check(not menu_open(page), "left-clicking another node closes it")

        print("Consecutive right-clicks")
        nodes.first.click(button="right", position={"x": 90, "y": 10})
        page.wait_for_timeout(300)
        page.locator(".react-flow__pane").click(button="right", position={"x": 700, "y": 600})
        page.wait_for_timeout(400)
        check(menu_open(page), "right-clicking elsewhere replaces the menu rather than closing it")
        check(
            page.locator('[data-testid="context-menu"]').locator("text=Add step").count() > 0,
            "the replacement is the canvas menu, not the old node menu",
        )
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

        print("Menu items still work")
        before = len(_api(args.url, f"/api/projects/{project_id}")["shots"][0]["steps"])
        nodes.first.click(button="right", position={"x": 90, "y": 10})
        page.wait_for_timeout(300)
        page.locator('[data-testid="context-menu"]').locator("text=Duplicate Step").first.click()
        page.wait_for_timeout(1500)
        after = len(_api(args.url, f"/api/projects/{project_id}")["shots"][0]["steps"])
        check(after == before + 1, f"clicking an item runs it ({before} -> {after})")
        check(not menu_open(page), "and closes the menu")

        print("Side panels")
        # `.last` disambiguates the panel heading from the nav link of the same name.
        page.locator("text=WORKFLOWS").last.click(button="right")
        page.wait_for_timeout(200)
        page.locator("text=SHOTS").last.click()
        page.wait_for_timeout(300)
        check(not menu_open(page), "a menu opened over a panel closes on a click elsewhere")

        print("Menu bar")
        page.locator("button:text-is('Edit')").click()
        page.wait_for_timeout(300)
        check(page.locator("text=Undo").count() > 0, "the Edit dropdown opens")
        # Same stopPropagation trap as above, for the menu bar's own dismissal.
        page.locator(".react-flow__pane").click(position={"x": 800, "y": 700})
        page.wait_for_timeout(300)
        check(page.locator("text=Select All Steps").count() == 0, "clicking the canvas closes the dropdown")

        print("Timeline")
        page.goto(f"{args.url}/p/{project_id}/timeline", wait_until="networkidle")
        page.wait_for_timeout(1500)
        clips = page.locator("[id^='clip-']")
        if clips.count():
            clips.first.click(button="right")
            page.wait_for_timeout(300)
            check(menu_open(page), "clip menu opens")
            # The clip drag handler stops mousedown propagating; this is the other broken case.
            clips.first.click(position={"x": 5, "y": 5})
            page.wait_for_timeout(300)
            check(not menu_open(page), "left-clicking a clip closes it")
        else:
            print("  (no clips on the timeline; skipped)")

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
    print("Dismiss test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
