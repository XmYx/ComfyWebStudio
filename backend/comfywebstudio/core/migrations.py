"""Forward migrations for ``project.json``.

Every migration takes the raw dict at version *n* and returns it at *n+1*. Keeping them as dict-to-dict
transforms means an old project loads even when the pydantic models have moved on considerably.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .errors import ValidationFailed
from .models import PROJECT_SCHEMA_VERSION

logger = logging.getLogger(__name__)

Migration = Callable[[dict[str, Any]], dict[str, Any]]

#: ``from_version -> migration``. Add an entry here and bump PROJECT_SCHEMA_VERSION together.
MIGRATIONS: dict[int, Migration] = {}


def register(from_version: int) -> Callable[[Migration], Migration]:
    def decorator(fn: Migration) -> Migration:
        MIGRATIONS[from_version] = fn
        return fn

    return decorator


def migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Bring a raw project dict up to the current schema version."""
    version = int(data.get("schema_version", 1))

    if version > PROJECT_SCHEMA_VERSION:
        raise ValidationFailed(
            f"This project was saved by a newer version of ComfyWebStudio "
            f"(schema {version}, this build understands {PROJECT_SCHEMA_VERSION}). Please update.",
            details={"schema_version": version, "supported": PROJECT_SCHEMA_VERSION},
        )

    while version < PROJECT_SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise ValidationFailed(
                f"No migration from project schema {version} to {version + 1}.",
                details={"schema_version": version},
            )
        logger.info("Migrating project from schema %d to %d", version, version + 1)
        data = migration(data)
        version += 1
        data["schema_version"] = version

    return data
