"""Describing what changed between two versions of a project.

This is what turns a pile of snapshots into a readable history. Every entry names *what* was touched and
*what happened to it*, so the history panel reads like "Renamed shot to Opening" rather than "project.json
changed".

Each change is attributed to a scope and a target id, which is what makes element-level history possible:
filtering the log by ``target_id`` gives you the history of one step, one link or one clip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Scope = str  # "project" | "shot" | "step" | "link" | "workflow" | "timeline" | "track" | "clip" | "asset"


@dataclass(slots=True)
class Change:
    scope: Scope
    target_id: str
    target_name: str
    action: str
    summary: str
    #: Extra context for the UI, e.g. the parameter that changed.
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "target_id": self.target_id,
            "target_name": self.target_name,
            "action": self.action,
            "summary": self.summary,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Change:
        return cls(
            scope=str(data.get("scope", "project")),
            target_id=str(data.get("target_id", "")),
            target_name=str(data.get("target_name", "")),
            action=str(data.get("action", "changed")),
            summary=str(data.get("summary", "")),
            detail=dict(data.get("detail") or {}),
        )


def diff_projects(old: dict[str, Any] | None, new: dict[str, Any]) -> list[Change]:
    """Every meaningful difference between two serialised projects.

    ``modified`` is ignored — it changes on every save by definition and would drown the real signal.
    """
    if old is None:
        return [
            Change("project", str(new.get("id", "")), str(new.get("name", "Project")), "created",
                   f"Created project “{new.get('name', 'Untitled')}”")
        ]

    changes: list[Change] = []
    _diff_project_fields(old, new, changes)
    _diff_workflows(old, new, changes)
    _diff_shots(old, new, changes)
    _diff_timeline(old, new, changes)
    _diff_assets(old, new, changes)
    return changes


# -- project ---------------------------------------------------------------------------------------------


def _diff_project_fields(old: dict, new: dict, changes: list[Change]) -> None:
    project_id = str(new.get("id", ""))
    name = str(new.get("name", "Project"))

    if old.get("name") != new.get("name"):
        changes.append(
            Change("project", project_id, name, "renamed",
                   f"Renamed project to “{new.get('name')}”",
                   {"from": old.get("name"), "to": new.get("name")})
        )
    if old.get("description") != new.get("description"):
        changes.append(
            Change("project", project_id, name, "edited", "Changed the project description")
        )

    old_settings = old.get("settings") or {}
    new_settings = new.get("settings") or {}
    for key in ("fps", "width", "height", "backend_id"):
        if old_settings.get(key) != new_settings.get(key):
            changes.append(
                Change("project", project_id, name, "settings",
                       f"Set project {key} to {new_settings.get(key)}",
                       {"field": key, "from": old_settings.get(key), "to": new_settings.get(key)})
            )


# -- workflows -------------------------------------------------------------------------------------------


def _diff_workflows(old: dict, new: dict, changes: list[Change]) -> None:
    old_workflows = old.get("workflows") or {}
    new_workflows = new.get("workflows") or {}

    for workflow_id in new_workflows.keys() - old_workflows.keys():
        workflow = new_workflows[workflow_id]
        changes.append(
            Change("workflow", workflow_id, str(workflow.get("name", "Workflow")), "added",
                   f"Added workflow “{workflow.get('name')}”")
        )

    for workflow_id in old_workflows.keys() - new_workflows.keys():
        workflow = old_workflows[workflow_id]
        changes.append(
            Change("workflow", workflow_id, str(workflow.get("name", "Workflow")), "removed",
                   f"Removed workflow “{workflow.get('name')}”")
        )

    for workflow_id in old_workflows.keys() & new_workflows.keys():
        before, after = old_workflows[workflow_id], new_workflows[workflow_id]
        name = str(after.get("name", "Workflow"))

        if before.get("name") != after.get("name"):
            changes.append(
                Change("workflow", workflow_id, name, "renamed",
                       f"Renamed workflow to “{name}”")
            )

        if before.get("hash") != after.get("hash"):
            old_ports = {p["key"] for p in before.get("ports") or []}
            new_ports = {p["key"] for p in after.get("ports") or []}
            added, removed = new_ports - old_ports, old_ports - new_ports
            bits = []
            if added:
                bits.append(f"+{', '.join(sorted(added))}")
            if removed:
                bits.append(f"-{', '.join(sorted(removed))}")
            changes.append(
                Change("workflow", workflow_id, name, "synced",
                       f"Updated workflow “{name}” from ComfyUI" + (f" ({'; '.join(bits)})" if bits else ""),
                       {"ports_added": sorted(added), "ports_removed": sorted(removed)})
            )
        else:
            old_params = {p["key"] for p in before.get("params") or []}
            new_params = {p["key"] for p in after.get("params") or []}
            for key in new_params - old_params:
                changes.append(
                    Change("workflow", workflow_id, name, "exposed",
                           f"Exposed parameter {key} on “{name}”", {"param": key})
                )
            for key in old_params - new_params:
                changes.append(
                    Change("workflow", workflow_id, name, "unexposed",
                           f"Stopped exposing {key} on “{name}”", {"param": key})
                )


# -- shots, steps, links ---------------------------------------------------------------------------------


def _diff_shots(old: dict, new: dict, changes: list[Change]) -> None:
    old_shots = {s["id"]: s for s in old.get("shots") or []}
    new_shots = {s["id"]: s for s in new.get("shots") or []}

    for shot_id in new_shots.keys() - old_shots.keys():
        shot = new_shots[shot_id]
        changes.append(
            Change("shot", shot_id, str(shot.get("name", "Shot")), "added",
                   f"Added shot “{shot.get('name')}”")
        )

    for shot_id in old_shots.keys() - new_shots.keys():
        shot = old_shots[shot_id]
        changes.append(
            Change("shot", shot_id, str(shot.get("name", "Shot")), "removed",
                   f"Deleted shot “{shot.get('name')}”")
        )

    for shot_id in old_shots.keys() & new_shots.keys():
        _diff_one_shot(old_shots[shot_id], new_shots[shot_id], changes)


def _diff_one_shot(before: dict, after: dict, changes: list[Change]) -> None:
    shot_id = str(after["id"])
    shot_name = str(after.get("name", "Shot"))

    if before.get("name") != after.get("name"):
        changes.append(
            Change("shot", shot_id, shot_name, "renamed", f"Renamed shot to “{shot_name}”",
                   {"from": before.get("name"), "to": after.get("name")})
        )
    if before.get("notes") != after.get("notes"):
        changes.append(Change("shot", shot_id, shot_name, "edited", f"Edited notes on “{shot_name}”"))

    _diff_steps(before, after, shot_id, shot_name, changes)
    _diff_links(before, after, shot_id, shot_name, changes)


def _diff_steps(before: dict, after: dict, shot_id: str, shot_name: str, changes: list[Change]) -> None:
    old_steps = {s["id"]: s for s in before.get("steps") or []}
    new_steps = {s["id"]: s for s in after.get("steps") or []}

    for step_id in new_steps.keys() - old_steps.keys():
        step = new_steps[step_id]
        changes.append(
            Change("step", step_id, str(step.get("name", "Step")), "added",
                   f"Added step “{step.get('name')}” to {shot_name}", {"shot_id": shot_id})
        )

    for step_id in old_steps.keys() - new_steps.keys():
        step = old_steps[step_id]
        changes.append(
            Change("step", step_id, str(step.get("name", "Step")), "removed",
                   f"Deleted step “{step.get('name')}” from {shot_name}", {"shot_id": shot_id})
        )

    for step_id in old_steps.keys() & new_steps.keys():
        old_step, new_step = old_steps[step_id], new_steps[step_id]
        name = str(new_step.get("name", "Step"))
        base = {"shot_id": shot_id}

        if old_step.get("name") != new_step.get("name"):
            changes.append(
                Change("step", step_id, name, "renamed", f"Renamed step to “{name}”",
                       {**base, "from": old_step.get("name")})
            )
        if old_step.get("enabled") != new_step.get("enabled"):
            state = "Enabled" if new_step.get("enabled") else "Disabled"
            changes.append(Change("step", step_id, name, "toggled", f"{state} step “{name}”", base))
        if old_step.get("seed_mode") != new_step.get("seed_mode"):
            changes.append(
                Change("step", step_id, name, "edited",
                       f"Set seed mode on “{name}” to {new_step.get('seed_mode') or 'project default'}", base)
            )
        if old_step.get("backend_id") != new_step.get("backend_id"):
            changes.append(
                Change("step", step_id, name, "edited",
                       f"Changed which backend “{name}” runs on", base)
            )

        # Parameters are the thing users tweak most, so each one gets its own entry.
        old_params = old_step.get("param_overrides") or {}
        new_params = new_step.get("param_overrides") or {}
        for key in new_params.keys() | old_params.keys():
            if old_params.get(key) == new_params.get(key):
                continue
            if key not in new_params:
                changes.append(
                    Change("step", step_id, name, "param",
                           f"Reset {key} on “{name}” to the workflow default",
                           {**base, "param": key, "from": old_params.get(key)})
                )
            else:
                changes.append(
                    Change("step", step_id, name, "param",
                           f"Set {key} on “{name}” to {_short(new_params[key])}",
                           {**base, "param": key,
                            "from": old_params.get(key), "to": new_params.get(key)})
                )

        # Layout changes are recorded but marked, so the UI can hide them from the main history.
        if old_step.get("ui_pos") != new_step.get("ui_pos"):
            changes.append(
                Change("step", step_id, name, "moved", f"Moved “{name}”", {**base, "layout": True})
            )
        if old_step.get("ui_size") != new_step.get("ui_size"):
            size = new_step.get("ui_size") or {}
            changes.append(
                Change("step", step_id, name, "resized",
                       f"Resized “{name}” to {int(size.get('w', 0))}×{int(size.get('h', 0))}",
                       {**base, "layout": True})
            )


def _diff_links(before: dict, after: dict, shot_id: str, shot_name: str, changes: list[Change]) -> None:
    def key_of(link: dict) -> tuple[str, str, str, str]:
        return (
            str(link.get("from_step")), str(link.get("from_port")),
            str(link.get("to_step")), str(link.get("to_port")),
        )

    old_links = {key_of(link): link for link in before.get("links") or []}
    new_links = {key_of(link): link for link in after.get("links") or []}
    step_names = {s["id"]: s.get("name", "step") for s in (after.get("steps") or []) + (before.get("steps") or [])}

    for key in new_links.keys() - old_links.keys():
        link = new_links[key]
        changes.append(
            Change("link", str(link.get("id", "")), f"{key[1]} → {key[3]}", "connected",
                   f"Connected {step_names.get(key[0], '?')}.{key[1]} → "
                   f"{step_names.get(key[2], '?')}.{key[3]} in {shot_name}",
                   {"shot_id": shot_id, "from_step": key[0], "to_step": key[2]})
        )

    for key in old_links.keys() - new_links.keys():
        link = old_links[key]
        changes.append(
            Change("link", str(link.get("id", "")), f"{key[1]} → {key[3]}", "disconnected",
                   f"Disconnected {step_names.get(key[0], '?')}.{key[1]} → "
                   f"{step_names.get(key[2], '?')}.{key[3]} in {shot_name}",
                   {"shot_id": shot_id, "from_step": key[0], "to_step": key[2]})
        )


# -- timeline --------------------------------------------------------------------------------------------


def _diff_timeline(old: dict, new: dict, changes: list[Change]) -> None:
    old_timeline = old.get("timeline") or {}
    new_timeline = new.get("timeline") or {}

    for key in ("fps", "width", "height", "background"):
        if old_timeline.get(key) != new_timeline.get(key):
            changes.append(
                Change("timeline", "timeline", "Timeline", "settings",
                       f"Set timeline {key} to {new_timeline.get(key)}",
                       {"field": key, "from": old_timeline.get(key), "to": new_timeline.get(key)})
            )

    old_tracks = {t["id"]: t for t in old_timeline.get("tracks") or []}
    new_tracks = {t["id"]: t for t in new_timeline.get("tracks") or []}

    for track_id in new_tracks.keys() - old_tracks.keys():
        track = new_tracks[track_id]
        changes.append(
            Change("track", track_id, str(track.get("name", "Track")), "added",
                   f"Added {track.get('kind')} track “{track.get('name')}”")
        )
    for track_id in old_tracks.keys() - new_tracks.keys():
        track = old_tracks[track_id]
        changes.append(
            Change("track", track_id, str(track.get("name", "Track")), "removed",
                   f"Deleted track “{track.get('name')}”")
        )

    for track_id in old_tracks.keys() & new_tracks.keys():
        _diff_track(old_tracks[track_id], new_tracks[track_id], changes)


def _diff_track(before: dict, after: dict, changes: list[Change]) -> None:
    track_id = str(after["id"])
    track_name = str(after.get("name", "Track"))

    if before.get("name") != after.get("name"):
        changes.append(
            Change("track", track_id, track_name, "renamed", f"Renamed track to “{track_name}”")
        )
    for flag, verb in (("muted", "Muted"), ("locked", "Locked")):
        if before.get(flag) != after.get(flag):
            action = verb if after.get(flag) else f"Un{verb.lower()}"
            changes.append(
                Change("track", track_id, track_name, "toggled", f"{action} track “{track_name}”")
            )

    old_clips = {c["id"]: c for c in before.get("clips") or []}
    new_clips = {c["id"]: c for c in after.get("clips") or []}

    for clip_id in new_clips.keys() - old_clips.keys():
        clip = new_clips[clip_id]
        changes.append(
            Change("clip", clip_id, _clip_name(clip), "added",
                   f"Added clip “{_clip_name(clip)}” to {track_name}", {"track_id": track_id})
        )
    for clip_id in old_clips.keys() - new_clips.keys():
        clip = old_clips[clip_id]
        changes.append(
            Change("clip", clip_id, _clip_name(clip), "removed",
                   f"Removed clip “{_clip_name(clip)}” from {track_name}", {"track_id": track_id})
        )

    for clip_id in old_clips.keys() & new_clips.keys():
        old_clip, new_clip = old_clips[clip_id], new_clips[clip_id]
        name = _clip_name(new_clip)
        base = {"track_id": track_id}

        if old_clip.get("start") != new_clip.get("start"):
            changes.append(
                Change("clip", clip_id, name, "moved",
                       f"Moved “{name}” to {float(new_clip.get('start', 0)):.2f}s", base)
            )
        if old_clip.get("duration") != new_clip.get("duration"):
            changes.append(
                Change("clip", clip_id, name, "trimmed",
                       f"Set “{name}” duration to {float(new_clip.get('duration', 0)):.2f}s", base)
            )
        for field_name, label in (
            ("opacity", "opacity"), ("volume", "volume"), ("text", "text"), ("enabled", "enabled"),
        ):
            if old_clip.get(field_name) != new_clip.get(field_name):
                changes.append(
                    Change("clip", clip_id, name, "edited",
                           f"Changed {label} on “{name}”", {**base, "field": field_name})
                )
        if old_clip.get("transform") != new_clip.get("transform"):
            changes.append(Change("clip", clip_id, name, "edited", f"Adjusted transform on “{name}”", base))


def _clip_name(clip: dict) -> str:
    return str(clip.get("name") or clip.get("text") or (clip.get("source") or {}).get("port_key") or "clip")


# -- assets ----------------------------------------------------------------------------------------------


def _diff_assets(old: dict, new: dict, changes: list[Change]) -> None:
    old_assets = old.get("assets") or {}
    new_assets = new.get("assets") or {}

    for asset_id in new_assets.keys() - old_assets.keys():
        asset = new_assets[asset_id]
        changes.append(
            Change("asset", asset_id, str(asset.get("name", "Asset")), "added",
                   f"Imported {asset.get('kind')} “{asset.get('name')}”")
        )
    for asset_id in old_assets.keys() - new_assets.keys():
        asset = old_assets[asset_id]
        changes.append(
            Change("asset", asset_id, str(asset.get("name", "Asset")), "removed",
                   f"Removed asset “{asset.get('name')}”")
        )


def _short(value: Any, limit: int = 40) -> str:
    """Values go into a one-line summary, so long prompts must not blow it out."""
    text = str(value)
    if len(text) > limit:
        return f"“{text[:limit].rstrip()}…”"
    return f"“{text}”" if isinstance(value, str) else text


def summarize(changes: list[Change]) -> str:
    """One line describing a whole version, for the history list."""
    if not changes:
        return "No changes"
    if len(changes) == 1:
        return changes[0].summary
    # Layout-only churn is not interesting enough to headline a version.
    substantive = [c for c in changes if not c.detail.get("layout")]
    lead = (substantive or changes)[0]
    remaining = len(changes) - 1
    return f"{lead.summary} (+{remaining} more)" if remaining else lead.summary
