#!/usr/bin/env python
"""Dragging a shot into a shot places it as one contained node.

The behaviour under test: a dropped shot arrives as the same kind of node a template does — ports to wire,
controls to edit, a preview of what it produced — and its values are its own, so editing one placement
does not touch the shot it came from or any other placement of it.

Needs the backend running with a built frontend.

    .venv/bin/python scripts/ui_nested_shots.py [--url http://127.0.0.1:8500]
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


def scratch(url: str) -> tuple[str, str, str]:
    """A project with a one-step shot worth reusing, and an empty shot to place it in."""
    project = _api(url, "/api/projects", {"name": "Nested Shots"}, "POST")
    pid = project["id"]
    workflow = _api(url, f"/api/projects/{pid}/workflows", {"name": "Gen", "prompt": PROMPT}, "POST")
    inner = _api(url, f"/api/projects/{pid}/shots", {"name": "Inner"}, "POST")
    _api(url, f"/api/projects/{pid}/shots/{inner['id']}/steps",
         {"workflow_id": workflow["id"], "ui_pos": {"x": 60, "y": 60}}, "POST")
    outer = _api(url, f"/api/projects/{pid}/shots", {"name": "Outer"}, "POST")
    return pid, inner["id"], outer["id"]


def drag_shot_onto_canvas(page, shot_name: str) -> None:
    """HTML5 drag and drop, driven through the events the app actually listens for.

    Playwright's drag_to does not carry dataTransfer between elements, and the payload is the whole point
    here — so the transfer object is made once in the page and handed to each event.
    """
    page.evaluate("() => { window.__dt = new DataTransfer() }")
    source = page.get_by_text(shot_name, exact=True).last
    source.dispatch_event("dragstart", {"dataTransfer": page.evaluate_handle("() => window.__dt")})
    target = page.locator(".react-flow__pane, [data-testid='empty-canvas']").first
    target.dispatch_event("dragover", {"dataTransfer": page.evaluate_handle("() => window.__dt")})
    target.dispatch_event("drop", {"dataTransfer": page.evaluate_handle("() => window.__dt")})
    page.wait_for_timeout(1200)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8500")
    parser.add_argument("--shots-dir", default=None)
    args = parser.parse_args()

    shots = Path(args.shots_dir) if args.shots_dir else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    project_id, inner_id, outer_id = scratch(args.url)
    print(f"Using scratch project {project_id}")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1600, "height": 950})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            page.goto(f"{args.url}/p/{project_id}/shots", wait_until="networkidle")
            page.wait_for_timeout(1200)
            page.get_by_text("Outer", exact=True).last.click()
            page.wait_for_timeout(800)

            print("Dropping a shot onto another shot's canvas")
            drag_shot_onto_canvas(page, "Inner")

            instances = _api(args.url, f"/api/projects/{project_id}")["shots"]
            outer = next(s for s in instances if s["id"] == outer_id)
            check(len(outer["instances"]) == 1, f"it became one contained node ({len(outer['instances'])})")
            check(
                not outer["nodes"],
                "and not the old single-output reference node",
            )
            if not outer["instances"]:
                browser.close()
                raise SystemExit(1)

            instance = outer["instances"][0]
            check(
                instance["template_id"] == f"shot:{inner_id}",
                f"pointing at the shot it stands for ({instance['template_id']})",
            )
            check(instance["workflow_map"] == {}, "reusing the project's workflows rather than copying them")

            placed = _api(args.url, f"/api/projects/{project_id}/shots/{outer_id}/placed")[0]
            check(placed["source_shot_id"] == inner_id, "the canvas is told it is a shot, not a template")
            check(placed["stale"] is False, "a live shot is never stale")
            check(
                {p["key"] for p in placed["ports"]} == {"caption", "image"},
                f"with the shot's unwired ports promoted ({[p['key'] for p in placed['ports']]})",
            )

            print("What the node shows")
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1200)
            # A reload does not remember which shot was open, so pick it again before looking.
            page.get_by_text("Outer", exact=True).last.click()
            page.wait_for_timeout(1500)
            node = page.locator(f'[data-id="{instance["id"]}"]')
            check(node.count() == 1, "the node is drawn on the canvas")
            if not node.count():
                if shots:
                    page.screenshot(path=str(shots / "nested-shot-missing.png"))
                browser.close()
                raise SystemExit(1)
            check("shot" in node.inner_text().lower(), f"labelled as a shot ({node.inner_text()[:60]!r})")
            check(
                page.locator(f'[data-id="{instance["id"]}"] .react-flow__handle').count() == 2,
                "with a handle for each promoted port",
            )
            if shots:
                page.screenshot(path=str(shots / "nested-shot.png"))

            print("Values are instanced")
            _api(args.url, f"/api/projects/{project_id}/instances/{instance['id']}",
                 {"param_overrides": {"caption": "only mine"}}, "PATCH")
            source = next(s for s in _api(args.url, f"/api/projects/{project_id}")["shots"]
                          if s["id"] == inner_id)
            check(
                source["steps"][0]["param_overrides"].get("caption") != "only mine",
                "editing the placed node left the shot it came from alone",
            )

            real = [e for e in errors if "favicon" not in e.lower()]
            check(not real, f"no page errors ({len(real)} found)")
            for error in real[:5]:
                print(f"        {error[:160]}")

            browser.close()
    finally:
        try:
            _api(args.url, f"/api/projects/{project_id}", method="DELETE")
        except Exception as exc:  # noqa: BLE001
            print(f"  (could not clean up {project_id}: {exc})")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Nested shot test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
