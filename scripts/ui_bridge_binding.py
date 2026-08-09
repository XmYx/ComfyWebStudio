#!/usr/bin/env python
"""Each ComfyUI workflow syncs back to its own step, and only its own.

The bug this guards against: the bridge used to keep one binding for the whole ComfyUI origin, in
localStorage. Every tab shares that, and auto-sync fires on every graph change — so editing workflow B
pushed B's graph into whichever step was linked last. Adding an input to one workflow made every other
workflow "inherit" it.

The check opens two different workflows from the framework in one browser (so they share localStorage),
saves from the first, and asserts the second is untouched.

Needs both servers running, and the node pack installed in ComfyUI.

    .venv/bin/python scripts/ui_bridge_binding.py [--url http://127.0.0.1:8500]
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


def fingerprint(url: str, project_id: str, workflow_id: str) -> dict:
    """What a sync would change: the graph hash and the discovered ports."""
    workflow = _api(url, f"/api/projects/{project_id}/workflows/{workflow_id}")
    return {
        "hash": workflow["hash"],
        "ports": sorted(p["key"] for p in workflow["ports"]),
        "params": sorted(p["key"] for p in workflow["params"]),
    }


def open_in_comfy(context, url: str, project_id: str, workflow_id: str):
    """Open a workflow in its own ComfyUI tab and wait until its graph is really on the canvas."""
    payload = _api(url, f"/api/projects/{project_id}/workflows/{workflow_id}/open-in-comfy", {}, "POST")
    page = context.new_page()
    page.goto(payload["url"], wait_until="domcontentloaded")
    page.wait_for_function("() => window.app?.extensionManager?.workflow !== undefined", timeout=90000)
    page.wait_for_function(
        "() => (window.app?.canvas?.graph?._nodes?.length ?? 0) > 0", timeout=90000
    )
    page.wait_for_timeout(1500)
    return page, payload


def active_path(page) -> str | None:
    return page.evaluate(
        "() => window.app?.extensionManager?.workflow?.activeWorkflow?.path ?? null"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8500")
    parser.add_argument("--first", default="image_krea2_turbo_t2i.json")
    parser.add_argument("--second", default="LTX2_TXT2IMG.json")
    args = parser.parse_args()

    project = _api(args.url, "/api/projects", {"name": "Bridge Binding"}, "POST")
    pid = project["id"]
    print(f"Using scratch project {pid}")

    try:
        first = _api(args.url, f"/api/comfy/projects/{pid}/import", {"path": args.first}, "POST")
        second = _api(args.url, f"/api/comfy/projects/{pid}/import", {"path": args.second}, "POST")

        before_first = fingerprint(args.url, pid, first["id"])
        before_second = fingerprint(args.url, pid, second["id"])
        check(
            before_first != before_second,
            "the two workflows start out different (otherwise this proves nothing)",
        )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            # One context, so both tabs share localStorage — the condition the bug needed.
            context = browser.new_context(viewport={"width": 1500, "height": 900})

            page_a, payload_a = open_in_comfy(context, args.url, pid, first["id"])
            check(
                active_path(page_a) == payload_a["comfy_path"],
                f"tab A opened {args.first} ({active_path(page_a)})",
            )

            page_b, payload_b = open_in_comfy(context, args.url, pid, second["id"])
            check(
                active_path(page_b) == payload_b["comfy_path"],
                f"tab B opened {args.second} ({active_path(page_b)})",
            )

            # Saving from A must go to A. Under the old global binding this wrote A's graph into B,
            # because B was linked last.
            page_a.bring_to_front()
            page_a.locator("#comfywebstudio-badge").click()
            page_a.wait_for_function(
                "() => (document.getElementById('comfywebstudio-badge')?.textContent ?? '')"
                ".includes('Synced')",
                timeout=30000,
            )

            after_first = fingerprint(args.url, pid, first["id"])
            after_second = fingerprint(args.url, pid, second["id"])

            check(after_second == before_second, "saving from tab A left workflow B completely untouched")
            check(
                after_first["ports"] == before_first["ports"],
                "and workflow A still has its own ports",
            )
            check(
                not set(after_first["ports"]) & set(before_second["ports"]) - set(before_first["ports"]),
                "no port leaked from B into A",
            )

            # The badge must describe whatever workflow is in front of the user, not the last linked one.
            page_b.bring_to_front()
            page_b.wait_for_timeout(1500)
            badge_b = page_b.locator("#comfywebstudio-badge").inner_text()
            check("Linked" in badge_b, f"tab B still reports its own link ({badge_b})")

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
    print("Bridge binding test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
