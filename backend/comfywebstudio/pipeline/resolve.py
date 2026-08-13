"""Layering a board's edits over the app's, and the app's over the built-ins.

Three layers, each one sparse. A board that rewrote its describe prompt stores *that stage* and nothing
else, so it keeps picking up improvements to the other four — and resetting it is deleting one entry
rather than reasoning about what a copy has drifted from.

The order of the layers is the obvious one: **built-in → app → board**, most specific wins.
"""

from __future__ import annotations

from ..core.pipeline import Pipeline, PipelineOverlay, Stage
from .builtin import BUILTIN_REVISION, builtin_pipeline


def apply_overlay(base: Pipeline, overlay: PipelineOverlay | None) -> Pipeline:
    """`base` with one layer of edits on top."""
    if overlay is None or overlay.is_empty():
        return base

    stages = {stage.id: stage for stage in base.stages}
    order = [stage.id for stage in base.stages]

    for stage_id, replacement in overlay.stages.items():
        if stage_id not in stages:
            order.append(stage_id)                 # a stage this layer added
        stages[stage_id] = replacement

    for stage_id in overlay.removed:
        stages.pop(stage_id, None)
        if stage_id in order:
            order.remove(stage_id)

    if overlay.order:
        # The stored order decides, but it is not trusted to be complete: a stage added by a newer build
        # would otherwise disappear from a board saved by an older one. Anything unmentioned keeps its
        # place relative to what came before it.
        named = [sid for sid in overlay.order if sid in stages]
        order = named + [sid for sid in order if sid not in named]

    return Pipeline(
        id=base.id,
        name=base.name,
        revision=max(base.revision, overlay.revision),
        stages=[stages[sid] for sid in order],
    )


def resolve(settings, board=None) -> Pipeline:
    """The pipeline that actually runs for this board.

    `settings` is an :class:`AppSettings`; `board` a :class:`Storyboard` or None for the app-level view.
    """
    pipeline = apply_overlay(builtin_pipeline(), getattr(settings.story, "pipeline", None))
    if board is not None:
        pipeline = apply_overlay(pipeline, board.pipeline)
    return pipeline


def stage_is_stale(stage: Stage) -> bool:
    """True when this stored stage was edited against a built-in that has since moved on."""
    return bool(stage.builtin_id) and stage.builtin_revision < BUILTIN_REVISION


def overlay_with(overlay: PipelineOverlay | None, stage: Stage) -> PipelineOverlay:
    """`overlay` with `stage` replacing whatever was there — the shape a save takes."""
    updated = (overlay or PipelineOverlay()).model_copy(deep=True)
    updated.stages[stage.id] = stage
    if stage.id in updated.removed:
        updated.removed.remove(stage.id)
    updated.touch()
    return updated


def overlay_without(overlay: PipelineOverlay | None, stage_id: str) -> PipelineOverlay | None:
    """`overlay` with any edit to `stage_id` dropped — "reset this stage to the default".

    Returns None once nothing is left, so a board that has been reset all the way back stores no overlay
    at all and goes back to tracking the layer below it.
    """
    if overlay is None:
        return None
    updated = overlay.model_copy(deep=True)
    updated.stages.pop(stage_id, None)
    updated.order = [sid for sid in updated.order if sid != stage_id]
    updated.touch()
    return None if updated.is_empty() else updated
