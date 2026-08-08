#!/usr/bin/env python
"""Browser test for the application menus.

Covers the parts that only exist in the browser: the menu bar opens and renders enabled/disabled state
correctly, keyboard shortcuts fire the same commands, Undo/Redo round-trips a real edit, panel toggles
change the layout, and the Help dialogs open.

    .venv/bin/python scripts/ui_menus.py [--url http://127.0.0.1:8500]
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


def _make_scratch_project(url: str) -> str:
    """Build a small project of our own, so the test neither depends on nor disturbs real work."""
    prompt = {
        "1": {"class_type": "WSStringInput", "inputs": {"port_name": "caption", "value": "hello"}},
        "2": {"class_type": "EmptyImage", "inputs": {"width": 32, "height": 32, "batch_size": 1}},
        "3": {
            "class_type": "WSImageOutput",
            "inputs": {"image": ["2", 0], "port_name": "image", "format": "png", "run_key": ""},
        },
    }
    project = _api(url, "/api/projects", {"name": "Menu Test"}, "POST")
    workflow = _api(
        url, f"/api/projects/{project['id']}/workflows", {"name": "Probe", "prompt": prompt}, "POST"
    )
    shot = _api(url, f"/api/projects/{project['id']}/shots", {"name": "Shot"}, "POST")
    _api(
        url,
        f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
        {"workflow_id": workflow["id"]},
        "POST",
    )
    return project["id"]


def _delete_project(url: str, project_id: str) -> None:
    try:
        _api(url, f"/api/projects/{project_id}", method="DELETE")
    except Exception as exc:  # noqa: BLE001 - cleanup failure must not mask a test result
        print(f"  (could not clean up scratch project: {exc})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8500")
    parser.add_argument("--shots-dir", default=None)
    args = parser.parse_args()

    shots = Path(args.shots_dir) if args.shots_dir else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    project_id = _make_scratch_project(args.url)
    print(f"Using scratch project {project_id}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(f"{args.url}/p/{project_id}/shots", wait_until="networkidle")
        page.wait_for_selector(".react-flow__node", timeout=15000)
        page.wait_for_timeout(700)

        print("Menu bar")
        for label in ("File", "Edit", "Window", "Plugins", "Help"):
            check(page.locator(f"button:text-is('{label}')").count() == 1, f"{label} menu present")

        page.locator("button:text-is('File')").click()
        page.wait_for_timeout(200)
        check(page.locator("text=New Project…").count() > 0, "File menu opens with its items")
        check(page.locator("text=Import").count() > 0, "File menu has an Import submenu")
        if shots:
            page.screenshot(path=str(shots / "menu-file.png"))

        # Hovering a submenu reveals its children.
        page.locator("text=Import").first.hover()
        page.wait_for_timeout(300)
        check(
            page.locator("text=Import Workflow from ComfyUI…").count() > 0,
            "Import submenu expands on hover",
        )
        page.keyboard.press("Escape")

        print("Enabled state")
        page.locator("button:text-is('Edit')").click()
        page.wait_for_timeout(200)
        paste = page.locator("button", has_text="Paste").first
        check(paste.is_disabled(), "Paste is disabled with an empty clipboard")
        if shots:
            page.screenshot(path=str(shots / "menu-edit.png"))
        page.keyboard.press("Escape")

        print("Copy and paste a step")
        before = len(json.load(urllib.request.urlopen(f"{args.url}/api/projects/{project_id}"))["shots"][0]["steps"])
        page.locator(".react-flow__node").first.locator("div").first.click(position={"x": 90, "y": 10})
        page.wait_for_timeout(300)
        page.keyboard.press("Control+c")
        page.wait_for_timeout(300)
        page.keyboard.press("Control+v")
        page.wait_for_timeout(1500)
        after = len(json.load(urllib.request.urlopen(f"{args.url}/api/projects/{project_id}"))["shots"][0]["steps"])
        check(after == before + 1, f"paste added a step ({before} -> {after})")

        print("Undo and redo")
        page.keyboard.press("Control+z")
        page.wait_for_timeout(1500)
        undone = len(json.load(urllib.request.urlopen(f"{args.url}/api/projects/{project_id}"))["shots"][0]["steps"])
        check(undone == before, f"undo removed the pasted step ({after} -> {undone})")

        page.keyboard.press("Control+Shift+z")
        page.wait_for_timeout(1500)
        redone = len(json.load(urllib.request.urlopen(f"{args.url}/api/projects/{project_id}"))["shots"][0]["steps"])
        check(redone == after, f"redo restored it ({undone} -> {redone})")

        # Leave the project as we found it.
        page.keyboard.press("Control+z")
        page.wait_for_timeout(1500)

        print("Window menu")
        panels_before = page.locator("text=WORKFLOWS").count()
        page.keyboard.press("Control+1")
        page.wait_for_timeout(500)
        check(
            page.locator("text=WORKFLOWS").count() != panels_before,
            "Ctrl+1 toggles the workflows panel",
        )
        page.keyboard.press("Control+1")
        page.wait_for_timeout(400)
        check(page.locator("text=WORKFLOWS").count() == panels_before, "toggling back restores it")

        page.locator("button:text-is('Window')").click()
        page.wait_for_timeout(200)
        check(page.locator("text=Fit Graph to Window").count() > 0, "Window menu lists canvas commands")
        page.keyboard.press("Escape")

        print("Help")
        page.keyboard.press("Control+/")
        page.wait_for_timeout(600)
        check(page.locator("text=Keyboard shortcuts").count() > 0, "shortcuts dialog opens")
        if shots:
            page.screenshot(path=str(shots / "menu-shortcuts.png"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        page.locator("button:text-is('Help')").click()
        page.wait_for_timeout(200)
        page.locator("text=About ComfyWebStudio").first.click()
        page.wait_for_timeout(800)
        check(page.locator("text=Connected backends").count() > 0, "About dialog shows backend info")
        if shots:
            page.screenshot(path=str(shots / "menu-about.png"))
        page.keyboard.press("Escape")

        print("Plugins")
        page.locator("button:text-is('Plugins')").click()
        page.wait_for_timeout(200)
        page.locator("text=Manage Plugins…").first.click()
        page.wait_for_timeout(800)
        check(page.locator("text=A plugin is a reusable bundle").count() > 0, "Plugins dialog opens")
        if shots:
            page.screenshot(path=str(shots / "menu-plugins.png"))
        page.keyboard.press("Escape")

        real = [e for e in errors if "favicon" not in e.lower()]
        check(not real, f"no console errors ({len(real)} found)")
        for error in real[:5]:
            print(f"        {error[:160]}")

        browser.close()

    _delete_project(args.url, project_id)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Menu test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
