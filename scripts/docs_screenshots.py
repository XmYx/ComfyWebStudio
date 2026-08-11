#!/usr/bin/env python
"""Capture the screenshots the documentation uses.

Committed images go stale silently, so they are generated rather than taken by hand: this builds a small
demo project, runs it against the configured ComfyUI so the previews are real output, poses the UI, and
writes every image the guide references into `docs/images/`.

Re-run it after any change to the workspace and commit whatever moves.

    .venv/bin/python scripts/docs_screenshots.py [--url http://127.0.0.1:8500]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

VIEWPORT = {"width": 1600, "height": 950}

GENERATOR = {
    "1": {"class_type": "WSStringInput",
          "inputs": {"port_name": "caption", "value": "a lighthouse at dusk"}},
    "2": {"class_type": "EmptyImage",
          "inputs": {"width": 96, "height": 96, "batch_size": 1, "color": 0x2C6E9B}},
    "3": {"class_type": "WSImageOutput",
          "inputs": {"image": ["2", 0], "port_name": "image", "format": "png",
                     "run_key": "", "quality": 92, "lossless": False}},
}

UPSCALE = {
    "1": {"class_type": "WSImageInput", "inputs": {"port_name": "image", "source": ""}},
    "2": {"class_type": "ImageScale",
          "inputs": {"image": ["1", 0], "upscale_method": "nearest-exact",
                     "width": 192, "height": 192, "crop": "disabled"}},
    "3": {"class_type": "WSImageOutput",
          "inputs": {"image": ["2", 0], "port_name": "final", "format": "png",
                     "run_key": "", "quality": 92, "lossless": False}},
}


def _api(url: str, path: str, payload=None, method="GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request) as response:
        body = response.read()
    return json.loads(body) if body else None


def build_demo(url: str) -> tuple[str, str, str]:
    """A project with a chained, *run* shot, so the previews in the screenshots are real output."""
    project = _api(url, "/api/projects", {"name": "Lighthouse Demo"}, "POST")
    pid = project["id"]
    gen = _api(url, f"/api/projects/{pid}/workflows", {"name": "Generate", "prompt": GENERATOR}, "POST")
    up = _api(url, f"/api/projects/{pid}/workflows", {"name": "Upscale", "prompt": UPSCALE}, "POST")

    shot = _api(url, f"/api/projects/{pid}/shots", {"name": "Establishing"}, "POST")
    a = _api(url, f"/api/projects/{pid}/shots/{shot['id']}/steps",
             {"workflow_id": gen["id"], "ui_pos": {"x": 40, "y": 80}}, "POST")
    b = _api(url, f"/api/projects/{pid}/shots/{shot['id']}/steps",
             {"workflow_id": up["id"], "ui_pos": {"x": 430, "y": 80}}, "POST")
    _api(url, f"/api/projects/{pid}/shots/{shot['id']}/links",
         {"from_step": a["id"], "from_port": "image", "to_step": b["id"], "to_port": "image"}, "POST")

    run = _api(url, f"/api/projects/{pid}/shots/{shot['id']}/run", {"mode": "shot"}, "POST")
    for _ in range(300):
        time.sleep(0.4)
        run = _api(url, f"/api/projects/{pid}/runs/{run['id']}")
        if run["status"] in {"success", "error", "cancelled"}:
            break
    if run["status"] != "success":
        raise SystemExit(f"The demo run did not succeed ({run['status']}): {run.get('error')}")

    # A second shot, so nesting and the timeline have something to show.
    reuse = _api(url, f"/api/projects/{pid}/shots", {"name": "Reuse"}, "POST")
    _api(url, f"/api/projects/{pid}/timeline/from-shots", None, "POST")
    return pid, shot["id"], reuse["id"]


def shoot(page, out: Path, name: str) -> None:
    path = out / f"{name}.png"
    page.screenshot(path=str(path))
    print(f"  wrote {path}")


def reset_layout(page, url: str, project_id: str, tab: str = "shots") -> None:
    page.goto(f"{url}/p/{project_id}/{tab}", wait_until="networkidle")
    page.evaluate("() => localStorage.removeItem('comfywebstudio.layout')")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(2000)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8500")
    parser.add_argument("--out", default="docs/images")
    parser.add_argument("--keep", action="store_true", help="leave the demo project behind")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    pid, shot_id, reuse_id = build_demo(args.url)
    print(f"Built demo project {pid}")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport=VIEWPORT)

            print("Workspace")
            reset_layout(page, args.url, pid)
            page.locator(".react-flow__node").first.click(position={"x": 90, "y": 10})
            page.wait_for_timeout(1200)
            shoot(page, out, "workspace")

            print("Docking")
            canvas = page.locator("[data-dock-group]").nth(1).bounding_box()
            tab = page.get_by_role("button", name="Inspector", exact=True).first.bounding_box()
            page.mouse.move(tab["x"] + tab["width"] / 2, tab["y"] + tab["height"] / 2)
            page.mouse.down()
            page.mouse.move(
                canvas["x"] + canvas["width"] / 2, canvas["y"] + canvas["height"] * 0.88, steps=25
            )
            page.wait_for_timeout(500)
            shoot(page, out, "docking-drop-zone")
            page.mouse.up()
            page.wait_for_timeout(800)
            shoot(page, out, "docking-split")

            print("Timeline")
            reset_layout(page, args.url, pid, tab="timeline")
            page.wait_for_timeout(1500)
            shoot(page, out, "timeline")

            print("Settings")
            page.goto(f"{args.url}/settings", wait_until="networkidle")
            page.wait_for_timeout(1500)
            shoot(page, out, "settings")

            print("Nested shot")
            reset_layout(page, args.url, pid)
            page.get_by_text("Reuse", exact=True).last.click()
            page.wait_for_timeout(900)
            page.evaluate("() => { window.__dt = new DataTransfer() }")
            source = page.get_by_text("Establishing", exact=True).last
            source.dispatch_event("dragstart", {"dataTransfer": page.evaluate_handle("() => window.__dt")})
            target = page.locator(".react-flow__pane, [data-testid='empty-canvas']").first
            target.dispatch_event("dragover", {"dataTransfer": page.evaluate_handle("() => window.__dt")})
            target.dispatch_event("drop", {"dataTransfer": page.evaluate_handle("() => window.__dt")})
            page.wait_for_timeout(2500)
            shoot(page, out, "nested-shot")

            print("ComfyUI panel")
            reset_layout(page, args.url, pid)
            page.locator("button:text-is('Workflows')").first.click()
            page.wait_for_timeout(600)
            page.get_by_role("button", name="Open in ComfyUI").first.click()
            page.wait_for_timeout(22000)
            shoot(page, out, "comfy-panel")
            page.locator("button[title='Fill the workspace with ComfyUI']").click()
            page.wait_for_timeout(1500)
            shoot(page, out, "comfy-maximised")

            browser.close()
    finally:
        if not args.keep:
            try:
                _api(args.url, f"/api/projects/{pid}", method="DELETE")
            except Exception as exc:  # noqa: BLE001
                print(f"  (could not clean up {pid}: {exc})")

    print("\nDone. Commit whatever changed under", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
