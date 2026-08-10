"""Application settings.

Every path derives from a single root so a user can relocate all state with one env var, and every field is
overridable three ways, in increasing precedence: built-in default -> ``settings.json`` -> ``CWS_*`` env var.

The persisted file is the one the Settings page edits; env vars stay authoritative so a deployment can pin
values the UI cannot override.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

APP_NAME = "ComfyWebStudio"
ENV_PREFIX = "CWS_"

BackendKind = Literal["local", "remote"]


def default_root() -> Path:
    return Path(os.environ.get(f"{ENV_PREFIX}ROOT", Path.home() / ".comfywebstudio")).expanduser()


class ComfyBackendConfig(BaseModel):
    """One configured ComfyUI instance."""

    id: str
    name: str = "ComfyUI"
    kind: BackendKind = "local"
    base_url: str = "http://127.0.0.1:8188"
    #: Extra headers sent on every request (auth tokens for hosted instances).
    headers: dict[str, str] = Field(default_factory=dict)
    #: Filesystem root of the ComfyUI install. Only meaningful for ``kind == "local"``; when set we can link
    #: files between output/ and input/ instead of pushing them over HTTP.
    comfy_root: str | None = None
    #: Which ComfyUI user directory workflows live under (``/userdata`` is scoped per user).
    comfy_user: str = "default"
    enabled: bool = True
    timeout_s: float = 60.0

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def uses_shared_filesystem(self) -> bool:
        return self.kind == "local" and bool(self.comfy_root) and Path(self.comfy_root).is_dir()

    def resolve_dir(self, which: Literal["input", "output", "temp", "user"]) -> Path | None:
        """Absolute path to one of ComfyUI's directories, or None when we have no filesystem access."""
        if not self.uses_shared_filesystem:
            return None
        return Path(self.comfy_root) / which  # type: ignore[arg-type]


class ExecutionSettings(BaseModel):
    max_concurrent_steps: int = Field(default=1, ge=1, le=16)
    #: Reuse a previous step result when workflow + params + upstream artifacts are unchanged.
    enable_cache: bool = True
    #: Seconds to wait for a submitted prompt before giving up. None disables the timeout.
    step_timeout_s: float | None = 1800.0
    retry_on_error: int = Field(default=0, ge=0, le=5)
    #: Randomise seed inputs on every run unless a step pins them.
    default_seed_mode: Literal["fixed", "randomize", "increment"] = "fixed"


class RenderSettings(BaseModel):
    fps: float = 24.0
    width: int = 1024
    height: int = 1024
    container: str = "mp4"
    video_codec: str = "libx264"
    crf: int = Field(default=18, ge=0, le=51)
    pix_fmt: str = "yuv420p"
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    audio_sample_rate: int = 48000


class PreviewSettings(BaseModel):
    thumbnail_size: int = Field(default=320, ge=64, le=2048)
    thumbnail_format: Literal["webp", "jpeg", "png"] = "webp"
    thumbnail_quality: int = Field(default=82, ge=1, le=100)
    autoplay_video: bool = False


class UISettings(BaseModel):
    theme: Literal["dark", "light", "system"] = "dark"
    timeline_snap: bool = True
    timeline_snap_frames: int = 1


class AppSettings(BaseModel):
    """Root settings object. Persisted to ``<root>/settings.json``."""

    schema_version: int = 1

    root: Path = Field(default_factory=default_root)
    projects_dir: Path | None = None
    cache_dir: Path | None = None
    temp_dir: Path | None = None

    host: str = "127.0.0.1"
    port: int = 8500
    #: Browser origins allowed to call the API. The ComfyUI origin must be here for the bridge extension
    #: to be able to POST saved workflows back to us.
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173", "http://127.0.0.1:8188"]
    )

    backends: list[ComfyBackendConfig] = Field(default_factory=list)
    default_backend_id: str | None = None

    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)
    preview: PreviewSettings = Field(default_factory=PreviewSettings)
    ui: UISettings = Field(default_factory=UISettings)

    # -- derived paths ---------------------------------------------------------------------------------

    def model_post_init(self, _ctx: Any) -> None:
        self.root = Path(self.root).expanduser()
        self.projects_dir = Path(self.projects_dir or self.root / "projects").expanduser()
        self.cache_dir = Path(self.cache_dir or self.root / "cache").expanduser()
        self.temp_dir = Path(self.temp_dir or self.root / "temp").expanduser()

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    def ensure_dirs(self) -> None:
        for p in (self.root, self.projects_dir, self.cache_dir, self.temp_dir):
            Path(p).mkdir(parents=True, exist_ok=True)

    def backend(self, backend_id: str | None = None) -> ComfyBackendConfig | None:
        wanted = backend_id or self.default_backend_id
        for b in self.backends:
            if b.id == wanted:
                return b
        return next((b for b in self.backends if b.enabled), None)


# -- load / save ---------------------------------------------------------------------------------------

#: Env overrides, mapped to a dotted path into the settings model.
_ENV_MAP: dict[str, str] = {
    "ROOT": "root",
    "PROJECTS_DIR": "projects_dir",
    "CACHE_DIR": "cache_dir",
    "TEMP_DIR": "temp_dir",
    "HOST": "host",
    "PORT": "port",
    "CORS_ORIGINS": "cors_origins",
    "MAX_CONCURRENT_STEPS": "execution.max_concurrent_steps",
    "ENABLE_CACHE": "execution.enable_cache",
}


def _coerce(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if "," in raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _assign(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cursor = data
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def _apply_env(data: dict[str, Any]) -> dict[str, Any]:
    for suffix, dotted in _ENV_MAP.items():
        raw = os.environ.get(ENV_PREFIX + suffix)
        if raw is not None:
            _assign(data, dotted, _coerce(raw))
    return data


def load_settings(path: Path | None = None) -> AppSettings:
    """Read settings from disk, layer env overrides on top, and make sure the directories exist."""
    root = Path(os.environ.get(f"{ENV_PREFIX}ROOT", "")).expanduser() if os.environ.get(f"{ENV_PREFIX}ROOT") else None
    file = path or (root or default_root()) / "settings.json"

    data: dict[str, Any] = {}
    if file.is_file():
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:  # a corrupt file must not brick startup
            data = {}
            _CORRUPT_NOTICE.append(f"{file}: {exc}")

    settings = AppSettings.model_validate(_apply_env(data))
    settings.ensure_dirs()
    return settings


def save_settings(settings: AppSettings, path: Path | None = None) -> Path:
    """Persist settings atomically so a crash mid-write cannot leave an unreadable file."""
    settings.ensure_dirs()
    target = path or settings.settings_file
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    payload = settings.model_dump(mode="json", exclude_none=False)
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    os.replace(tmp, target)
    return target


#: Populated when a settings file could not be parsed; surfaced by the API so the UI can warn.
_CORRUPT_NOTICE: list[str] = []


def corrupt_settings_notices() -> list[str]:
    return list(_CORRUPT_NOTICE)
