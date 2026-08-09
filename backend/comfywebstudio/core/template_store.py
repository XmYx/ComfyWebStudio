"""The shared template library.

Templates live under the studio root rather than inside a project, because a template that can only be
used by the project that produced it is not much of a template. The layout mirrors the plugin store:

    <root>/templates/<template_id>/
        template.json                 the structure, promotions and workflow descriptors
        workflows/<key>.api.json      the runnable prompt for each workflow it needs
        workflows/<key>.ui.json       the LiteGraph document, when one exists

The workflow graphs are copied in rather than referenced. A template has to keep working when the project
that built it is deleted, and a stored prompt is the only thing that guarantees that.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .errors import NotFound, ValidationFailed
from .ids import safe_component
from .templates import ShotTemplate, TemplateSummary

logger = logging.getLogger(__name__)

MANIFEST_NAME = "template.json"


class TemplateStore:
    """Reads and writes the shared template library."""

    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def directory(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def template_dir(self, template_id: str) -> Path:
        return self.directory / safe_component(template_id, "template")

    # -- reading -----------------------------------------------------------------------------------

    def list(self) -> list[TemplateSummary]:
        """Every template in the library, newest first.

        An unreadable template is skipped with a warning rather than breaking the list — one corrupt file
        should not make the whole library unusable.
        """
        summaries: list[TemplateSummary] = []
        for entry in sorted(self.directory.iterdir()):
            if not (entry / MANIFEST_NAME).is_file():
                continue
            try:
                template = self._read(entry / MANIFEST_NAME)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                logger.warning("Skipping unreadable template at %s: %s", entry, exc)
                continue
            summaries.append(summarize(template))
        summaries.sort(key=lambda s: s.modified, reverse=True)
        return summaries

    def get(self, template_id: str) -> ShotTemplate:
        path = self.template_dir(template_id) / MANIFEST_NAME
        if not path.is_file():
            raise NotFound(f"No template {template_id!r} in the library")
        return self._read(path)

    def exists(self, template_id: str) -> bool:
        return (self.template_dir(template_id) / MANIFEST_NAME).is_file()

    def read_workflow(self, template_id: str, workflow_key: str, fmt: str) -> dict[str, Any]:
        path = self._workflow_path(template_id, workflow_key, fmt)
        if not path.is_file():
            raise NotFound(f"Template {template_id!r} has no {fmt}-format graph for {workflow_key!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    def has_workflow(self, template_id: str, workflow_key: str, fmt: str) -> bool:
        return self._workflow_path(template_id, workflow_key, fmt).is_file()

    # -- writing -----------------------------------------------------------------------------------

    def save(self, template: ShotTemplate, graphs: dict[str, dict[str, Any]]) -> ShotTemplate:
        """Write a template and the workflow graphs it carries.

        ``graphs`` maps ``"<workflow key>.<fmt>"`` to the document, which is exactly what the capture step
        collects. Written to a temporary directory and swapped in, so a crash mid-write cannot leave a
        template that lists workflows whose graphs are missing.
        """
        if not template.steps:
            raise ValidationFailed("A template needs at least one step.")

        target = self.template_dir(template.id)
        staging = target.with_name(target.name + ".tmp")
        shutil.rmtree(staging, ignore_errors=True)
        (staging / "workflows").mkdir(parents=True, exist_ok=True)

        try:
            for name, document in graphs.items():
                key, _, fmt = name.rpartition(".")
                if fmt not in {"api", "ui"} or not key:
                    raise ValidationFailed(f"Unexpected graph name {name!r} in a template.")
                (staging / "workflows" / f"{safe_component(key, 'wf')}.{fmt}.json").write_text(
                    json.dumps(document, indent=2), encoding="utf-8"
                )
            (staging / MANIFEST_NAME).write_text(
                json.dumps(template.model_dump(mode="json"), indent=2), encoding="utf-8"
            )
            shutil.rmtree(target, ignore_errors=True)
            staging.rename(target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        logger.info("Saved template %r (%s) at revision %d", template.name, template.id, template.revision)
        return template

    def write(self, template: ShotTemplate) -> ShotTemplate:
        """Persist a change to a template that does not touch the workflows it carries."""
        path = self.template_dir(template.id) / MANIFEST_NAME
        if not path.is_file():
            raise NotFound(f"No template {template.id!r} in the library")
        path.write_text(json.dumps(template.model_dump(mode="json"), indent=2), encoding="utf-8")
        return template

    def update_metadata(self, template_id: str, *, name: str | None = None,
                        description: str | None = None) -> ShotTemplate:
        """Rename or re-describe a template without touching its structure.

        Deliberately does not bump the revision: nothing a placed instance runs has changed, so making
        every instance look out of date over a typo fix would be noise.
        """
        template = self.get(template_id)
        if name is not None:
            template.name = name
        if description is not None:
            template.description = description
        return self.write(template)

    def delete(self, template_id: str) -> None:
        directory = self.template_dir(template_id)
        if not directory.is_dir():
            raise NotFound(f"No template {template_id!r} in the library")
        shutil.rmtree(directory)
        logger.info("Deleted template %s", template_id)

    # -- internals ---------------------------------------------------------------------------------

    def _workflow_path(self, template_id: str, workflow_key: str, fmt: str) -> Path:
        return (
            self.template_dir(template_id) / "workflows" / f"{safe_component(workflow_key, 'wf')}.{fmt}.json"
        )

    def _read(self, path: Path) -> ShotTemplate:
        return ShotTemplate.model_validate(json.loads(path.read_text(encoding="utf-8")))


def summarize(template: ShotTemplate) -> TemplateSummary:
    return TemplateSummary(
        id=template.id,
        name=template.name,
        description=template.description,
        revision=template.revision,
        modified=template.modified,
        source_project=template.source_project,
        step_count=len(template.steps),
        input_count=sum(1 for p in template.shown_ports if p.direction == "in"),
        output_count=sum(1 for p in template.shown_ports if p.direction == "out"),
        control_count=len(template.shown_controls),
    )
