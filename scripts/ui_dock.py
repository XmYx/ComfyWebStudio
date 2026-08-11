#!/usr/bin/env python
"""Panels dock beside, above and below each other — not only as tabs.

The workspace layout is a tree of splits and tab groups, so dropping a panel on the edge of another one
splits that one in half rather than stacking a tab on it. This drives the real thing: drag a tab, check
where it landed, drag a splitter, check the sizes moved.

Needs the backend running with a built frontend, and at least one project.

    .venv/bin/python scripts/ui_dock.py [--url http://127.0.0.1:8500]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

FAILURES: list[str] = []

#: The dock tree, flattened to something readable: `row[(a,b) col[(c) (d)]]`.
TREE_JS = """
() => {
  const stored = localStorage.getItem('comfywebstudio.layout')
  if (!stored) return null
  const show = (node) => node.type === 'group'
    ? `(${node.tabs.join(',')})`
    : `${node.direction}[${node.children.map(show).join(' ')}]`
  // Each route has its own layout now; this suite drives the shot editor's.
  return show(JSON.parse(stored).state.workspaces.shots.tree)
}
"""

SIZES_JS = (
    "() => JSON.parse(localStorage.getItem('comfywebstudio.layout'))"
    ".state.workspaces.shots.tree.sizes"
)

#: The tab labels of each dock group, in layout order — the layout as rendered rather than as stored.
GROUPS_JS = """
() => [...document.querySelectorAll('[data-dock-group]')].map(
  (group) => [...group.querySelectorAll('[data-tabstrip] button')]
    .map((b) => b.textContent.trim()).filter((t) => t && t.length > 1)
)
"""


def check(condition: bool, message: str) -> None:
    print(f"  [{'ok  ' if condition else 'FAIL'}] {message}")
    if not condition:
        FAILURES.append(message)


def _api(url: str, path: str):
    with urllib.request.urlopen(url + path) as response:
        return json.loads(response.read())


def drag(page, start, end, steps: int = 25) -> None:
    """A press-move-release the app will read as a drag, with enough steps to fire mousemove."""
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(*end, steps=steps)
    page.wait_for_timeout(250)
    page.mouse.up()
    page.wait_for_timeout(500)


def centre_of(box) -> tuple[float, float]:
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8500")
    parser.add_argument("--shots-dir", default=None)
    args = parser.parse_args()

    from pathlib import Path

    shots = Path(args.shots_dir) if args.shots_dir else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    projects = _api(args.url, "/api/projects")
    if not projects:
        print("No projects to open; create one first.")
        return 1
    project_id = projects[0]["id"]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(f"{args.url}/p/{project_id}", wait_until="networkidle")
        # Start from the default layout rather than whatever a previous run left behind.
        page.evaluate("() => localStorage.removeItem('comfywebstudio.layout')")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)

        print("The default layout")
        # Read from the DOM rather than the stored layout: nothing has changed yet, so the store has not
        # been persisted, and the rendered tab strips are what the user is actually looking at.
        rendered = page.evaluate(GROUPS_JS)
        check(
            # Timeline, ComfyUI, Monitor and Renders share these groups but start hidden, so they are
            # not drawn as tabs — the columns are the three panels a new workspace actually shows.
            rendered == [["Shots", "Workflows", "Assets"], ["Canvas"], ["Inspector"]],
            f"the visible panels are grouped into three columns ({rendered})",
        )
        check(page.locator("[role=separator]").count() >= 1, "with splitters between them")

        print("Dropping a panel on the bottom edge of another")
        canvas = page.locator("[data-dock-group]").nth(1).bounding_box()
        tab = page.get_by_role("button", name="Inspector").first.bounding_box()
        drag(page, centre_of(tab), (canvas["x"] + canvas["width"] / 2, canvas["y"] + canvas["height"] * 0.9))
        tree = page.evaluate(TREE_JS)
        check(
            tree == "row[(shots,workflows,assets) column[(canvas,comfy) (inspector)] (monitor,renders)]",
            f"splits it into a column, the panel underneath ({tree})",
        )
        if shots:
            page.screenshot(path=str(shots / "dock-split-below.png"))

        print("Dragging a splitter")
        before = page.evaluate(SIZES_JS)
        seam = page.locator("[role=separator]").first.bounding_box()
        drag(page, centre_of(seam), (seam["x"] + 200, seam["y"] + seam["height"] / 2))
        after = page.evaluate(SIZES_JS)
        check(after[0] > before[0] + 0.05, f"widens the panel before it ({before[0]:.2f} → {after[0]:.2f})")
        check(
            abs(sum(after) - 1) < 0.001 and after[-1] == before[-1],
            "and takes the space from its neighbour only, not the whole row",
        )

        print("Dropping a panel on the workspace rim")
        tab = page.get_by_role("button", name="Shots").first.bounding_box()
        drag(page, centre_of(tab), (800, 893))
        tree = page.evaluate(TREE_JS)
        check(
            tree.startswith("column[") and tree.rstrip("]").endswith("(shots)"),
            f"spans the full width along the bottom ({tree})",
        )
        if shots:
            page.screenshot(path=str(shots / "dock-rim.png"))

        print("Dropping a panel into the middle of a group")
        tab = page.get_by_role("button", name="Shots").first.bounding_box()
        target = page.locator("[data-dock-group]").nth(0).bounding_box()
        drag(page, centre_of(tab), centre_of(target))
        tree = page.evaluate(TREE_JS)
        check("(workflows,assets,shots)" in tree, f"tabs it in alongside ({tree})")
        check(
            # The bottom strip held only Shots, so the column wrapping the workspace has to collapse back
            # to the row underneath it. The column made earlier, which still holds two panels, must stay.
            tree.startswith("row[") and "column[(canvas,comfy) (inspector)]" in tree,
            f"and the strip it vacated collapses, leaving the other split alone ({tree})",
        )

        real = [e for e in errors if "favicon" not in e.lower()]
        check(not real, f"no page errors ({len(real)} found)")
        for error in real[:5]:
            print(f"        {error[:160]}")

        browser.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Dock layout test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
