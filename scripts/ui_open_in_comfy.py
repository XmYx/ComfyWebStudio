#!/usr/bin/env python
"""End-to-end check of "Open in ComfyUI", driving the real ComfyUI.

The behaviour under test: the tab that opens must be a *named, saved* workflow, not "Unsaved Workflow".
That is what makes Ctrl+S in ComfyUI save in place, to the same file the framework reads back, instead of
prompting the user to invent a name.

Needs both servers running, and the node pack installed in ComfyUI.

    .venv/bin/python scripts/ui_open_in_comfy.py [--url ...] [--comfy http://127.0.0.1:8199]
"""

from __future__ import annotations

import argparse
import json
import sys
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


PROMPT = {
    "1": {"class_type": "WSStringInput", "inputs": {"port_name": "caption", "value": "hello"}},
    "2": {"class_type": "EmptyImage",
          "inputs": {"width": 32, "height": 32, "batch_size": 1, "color": 0x3366FF}},
    "3": {"class_type": "WSImageOutput",
          "inputs": {"image": ["2", 0], "port_name": "image", "format": "png",
                     "run_key": "", "quality": 92, "lossless": False}},
}


def active_workflow(page) -> dict:
    return page.evaluate(
        """() => {
            const w = window.app?.extensionManager?.workflow?.activeWorkflow;
            return w ? {path: w.path, filename: w.filename, isTemporary: !!w.isTemporary,
                        isModified: !!w.isModified} : null;
        }"""
    )


def open_in_comfy(page, url: str, comfy: str, project_id: str, workflow_id: str) -> dict:
    payload = _api(url, f"/api/projects/{project_id}/workflows/{workflow_id}/open-in-comfy", {}, "POST")
    page.goto(payload["url"], wait_until="networkidle")
    # ComfyUI's frontend boots slowly; the extension runs in its setup hook.
    page.wait_for_function(
        "() => window.app?.extensionManager?.workflow !== undefined", timeout=60000
    )
    page.wait_for_timeout(6000)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8500")
    parser.add_argument("--comfy", default="http://127.0.0.1:8199")
    parser.add_argument("--shots-dir", default=None)
    args = parser.parse_args()

    from pathlib import Path

    shots = Path(args.shots_dir) if args.shots_dir else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    project = _api(args.url, "/api/projects", {"name": "Open In Comfy"}, "POST")
    pid = project["id"]

    # A workflow imported in API format has no LiteGraph document — the harder of the two cases.
    api_only = _api(args.url, f"/api/projects/{pid}/workflows",
                    {"name": "Api Only", "prompt": PROMPT}, "POST")
    # And one that came from ComfyUI's own directory, which must reopen its original file.
    from_comfy = _api(args.url, f"/api/comfy/projects/{pid}/import", {"path": "LTX2_TXT2IMG.json"}, "POST")

    print(f"Using scratch project {pid}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        print("A workflow that came from ComfyUI")
        payload = open_in_comfy(page, args.url, args.comfy, pid, from_comfy["id"])
        check(
            payload["comfy_path"] == "workflows/LTX2_TXT2IMG.json",
            f"reopens its original file ({payload['comfy_path']})",
        )
        active = active_workflow(page)
        check(active is not None, "a workflow is active")
        check(
            bool(active) and active["path"] == "workflows/LTX2_TXT2IMG.json",
            f"the open tab is that workflow (got {active and active['path']})",
        )
        check(bool(active) and not active["isTemporary"], "it is a saved workflow, not an unsaved one")
        check(
            bool(active) and "unsaved" not in (active["filename"] or "").lower(),
            f"named {active and active['filename']!r}, not Unsaved Workflow",
        )
        check(
            page.locator("#comfywebstudio-badge").count() == 1
            and "Linked" in page.locator("#comfywebstudio-badge").inner_text(),
            "the bridge badge shows it is linked",
        )
        if shots:
            page.screenshot(path=str(shots / "open-from-comfy.png"))

        print("A workflow imported in API format")
        payload = open_in_comfy(page, args.url, args.comfy, pid, api_only["id"])
        page.wait_for_timeout(4000)  # it saves itself back, then reopens by path
        active = active_workflow(page)
        check(bool(active), "a workflow is active")
        check(
            bool(active) and not active["isTemporary"],
            f"it too became a saved workflow (path={active and active['path']})",
        )
        check(
            bool(active) and "ComfyWebStudio" in (active["path"] or ""),
            "saved under the framework's own folder rather than loose in the user's workflows",
        )

        stored = _api(args.url, f"/api/projects/{pid}/workflows/{api_only['id']}")
        check(
            bool(stored["comfy_userdata_path"]),
            f"the framework recorded where it lives ({stored['comfy_userdata_path']})",
        )
        check(
            bool(active) and stored["comfy_userdata_path"] == active["path"],
            "and that matches what ComfyUI has open",
        )
        if shots:
            page.screenshot(path=str(shots / "open-api-only.png"))

        print("ComfyUI can list it")
        listed = _api(args.url, "/api/comfy/workflows")
        paths = {w["path"] for w in listed["workflows"]}
        check(
            any(p.startswith("ComfyWebStudio/") for p in paths),
            "the saved copy appears in ComfyUI's workflow list",
        )

        print("Cleanup rules")
        managed = _api(args.url, f"/api/projects/{pid}/workflows/{api_only['id']}")["comfy_userdata_path"]
        original = _api(args.url, f"/api/projects/{pid}/workflows/{from_comfy['id']}")["comfy_userdata_path"]
        _api(args.url, f"/api/projects/{pid}/workflows/{api_only['id']}", method="DELETE")
        _api(args.url, f"/api/projects/{pid}/workflows/{from_comfy['id']}", method="DELETE")
        remaining = {w["path"] for w in _api(args.url, "/api/comfy/workflows")["workflows"]}
        check(
            managed.removeprefix("workflows/") not in remaining,
            "deleting a framework-owned workflow removes its copy from ComfyUI",
        )
        check(
            original.removeprefix("workflows/") in remaining,
            "deleting one imported from ComfyUI leaves the user's own file alone",
        )

        real = [e for e in errors if "favicon" not in e.lower()]
        check(not real, f"no page errors ({len(real)} found)")
        for error in real[:5]:
            print(f"        {error[:160]}")

        browser.close()

    try:
        # The workflows were already removed by the cleanup-rules checks above, which is what took our
        # copies out of ComfyUI with them.
        _api(args.url, f"/api/projects/{pid}", method="DELETE")
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not clean up: {exc})")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Open-in-ComfyUI test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
