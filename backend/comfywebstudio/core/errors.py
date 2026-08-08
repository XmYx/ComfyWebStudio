"""Application errors.

Each carries an HTTP status and a stable ``code`` so the frontend can react to a specific failure instead of
pattern-matching on message text.
"""

from __future__ import annotations

from typing import Any


class StudioError(Exception):
    status_code = 400
    code = "error"

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class NotFound(StudioError):
    status_code = 404
    code = "not_found"


class Conflict(StudioError):
    status_code = 409
    code = "conflict"


class ValidationFailed(StudioError):
    status_code = 422
    code = "validation_failed"


class BackendUnavailable(StudioError):
    status_code = 503
    code = "backend_unavailable"


class ExecutionFailed(StudioError):
    status_code = 500
    code = "execution_failed"


class GraphError(ValidationFailed):
    """A shot's step graph is not runnable — a cycle, a dangling link, an incompatible port pair."""

    code = "graph_error"
