"""Step result cache.

A step is skippable when nothing that determines its output has changed: the workflow graph, the resolved
parameter values, and the content of every upstream artifact feeding it. Those three are hashed into one
cache key.

Upstream artifacts are keyed by *content* hash, not by path, so a rerun that regenerates a byte-identical
image still counts as a hit — which is what makes re-running a long chain after editing only its last step
cheap.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from ..core.models import Artifact, StepRun
from ..core.store import ProjectStore

logger = logging.getLogger(__name__)

INDEX_FILE = ".cache-index.json"

#: Cap the index so a long-lived project does not accumulate an unbounded map.
MAX_ENTRIES = 2000


def compute_cache_key(
    *,
    workflow_hash: str,
    resolved_params: dict[str, Any],
    upstream: dict[str, str],
    output_ports: list[str],
) -> str:
    """Hash everything that determines a step's output.

    ``upstream`` maps input port key to the SHA of the artifact feeding it. Output port names are included
    because adding an output port changes what the step must produce, even though nothing else moved.
    """
    payload = {
        "workflow": workflow_hash,
        "params": {k: resolved_params[k] for k in sorted(resolved_params)},
        "upstream": {k: upstream[k] for k in sorted(upstream)},
        "outputs": sorted(output_ports),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:40]


class CacheIndex:
    """Maps cache key to the run that produced it, so a hit is one file read rather than a scan."""

    def __init__(self, store: ProjectStore, project_id: str):
        self.store = store
        self.project_id = project_id
        self._path = store.project_dir(project_id) / "runs" / INDEX_FILE
        self._data: dict[str, dict[str, str]] | None = None

    def _load(self) -> dict[str, dict[str, str]]:
        if self._data is None:
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._data = {}
        return self._data

    def _save(self) -> None:
        data = self._load()
        if len(data) > MAX_ENTRIES:
            # Keep the newest half; entries are appended in run order.
            data = dict(list(data.items())[-MAX_ENTRIES // 2 :])
            self._data = data
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temp, self._path)

    def record(self, cache_key: str, run_id: str, step_id: str) -> None:
        if not cache_key:
            return
        data = self._load()
        data.pop(cache_key, None)  # re-append so insertion order tracks recency
        data[cache_key] = {"run_id": run_id, "step_id": step_id}
        self._save()

    def lookup(self, cache_key: str) -> StepRun | None:
        """The previous successful StepRun for this key, or None if it is gone or its files are missing."""
        if not cache_key:
            return None
        entry = self._load().get(cache_key)
        if not entry:
            return None

        try:
            run = self.store.load_run(self.project_id, entry["run_id"])
        except Exception:  # noqa: BLE001 - a deleted run is a miss, not an error
            self._forget(cache_key)
            return None

        step_run = run.step_run(entry["step_id"])
        if step_run is None or step_run.status not in {"success", "cached"}:
            self._forget(cache_key)
            return None

        if not self._artifacts_present(step_run.outputs):
            logger.debug("Cache entry %s is stale: artifacts missing", cache_key[:8])
            self._forget(cache_key)
            return None

        return step_run

    def _artifacts_present(self, artifacts: list[Artifact]) -> bool:
        for artifact in artifacts:
            try:
                path = self.store.resolve(self.project_id, artifact.path)
            except Exception:  # noqa: BLE001
                return False
            if not Path(path).is_file():
                return False
        return True

    def _forget(self, cache_key: str) -> None:
        data = self._load()
        if data.pop(cache_key, None) is not None:
            self._save()

    def clear(self) -> None:
        self._data = {}
        self._save()
