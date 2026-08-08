"""Undo / redo for project edits.

Every mutation in this app goes through :meth:`ProjectStore.save`, so snapshotting there catches all of
them — renaming a shot, moving a step, wiring a link, trimming a clip — without each endpoint having to
remember to record anything.

Snapshots are the serialised project, held in memory and bounded. They are deliberately *not* persisted:
undo history that survives a restart would be surprising, and it would mean a crash could resurrect a
project state the user had deliberately moved past.

Run results are not snapshotted — they live in ``runs/`` and are append-only, so undoing an edit never
throws away work ComfyUI actually did.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DEPTH = 50


class ProjectHistory:
    """Bounded undo/redo stacks, one pair per project."""

    def __init__(self, depth: int = DEFAULT_DEPTH):
        self.depth = depth
        self._undo: dict[str, deque[dict[str, Any]]] = {}
        self._redo: dict[str, deque[dict[str, Any]]] = {}
        #: Set while we are applying an undo, so the resulting save is not itself recorded.
        self._suspended: set[str] = set()

    def record(self, project_id: str, snapshot: dict[str, Any]) -> None:
        """Push the state as it was *before* the change now being saved."""
        if project_id in self._suspended:
            return
        stack = self._undo.setdefault(project_id, deque(maxlen=self.depth))
        # A save that changed nothing but the timestamp is not worth an undo entry.
        if stack and _same_content(stack[-1], snapshot):
            return
        stack.append(snapshot)
        # Any new edit invalidates the redo branch, as in every editor.
        self._redo.pop(project_id, None)

    def can_undo(self, project_id: str) -> bool:
        return bool(self._undo.get(project_id))

    def can_redo(self, project_id: str) -> bool:
        return bool(self._redo.get(project_id))

    def undo(self, project_id: str, current: dict[str, Any]) -> dict[str, Any] | None:
        stack = self._undo.get(project_id)
        if not stack:
            return None
        snapshot = stack.pop()
        self._redo.setdefault(project_id, deque(maxlen=self.depth)).append(current)
        return snapshot

    def redo(self, project_id: str, current: dict[str, Any]) -> dict[str, Any] | None:
        stack = self._redo.get(project_id)
        if not stack:
            return None
        snapshot = stack.pop()
        self._undo.setdefault(project_id, deque(maxlen=self.depth)).append(current)
        return snapshot

    def suspend(self, project_id: str) -> _Suspension:
        return _Suspension(self, project_id)

    def clear(self, project_id: str) -> None:
        self._undo.pop(project_id, None)
        self._redo.pop(project_id, None)

    def depths(self, project_id: str) -> dict[str, int]:
        return {
            "undo": len(self._undo.get(project_id, ())),
            "redo": len(self._redo.get(project_id, ())),
        }


class _Suspension:
    def __init__(self, history: ProjectHistory, project_id: str):
        self._history = history
        self._project_id = project_id

    def __enter__(self) -> None:
        self._history._suspended.add(self._project_id)

    def __exit__(self, *_exc) -> None:
        self._history._suspended.discard(self._project_id)


def _same_content(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Compare ignoring ``modified``, which changes on every save by definition."""
    return {k: v for k, v in a.items() if k != "modified"} == {
        k: v for k, v in b.items() if k != "modified"
    }
