#!/usr/bin/env python
"""Browser smoke test against a running ComfyWebStudio.

Verifies the parts that only fail in a real browser: the shot canvas measures its nodes, draws typed
edges between ports, the inspector shows parameters and previews, and the timeline renders clips.

    .venv/bin/python scripts/ui_smoke.py [--url http://127.0.0.1:8500] [--shots-dir out/]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

FAILURES: list[str] = []


def _api(url: str, path: str, payload=None, method="GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request) as response:
        body = response.read()
    return json.loads(body) if body else None


def _scratch_project(url: str) -> str:
    """Build and run a two-step chain of our own.

    The smoke test needs a project that has actually produced artifacts, so it creates one and runs it
    against the configured ComfyUI rather than depending on whatever happens to be in the project list.
    """
    generator = {
        "1": {"class_type": "WSStringInput", "inputs": {"port_name": "caption", "value": "smoke"}},
        "2": {"class_type": "EmptyImage",
              "inputs": {"width": 32, "height": 32, "batch_size": 1, "color": 0x22AA55}},
        "3": {"class_type": "WSImageOutput",
              "inputs": {"image": ["2", 0], "port_name": "image", "format": "png",
                         "run_key": "", "quality": 92, "lossless": False}},
    }
    consumer = {
        "1": {"class_type": "WSImageInput", "inputs": {"port_name": "image", "source": ""}},
        "2": {"class_type": "ImageScale",
              "inputs": {"image": ["1", 0], "upscale_method": "nearest-exact",
                         "width": 64, "height": 64, "crop": "disabled"}},
        "3": {"class_type": "WSImageOutput",
              "inputs": {"image": ["2", 0], "port_name": "final", "format": "png",
                         "run_key": "", "quality": 92, "lossless": False}},
    }

    project = _api(url, "/api/projects", {"name": "Smoke Test"}, "POST")
    pid = project["id"]
    gen = _api(url, f"/api/projects/{pid}/workflows", {"name": "Generate", "prompt": generator}, "POST")
    con = _api(url, f"/api/projects/{pid}/workflows", {"name": "Upscale", "prompt": consumer}, "POST")
    shot = _api(url, f"/api/projects/{pid}/shots", {"name": "Shot 1"}, "POST")

    a = _api(url, f"/api/projects/{pid}/shots/{shot['id']}/steps",
             {"workflow_id": gen["id"], "ui_pos": {"x": 40, "y": 60}}, "POST")
    b = _api(url, f"/api/projects/{pid}/shots/{shot['id']}/steps",
             {"workflow_id": con["id"], "ui_pos": {"x": 460, "y": 60}}, "POST")
    _api(url, f"/api/projects/{pid}/shots/{shot['id']}/links",
         {"from_step": a["id"], "from_port": "image", "to_step": b["id"], "to_port": "image"}, "POST")

    run = _api(url, f"/api/projects/{pid}/shots/{shot['id']}/run", {"mode": "shot"}, "POST")
    for _ in range(300):
        time.sleep(0.4)
        run = _api(url, f"/api/projects/{pid}/runs/{run['id']}")
        if run["status"] in {"success", "error", "cancelled"}:
            break
    if run["status"] != "success":
        raise SystemExit(f"The scratch run did not succeed ({run['status']}): {run.get('error')}")

    _api(url, f"/api/projects/{pid}/timeline/from-shots", None, "POST")
    return pid


def check(condition: bool, message: str) -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        FAILURES.append(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8500")
    parser.add_argument("--shots-dir", default=None, help="Write screenshots here")
    args = parser.parse_args()

    shots = Path(args.shots_dir) if args.shots_dir else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    project_id = _scratch_project(args.url)
    print(f"Using scratch project {project_id}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 950})

        console_errors: list[str] = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(str(e)))

        print("Projects page")
        page.goto(args.url, wait_until="networkidle")
        page.wait_for_selector("text=Projects")
        cards = page.locator("div.group")
        check(cards.count() > 0, f"{cards.count()} project card(s) listed")
        check(page.locator("text=Smoke Test").count() > 0, "the scratch project is listed")
        if shots:
            page.screenshot(path=str(shots / "01-projects.png"))

        print("Shot editor")
        page.goto(f"{args.url}/p/{project_id}/shots", wait_until="networkidle")
        page.wait_for_selector(".react-flow__node", timeout=15000)
        # React Flow keeps a node hidden until it has measured it; edges only appear afterwards.
        page.wait_for_function(
            "() => [...document.querySelectorAll('.react-flow__node')]"
            ".every(n => !n.style.visibility || n.style.visibility !== 'hidden')",
            timeout=15000,
        )
        page.wait_for_timeout(600)

        nodes = page.locator(".react-flow__node").count()
        edges = page.locator(".react-flow__edge").count()
        handles = page.locator(".react-flow__handle").count()
        check(nodes >= 2, f"{nodes} step node(s) rendered")
        check(edges >= 1, f"{edges} link edge(s) drawn between ports")
        check(handles >= 4, f"{handles} port handle(s) rendered")
        check(page.locator("text=WORKFLOWS").count() == 1, "workflow library present")
        if shots:
            page.screenshot(path=str(shots / "02-shot-editor.png"))

        print("Step inspector")
        page.locator(".react-flow__node").first.locator("div").first.click(position={"x": 90, "y": 10})
        page.wait_for_selector("text=Parameters", timeout=10000)
        check(page.locator("text=Parameters").count() > 0, "inspector opened on the Parameters tab")
        check(
            page.locator("input, textarea, select").count() > 0,
            "parameter widgets rendered",
        )

        page.locator("button", has_text="Output").first.click()
        page.wait_for_timeout(800)
        images = page.locator("img[src*='/api/projects/']").count()
        check(images > 0, f"{images} artifact preview(s) served through the API")
        if shots:
            page.screenshot(path=str(shots / "03-inspector.png"))

        print("Timeline")
        page.locator("a", has_text="Timeline").first.click()
        page.wait_for_timeout(1500)
        clips = page.locator("[id^='clip-']").count()
        check(clips > 0, f"{clips} clip(s) on the timeline")
        check(page.locator("text=Renders").count() > 0, "renders panel present")
        if shots:
            page.screenshot(path=str(shots / "04-timeline.png"))

        print("Settings")
        page.locator("a", has_text="Settings").first.click()
        page.wait_for_selector("text=ComfyUI backends", timeout=10000)
        page.locator("button", has_text="Test").first.click()
        page.wait_for_selector("text=reachable", timeout=20000)
        check(page.locator("text=reachable").count() > 0, "backend test reports the instance reachable")
        check(
            page.locator("text=node pack").count() > 0,
            "backend test reports node pack status",
        )
        if shots:
            page.screenshot(path=str(shots / "05-settings.png"))

        real_errors = [e for e in console_errors if "favicon" not in e.lower()]
        check(not real_errors, f"no console errors ({len(real_errors)} found)")
        for error in real_errors[:5]:
            print(f"        {error[:160]}")

        browser.close()

    try:
        _api(args.url, f"/api/projects/{project_id}", method="DELETE")
    except Exception as exc:  # noqa: BLE001 - cleanup failure must not mask a test result
        print(f"  (could not clean up scratch project: {exc})")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("UI smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
