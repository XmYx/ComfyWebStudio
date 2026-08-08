"""Plugin packs.

A plugin is a shareable bundle of *reusable project content* — the workflows and shot structures you have
built up — so a setup can be moved between machines or handed to someone else without exporting a whole
project full of renders.

    <name>.cwsplugin  (zip)
      plugin.json                 manifest
      workflows/<id>.ui.json      the LiteGraph document, when one exists
      workflows/<id>.api.json     the runnable prompt
      thumbnails/<id>.webp        optional

Installed plugins live in ``<root>/plugins/<plugin_id>/``. Installing never touches a project; applying a
plugin copies its content into the project you choose, with fresh ids, so the plugin stays a template
rather than becoming shared mutable state.

This is deliberately *content* rather than executable code: a plugin that could run arbitrary Python or JS
would make "load a plugin someone sent you" a genuinely dangerous action.
"""

from __future__ import annotations

import json
import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import Conflict, NotFound, ValidationFailed
from .ids import new_id, safe_component, slugify
from .models import Project, Shot, Step, WorkflowRef

logger = logging.getLogger(__name__)

MANIFEST_NAME = "plugin.json"
PLUGIN_SUFFIX = ".cwsplugin"
FORMAT_VERSION = 1


@dataclass(slots=True)
class PluginManifest:
    id: str
    name: str
    version: str = "1.0.0"
    author: str = ""
    description: str = ""
    format_version: int = FORMAT_VERSION
    created: str = ""
    #: Workflow descriptors: {id, name, ports, params, has_ui_graph}
    workflows: list[dict[str, Any]] = field(default_factory=list)
    #: Shot templates: {name, steps:[{name, workflow_id, param_overrides}], links:[...]}
    shot_templates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "format_version": self.format_version,
            "created": self.created or datetime.now().astimezone().isoformat(),
            "workflows": self.workflows,
            "shot_templates": self.shot_templates,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginManifest:
        if not data.get("id") or not data.get("name"):
            raise ValidationFailed("A plugin manifest needs at least an id and a name.")
        if int(data.get("format_version", 1)) > FORMAT_VERSION:
            raise ValidationFailed(
                f"{data.get('name')!r} was made by a newer version of ComfyWebStudio."
            )
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            version=str(data.get("version", "1.0.0")),
            author=str(data.get("author", "")),
            description=str(data.get("description", "")),
            format_version=int(data.get("format_version", 1)),
            created=str(data.get("created", "")),
            workflows=list(data.get("workflows") or []),
            shot_templates=list(data.get("shot_templates") or []),
        )


class PluginStore:
    """Installs, lists and applies plugin packs."""

    def __init__(self, root: Path, project_store):
        self.root = Path(root)
        self.project_store = project_store

    @property
    def directory(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def _plugin_dir(self, plugin_id: str) -> Path:
        return self.directory / safe_component(plugin_id, "plugin")

    # -- listing -----------------------------------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        plugins: list[dict[str, Any]] = []
        for entry in sorted(self.directory.iterdir()):
            manifest_path = entry / MANIFEST_NAME
            if not manifest_path.is_file():
                continue
            try:
                manifest = PluginManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, ValidationFailed) as exc:
                logger.warning("Skipping unreadable plugin at %s: %s", entry, exc)
                continue
            payload = manifest.to_dict()
            payload["enabled"] = not (entry / ".disabled").exists()
            payload["path"] = str(entry)
            plugins.append(payload)
        return plugins

    def get(self, plugin_id: str) -> PluginManifest:
        path = self._plugin_dir(plugin_id) / MANIFEST_NAME
        if not path.is_file():
            raise NotFound(f"No plugin {plugin_id!r} is installed.")
        return PluginManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        marker = self._plugin_dir(plugin_id) / ".disabled"
        if not marker.parent.is_dir():
            raise NotFound(f"No plugin {plugin_id!r} is installed.")
        if enabled:
            marker.unlink(missing_ok=True)
        else:
            marker.touch()

    def uninstall(self, plugin_id: str) -> None:
        directory = self._plugin_dir(plugin_id)
        if not directory.is_dir():
            raise NotFound(f"No plugin {plugin_id!r} is installed.")
        shutil.rmtree(directory)
        logger.info("Uninstalled plugin %s", plugin_id)

    # -- install -----------------------------------------------------------------------------------

    def install(self, archive_path: Path, *, overwrite: bool = False) -> PluginManifest:
        archive_path = Path(archive_path)
        if not archive_path.is_file():
            raise NotFound(f"No such file: {archive_path}")

        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            if MANIFEST_NAME not in names:
                raise ValidationFailed(
                    f"{archive_path.name} is not a ComfyWebStudio plugin (no {MANIFEST_NAME})."
                )
            manifest = PluginManifest.from_dict(json.loads(archive.read(MANIFEST_NAME)))

            target = self._plugin_dir(manifest.id)
            if target.is_dir() and not overwrite:
                existing = self.get(manifest.id)
                raise Conflict(
                    f"{manifest.name!r} version {existing.version} is already installed. "
                    "Reinstall to replace it."
                )
            if target.is_dir():
                shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)

            for name in names:
                if name.endswith("/"):
                    continue
                destination = (target / name).resolve()
                if not destination.is_relative_to(target.resolve()):
                    raise ValidationFailed(f"Plugin entry escapes its directory: {name}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, open(destination, "wb") as handle:
                    shutil.copyfileobj(source, handle)

        logger.info("Installed plugin %s (%s)", manifest.name, manifest.id)
        return manifest

    # -- build -------------------------------------------------------------------------------------

    def build(
        self,
        project: Project,
        destination: Path,
        *,
        name: str,
        workflow_ids: list[str],
        shot_ids: list[str] | None = None,
        version: str = "1.0.0",
        author: str = "",
        description: str = "",
    ) -> Path:
        """Package selected workflows and shots from a project into a ``.cwsplugin``."""
        if not workflow_ids and not shot_ids:
            raise ValidationFailed("Select at least one workflow or shot to package.")

        # A shot template is meaningless without the workflows its steps reference.
        needed = set(workflow_ids)
        for shot in project.shots:
            if shot_ids and shot.id not in shot_ids:
                continue
            if shot_ids:
                needed.update(step.workflow_id for step in shot.steps)

        manifest = PluginManifest(
            id=f"{slugify(name)}-{new_id('pl', 6).split('_')[-1]}",
            name=name,
            version=version,
            author=author,
            description=description,
        )

        destination = Path(destination)
        if destination.suffix != PLUGIN_SUFFIX:
            destination = destination.with_suffix(PLUGIN_SUFFIX)
        destination.parent.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for workflow_id in sorted(needed):
                workflow = project.workflow(workflow_id)
                if workflow is None:
                    continue

                has_ui = False
                for fmt in ("api", "ui"):
                    if not self.project_store.has_workflow(project.id, workflow_id, fmt):
                        continue
                    data = self.project_store.read_workflow(project.id, workflow_id, fmt)
                    if fmt == "ui" and not data.get("nodes"):
                        continue
                    archive.writestr(
                        f"workflows/{workflow_id}.{fmt}.json", json.dumps(data, indent=2)
                    )
                    has_ui = has_ui or fmt == "ui"

                manifest.workflows.append(
                    {
                        "id": workflow_id,
                        "name": workflow.name,
                        "has_ui_graph": has_ui,
                        "ports": [p.model_dump(mode="json") for p in workflow.ports],
                        "params": [p.model_dump(mode="json") for p in workflow.params],
                    }
                )

            for shot in project.shots:
                if shot_ids is not None and shot.id not in shot_ids:
                    continue
                manifest.shot_templates.append(
                    {
                        "name": shot.name,
                        "notes": shot.notes,
                        "steps": [
                            {
                                "key": step.id,
                                "name": step.name,
                                "workflow_id": step.workflow_id,
                                "param_overrides": step.param_overrides,
                                "seed_mode": step.seed_mode,
                                "ui_pos": step.ui_pos.model_dump(),
                            }
                            for step in shot.steps
                        ],
                        "links": [
                            {
                                "from_step": link.from_step,
                                "from_port": link.from_port,
                                "to_step": link.to_step,
                                "to_port": link.to_port,
                            }
                            for link in shot.links
                        ],
                    }
                )

            archive.writestr(MANIFEST_NAME, json.dumps(manifest.to_dict(), indent=2))

        logger.info("Built plugin %s at %s", manifest.name, destination)
        return destination

    # -- apply -------------------------------------------------------------------------------------

    def apply(
        self,
        plugin_id: str,
        project: Project,
        *,
        include_shots: bool = True,
    ) -> dict[str, Any]:
        """Copy a plugin's content into a project.

        Ids are re-issued so the project owns its copy outright — editing it later cannot corrupt the
        plugin, and the same plugin can be applied twice.
        """
        manifest = self.get(plugin_id)
        directory = self._plugin_dir(plugin_id)

        remap: dict[str, str] = {}
        added_workflows: list[str] = []

        for descriptor in manifest.workflows:
            source_id = str(descriptor.get("id"))
            workflow = WorkflowRef(
                name=_unique_name(str(descriptor.get("name", "Workflow")), project),
                ports=[p for p in descriptor.get("ports", [])],  # type: ignore[misc]
                params=[p for p in descriptor.get("params", [])],  # type: ignore[misc]
            )
            remap[source_id] = workflow.id

            wrote_api = False
            for fmt in ("api", "ui"):
                path = directory / "workflows" / f"{source_id}.{fmt}.json"
                if not path.is_file():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                self.project_store.write_workflow(project.id, workflow.id, fmt, data)
                wrote_api = wrote_api or fmt == "api"

            if not wrote_api:
                logger.warning("Plugin %s has no API graph for %s; skipping", plugin_id, source_id)
                remap.pop(source_id, None)
                continue

            project.workflows[workflow.id] = workflow
            added_workflows.append(workflow.id)

        added_shots: list[str] = []
        if include_shots:
            for template in manifest.shot_templates:
                shot = _shot_from_template(template, remap, project)
                if shot is not None:
                    project.shots.append(shot)
                    added_shots.append(shot.id)

        project.touch()
        self.project_store.save(project)

        return {
            "plugin": manifest.name,
            "workflows_added": len(added_workflows),
            "shots_added": len(added_shots),
            "workflow_ids": added_workflows,
            "shot_ids": added_shots,
        }


def _unique_name(name: str, project: Project) -> str:
    """Workflow names must stay distinct or the picker becomes ambiguous."""
    existing = {w.name for w in project.workflows.values()}
    if name not in existing:
        return name
    for index in range(2, 100):
        candidate = f"{name} ({index})"
        if candidate not in existing:
            return candidate
    return f"{name} ({new_id('x', 4)})"


def _shot_from_template(
    template: dict[str, Any], remap: dict[str, str], project: Project
) -> Shot | None:
    steps: list[Step] = []
    step_remap: dict[str, str] = {}

    for entry in template.get("steps", []):
        workflow_id = remap.get(str(entry.get("workflow_id")))
        if workflow_id is None:
            continue  # its workflow could not be imported
        step = Step(
            name=str(entry.get("name", "Step")),
            workflow_id=workflow_id,
            param_overrides=dict(entry.get("param_overrides") or {}),
            seed_mode=entry.get("seed_mode"),
        )
        position = entry.get("ui_pos") or {}
        step.ui_pos.x = float(position.get("x", 0.0))
        step.ui_pos.y = float(position.get("y", 0.0))
        step_remap[str(entry.get("key"))] = step.id
        steps.append(step)

    if not steps:
        return None

    from .models import Link

    links = [
        Link(
            from_step=step_remap[str(entry["from_step"])],
            from_port=str(entry["from_port"]),
            to_step=step_remap[str(entry["to_step"])],
            to_port=str(entry["to_port"]),
        )
        for entry in template.get("links", [])
        if str(entry.get("from_step")) in step_remap and str(entry.get("to_step")) in step_remap
    ]

    return Shot(
        name=_unique_shot_name(str(template.get("name", "Shot")), project),
        notes=str(template.get("notes", "")),
        steps=steps,
        links=links,
    )


def _unique_shot_name(name: str, project: Project) -> str:
    existing = {s.name for s in project.shots}
    if name not in existing:
        return name
    for index in range(2, 100):
        candidate = f"{name} ({index})"
        if candidate not in existing:
            return candidate
    return name
