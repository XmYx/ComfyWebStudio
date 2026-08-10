"""Version history.

Every save appends a **version** — a timestamped snapshot plus a description of what changed. That single
log serves three purposes:

* **Undo / redo** is a pointer moving along it.
* **Global history** is the log itself: every change to the project, in order, restorable.
* **Element history** is the log filtered by ``target_id``: everything that ever happened to one shot, one
  step, one clip — and any of those can be restored on its own, without reverting anything else.

Snapshots are content-addressed and gzipped, so an undo/redo cycle or a no-op save costs nothing extra.

    <project>/history/
        log.jsonl                 append-only, one version per line
        snapshots/<sha>.json.gz

Unlike the earlier in-memory stack this survives a restart, which is the point: "what did I change
yesterday, and can I put that one step back?" is only answerable if the log outlives the process.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .diffing import Change, diff_projects, summarize
from .errors import NotFound, ValidationFailed
from .ids import new_id

logger = logging.getLogger(__name__)

LOG_FILE = "log.jsonl"
SNAPSHOT_DIR = "snapshots"
POINTER_FILE = "pointer.json"

#: Versions kept per project before the oldest are pruned.
MAX_VERSIONS = 500

#: Scopes whose element can be restored on its own.
RESTORABLE_SCOPES = frozenset({"shot", "step", "timeline", "track", "workflow", "project"})


@dataclass(slots=True)
class Version:
    id: str
    ts: str
    snapshot: str
    changes: list[Change] = field(default_factory=list)
    label: str | None = None
    summary: str = ""
    #: Scopes touched, so the UI can filter without unpacking every change.
    scopes: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "snapshot": self.snapshot,
            "label": self.label,
            "summary": self.summary,
            "scopes": self.scopes,
            "targets": self.targets,
            "changes": [c.to_dict() for c in self.changes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Version:
        return cls(
            id=str(data["id"]),
            ts=str(data.get("ts", "")),
            snapshot=str(data.get("snapshot", "")),
            changes=[Change.from_dict(c) for c in data.get("changes") or []],
            label=data.get("label"),
            summary=str(data.get("summary", "")),
            scopes=list(data.get("scopes") or []),
            targets=list(data.get("targets") or []),
        )

    @property
    def is_named(self) -> bool:
        return bool(self.label)


class VersionStore:
    """The change log for one project."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._suspended = False

    # -- paths -------------------------------------------------------------------------------------

    @property
    def directory(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    @property
    def log_path(self) -> Path:
        return self.directory / LOG_FILE

    @property
    def snapshot_dir(self) -> Path:
        path = self.directory / SNAPSHOT_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- reading -----------------------------------------------------------------------------------

    def all(self) -> list[Version]:
        """Every version, oldest first."""
        if not self.log_path.is_file():
            return []
        versions: list[Version] = []
        with open(self.log_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    versions.append(Version.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError) as exc:
                    logger.warning("Skipping malformed history entry: %s", exc)
        return versions

    def list(
        self,
        *,
        scope: str | None = None,
        target_id: str | None = None,
        include_layout: bool = False,
        named_only: bool = False,
        limit: int = 100,
    ) -> list[Version]:
        """Versions newest first, optionally narrowed to one element.

        Filtering by ``target_id`` is what makes per-element history work: each version keeps the set of
        targets it touched, so a step's history is a scan rather than a diff of every snapshot.
        """
        results: list[Version] = []
        for version in reversed(self.all()):
            if named_only and not version.is_named:
                continue
            if target_id is not None and target_id not in version.targets:
                continue
            if scope is not None and scope not in version.scopes:
                continue

            changes = version.changes
            if target_id is not None:
                changes = [c for c in changes if c.target_id == target_id]
            if scope is not None:
                changes = [c for c in changes if c.scope == scope]
            if not include_layout:
                visible = [c for c in changes if not c.detail.get("layout")]
                # A version that only moved things is hidden unless it is a named checkpoint.
                if not visible and not version.is_named:
                    continue
                changes = visible or changes

            results.append(
                Version(
                    id=version.id, ts=version.ts, snapshot=version.snapshot, changes=changes,
                    label=version.label, summary=summarize(changes) if changes else version.summary,
                    scopes=version.scopes, targets=version.targets,
                )
            )
            if len(results) >= limit:
                break
        return results

    def get(self, version_id: str) -> Version:
        for version in self.all():
            if version.id == version_id:
                return version
        raise NotFound(f"No version {version_id!r} in this project's history")

    def snapshot(self, version_id: str) -> dict[str, Any]:
        return self._read_snapshot(self.get(version_id).snapshot)

    def _read_snapshot(self, sha: str) -> dict[str, Any]:
        path = self.snapshot_dir / f"{sha}.json.gz"
        if not path.is_file():
            raise NotFound("That version's snapshot is no longer stored (it may have been pruned).")
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)

    # -- writing -----------------------------------------------------------------------------------

    def record(
        self, before: dict[str, Any] | None, after: dict[str, Any], *, label: str | None = None
    ) -> Version | None:
        """Append a version for a save. Returns None when nothing meaningful changed."""
        if self._suspended:
            return None

        changes = diff_projects(before, after)
        if not changes and not label:
            return None

        sha = self._write_snapshot(after)
        version = Version(
            id=new_id("v", 12),
            ts=datetime.now(UTC).isoformat(),
            snapshot=sha,
            changes=changes,
            label=label,
            summary=summarize(changes),
            scopes=sorted({c.scope for c in changes}),
            targets=sorted({c.target_id for c in changes if c.target_id}),
        )

        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(version.to_dict(), ensure_ascii=False) + "\n")

        # A new edit invalidates anything that was ahead of the undo pointer.
        self._set_pointer(None)
        self._prune()
        return version

    def tag(self, label: str, project: dict[str, Any]) -> Version:
        """Name the current state, so it can be found again without scrolling the log."""
        label = (label or "").strip()
        if not label:
            raise ValidationFailed("A version needs a name.")

        versions = self.all()
        if versions and versions[-1].snapshot == _hash(project):
            # Nothing has changed since the last version; label that one rather than duplicating it.
            versions[-1].label = label
            self._rewrite(versions)
            return versions[-1]

        version = Version(
            id=new_id("v", 12),
            ts=datetime.now(UTC).isoformat(),
            snapshot=self._write_snapshot(project),
            label=label,
            summary=f"Named version “{label}”",
        )
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(version.to_dict(), ensure_ascii=False) + "\n")
        self._set_pointer(None)
        return version

    def relabel(self, version_id: str, label: str | None) -> Version:
        versions = self.all()
        for version in versions:
            if version.id == version_id:
                version.label = (label or "").strip() or None
                self._rewrite(versions)
                return version
        raise NotFound(f"No version {version_id!r}")

    def _write_snapshot(self, project: dict[str, Any]) -> str:
        sha = _hash(project)
        path = self.snapshot_dir / f"{sha}.json.gz"
        if not path.is_file():
            temp = _temp_beside(path)
            try:
                with gzip.open(temp, "wt", encoding="utf-8") as handle:
                    json.dump(project, handle, ensure_ascii=False)
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
        return sha

    def _rewrite(self, versions: list[Version]) -> None:
        temp = _temp_beside(self.log_path)
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                for version in versions:
                    handle.write(json.dumps(version.to_dict(), ensure_ascii=False) + "\n")
            os.replace(temp, self.log_path)
        finally:
            temp.unlink(missing_ok=True)

    def _prune(self) -> None:
        """Drop the oldest unnamed versions, and any snapshot nothing references any more."""
        versions = self.all()
        if len(versions) <= MAX_VERSIONS:
            return

        excess = len(versions) - MAX_VERSIONS
        keep: list[Version] = []
        dropped = 0
        for version in versions:
            # Named versions are explicit user checkpoints; never prune those.
            if dropped < excess and not version.is_named:
                dropped += 1
                continue
            keep.append(version)

        self._rewrite(keep)
        referenced = {v.snapshot for v in keep}
        for path in self.snapshot_dir.glob("*.json.gz"):
            if path.stem.removesuffix(".json") not in referenced:
                path.unlink(missing_ok=True)

    # -- undo / redo -------------------------------------------------------------------------------
    #
    # The pointer is an index into the log. Normally it is None, meaning "at the newest version".
    # Undo walks it backwards; redo walks it forwards; a new edit resets it.

    def _pointer(self) -> int | None:
        path = self.directory / POINTER_FILE
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("index")
        except (OSError, json.JSONDecodeError):
            return None

    def _set_pointer(self, index: int | None) -> None:
        path = self.directory / POINTER_FILE
        if index is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps({"index": index}), encoding="utf-8")

    def depths(self) -> dict[str, int]:
        versions = self.all()
        if not versions:
            return {"undo": 0, "redo": 0}
        index = self._pointer()
        current = len(versions) - 1 if index is None else index
        return {"undo": max(0, current), "redo": max(0, len(versions) - 1 - current)}

    def undo(self) -> dict[str, Any] | None:
        versions = self.all()
        if len(versions) < 2:
            return None
        index = self._pointer()
        current = len(versions) - 1 if index is None else index
        if current <= 0:
            return None
        self._set_pointer(current - 1)
        return self._read_snapshot(versions[current - 1].snapshot)

    def redo(self) -> dict[str, Any] | None:
        versions = self.all()
        index = self._pointer()
        if index is None or index >= len(versions) - 1:
            return None
        self._set_pointer(index + 1)
        return self._read_snapshot(versions[index + 1].snapshot)

    def suspend(self) -> _Suspension:
        return _Suspension(self)

    def clear(self) -> None:
        self.log_path.unlink(missing_ok=True)
        (self.directory / POINTER_FILE).unlink(missing_ok=True)
        for path in self.snapshot_dir.glob("*.json.gz"):
            path.unlink(missing_ok=True)

    # -- element restore ---------------------------------------------------------------------------

    def restore_element(
        self, version_id: str, scope: str, target_id: str, current: dict[str, Any]
    ) -> dict[str, Any]:
        """Put one element back the way it was, leaving everything else alone.

        This is the difference between "undo the last twenty things" and "put *this step's* parameters
        back to how they were on Tuesday".
        """
        if scope not in RESTORABLE_SCOPES:
            raise ValidationFailed(f"A {scope} cannot be restored on its own.")

        snapshot = self.snapshot(version_id)
        result = json.loads(json.dumps(current))  # deep copy

        if scope == "project":
            return snapshot

        if scope == "workflow":
            workflow = (snapshot.get("workflows") or {}).get(target_id)
            if workflow is None:
                raise NotFound("That workflow does not exist in the selected version.")
            result.setdefault("workflows", {})[target_id] = workflow
            return result

        if scope == "timeline":
            result["timeline"] = snapshot.get("timeline") or result.get("timeline")
            return result

        if scope == "track":
            track = _find_track(snapshot, target_id)
            if track is None:
                raise NotFound("That track does not exist in the selected version.")
            tracks = (result.get("timeline") or {}).get("tracks") or []
            for index, existing in enumerate(tracks):
                if existing.get("id") == target_id:
                    tracks[index] = track
                    return result
            tracks.append(track)
            return result

        if scope == "shot":
            shot = _find_shot(snapshot, target_id)
            if shot is None:
                raise NotFound("That shot does not exist in the selected version.")
            shots = result.setdefault("shots", [])
            for index, existing in enumerate(shots):
                if existing.get("id") == target_id:
                    shots[index] = shot
                    return result
            shots.append(shot)
            return result

        # scope == "step": replace the step in place, keeping the rest of its shot untouched.
        found = _find_step(snapshot, target_id)
        if found is None:
            raise NotFound("That step does not exist in the selected version.")
        _, step = found

        for shot in result.get("shots") or []:
            for index, existing in enumerate(shot.get("steps") or []):
                if existing.get("id") == target_id:
                    shot["steps"][index] = step
                    return result

        raise NotFound("That step is no longer in this project, so it cannot be restored in place.")


class _Suspension:
    def __init__(self, store: VersionStore):
        self._store = store

    def __enter__(self) -> None:
        self._store._suspended = True

    def __exit__(self, *_exc) -> None:
        self._store._suspended = False


# -- helpers ---------------------------------------------------------------------------------------------


def _temp_beside(path: Path) -> Path:
    """A scratch name next to ``path``, unique per write.

    Same reasoning as the project store: two writers sharing one temp name truncate each other's work
    mid-write and can leave a file that no longer parses. Same directory, so ``os.replace`` stays atomic.
    """
    return path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")


def _hash(project: dict[str, Any]) -> str:
    """Content hash ignoring ``modified``, so a save that changed nothing reuses the same snapshot."""
    payload = {k: v for k, v in project.items() if k != "modified"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def _find_shot(project: dict[str, Any], shot_id: str) -> dict[str, Any] | None:
    return next((s for s in project.get("shots") or [] if s.get("id") == shot_id), None)


def _find_step(project: dict[str, Any], step_id: str) -> tuple[dict, dict] | None:
    for shot in project.get("shots") or []:
        for step in shot.get("steps") or []:
            if step.get("id") == step_id:
                return shot, step
    return None


def _find_track(project: dict[str, Any], track_id: str) -> dict[str, Any] | None:
    tracks = (project.get("timeline") or {}).get("tracks") or []
    return next((t for t in tracks if t.get("id") == track_id), None)
