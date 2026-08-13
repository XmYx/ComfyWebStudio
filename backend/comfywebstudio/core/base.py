"""The two things every persisted model needs.

Split out of `models.py` so that other model modules — storyboards, and whatever comes next — can build on
them without importing the module that imports *them*, which is a circular import waiting to happen.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(BaseModel):
    #: Unknown keys are dropped rather than rejected, so a project written by a newer version still
    #: loads in an older one — with the parts it does not understand missing, but loading.
    model_config = ConfigDict(extra="ignore", validate_assignment=False)
