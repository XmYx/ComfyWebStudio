"""Keeping a workflow saved inside ComfyUI, under a name it already knows.

Opening a workflow used to hand ComfyUI a bare graph, which it treats as an unsaved document — so it showed
up as "Unsaved Workflow" and Ctrl+S asked the user to invent a name and pick a folder. Worse, whatever they
chose was a *different* file from the one the workflow came from, so the two quietly diverged.

Instead we make sure ComfyUI has the workflow saved at a stable path first, then ask it to open *that*.
The tab is then a real, named workflow: Ctrl+S saves in place, and our bridge picks the change up.

Workflows imported from ComfyUI keep their original path, so editing one edits the file it came from.
Anything else lives under ``workflows/ComfyWebStudio/<project>/`` — namespaced so it is obvious where it
came from and it cannot collide with the user's own files.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..core.ids import slugify
from ..core.models import Project, WorkflowRef
from .backend import ComfyBackend

logger = logging.getLogger(__name__)

#: Folder inside ComfyUI's workflows directory for workflows the framework owns.
MANAGED_DIR = "ComfyWebStudio"


def managed_path(project: Project, workflow: WorkflowRef) -> str:
    """Where a framework-owned workflow lives inside ComfyUI's user directory."""
    return (
        f"workflows/{MANAGED_DIR}/{slugify(project.name)}/"
        f"{slugify(workflow.name, fallback=workflow.id)}.json"
    )


def target_path(project: Project, workflow: WorkflowRef) -> str:
    """The path to keep this workflow at — its original one when it came from ComfyUI."""
    existing = (workflow.comfy_userdata_path or "").strip()
    if existing.startswith("workflows/"):
        return existing
    return managed_path(project, workflow)


async def ensure_saved_in_comfy(
    backend: ComfyBackend,
    project: Project,
    workflow: WorkflowRef,
    graph: dict[str, Any],
) -> str | None:
    """Write the workflow into ComfyUI's user directory. Returns the path, or None if we cannot.

    Never fatal: if ComfyUI refuses the write we fall back to loading the graph directly, which is what
    happened before this existed. The user just gets the old unnamed behaviour rather than an error.
    """
    if not graph or not graph.get("nodes"):
        # An API-format-only import has no LiteGraph document to save; the caller falls back.
        return None

    path = target_path(project, workflow)
    try:
        await backend.http.write_userdata(path, json.dumps(graph), overwrite=True)
    except Exception as exc:  # noqa: BLE001 - a read-only or remote ComfyUI must not break "open"
        logger.warning("Could not save %r into ComfyUI at %s: %s", workflow.name, path, exc)
        return None

    workflow.comfy_userdata_path = path
    logger.info("Saved workflow %r into ComfyUI at %s", workflow.name, path)
    return path


async def read_from_comfy(backend: ComfyBackend, workflow: WorkflowRef) -> dict[str, Any] | None:
    """The workflow document as ComfyUI currently holds it, or None when there is nothing to read.

    ComfyUI's own Ctrl+S writes this file and tells nobody — the bridge extension only reports the graphs it
    saves itself. So re-reading the file is the only way to notice an edit made without the extension, which
    is most often *the node the user just added*.

    Raises whatever the transport raises; a re-sync treats an unreachable ComfyUI as "no update available"
    rather than an error, because the stored copy is still perfectly usable.
    """
    path = (workflow.comfy_userdata_path or "").strip()
    if not path:
        return None

    graph = json.loads(await backend.http.read_userdata(path))
    if not isinstance(graph, dict) or not graph.get("nodes"):
        logger.debug("%s does not look like a LiteGraph document; ignoring it", path)
        return None
    return graph


def is_managed(path: str | None) -> bool:
    """True when *we* created this file inside ComfyUI, so it is ours to clean up.

    A workflow imported from ComfyUI keeps its original path and is emphatically not ours — deleting it
    from the framework must never delete the user's own file.
    """
    return bool(path) and str(path).startswith(f"workflows/{MANAGED_DIR}/")


async def remove_from_comfy(backend: ComfyBackend, workflow: WorkflowRef) -> bool:
    """Delete the copy we placed in ComfyUI, if it was ours. Returns whether anything was removed."""
    path = workflow.comfy_userdata_path
    if not is_managed(path):
        return False
    try:
        await backend.http.delete_userdata(str(path))
    except Exception as exc:  # noqa: BLE001 - tidying up must not fail the delete the user asked for
        logger.warning("Could not remove %s from ComfyUI: %s", path, exc)
        return False
    logger.info("Removed the framework's copy of %r from ComfyUI (%s)", workflow.name, path)
    return True
