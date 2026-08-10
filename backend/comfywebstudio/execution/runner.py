"""Running one step.

The sequence, and why each part is where it is:

1. Resolve parameters and upstream artifacts, and compute a cache key from them.
2. On a cache hit, return the previous result without touching ComfyUI.
3. Stage every linked input so this backend can read it.
4. Inject values and the run key into a copy of the workflow's API prompt.
5. **Subscribe to the websocket first**, then POST — ComfyUI can begin executing before the HTTP response
   returns, and an event emitted before the subscription exists is lost.
6. Wait for the ``executing: node=null`` sentinel, which ComfyUI sends *after* history is written.
   ``execution_success`` fires earlier and reading history on it races.
7. Read ``/history``, ingest each artifact into the project, and record the result.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any

from ..comfy.backend import ComfyBackend, ComfyFileRef
from ..comfy.discovery import prompt_hash
from ..comfy.http import ComfyError, ComfyPromptRejected
from ..comfy.inject import prepare_prompt, resolve_param_values
from ..core.ids import new_uuid
from ..core.models import (
    Artifact,
    Asset,
    PortKind,
    Project,
    Shot,
    Step,
    StepRun,
    WorkflowRef,
    utcnow,
)
from ..core.store import ProjectStore
from ..media.transfer import MediaTransfer
from ..settings import AppSettings
from .cache import CacheIndex, compute_cache_key
from .events import EventBus

logger = logging.getLogger(__name__)

#: Key our node pack writes into a node's ``ui`` payload.
UI_KEY = "webstudio"

#: Port kinds carrying a value the user types, rather than a file that has to be staged.
SCALAR_KINDS: frozenset[str] = frozenset({"string", "int", "float", "boolean"})


class StepFailed(RuntimeError):
    def __init__(self, message: str, *, node_id: str | None = None):
        super().__init__(message)
        self.node_id = node_id


@dataclass(slots=True)
class PinnedInput:
    """A value a canvas value node feeds into one of this step's input ports.

    Distinct from an upstream artifact because nothing produced it — there is no run behind it, so there is
    no SHA to reuse and nothing to wait for. It still has to reach the cache key, or editing the node would
    leave every step it feeds serving a stale result.
    """

    kind: PortKind
    #: Set for a literal (string / int / float / boolean).
    scalar: Any = None
    #: Set for a media node. ``None`` on a media node whose asset has not been chosen.
    asset: Asset | None = None
    #: Set for a dropped shot node — the artifact that shot last produced on the chosen port.
    artifact: Artifact | None = None
    #: What to call it when something is wrong with it.
    label: str = "value node"

    @property
    def media_path(self) -> str | None:
        """The project-relative file this pin supplies, whichever kind of source it is."""
        if self.artifact is not None:
            return self.artifact.path
        return self.asset.path if self.asset is not None else None

    @property
    def fingerprint(self) -> str:
        if self.artifact is not None:
            return self.artifact.sha256 or self.artifact.path
        if self.asset is not None:
            return self.asset.sha256 or self.asset.path
        return f"{self.kind}:{self.scalar!r}"


@dataclass(slots=True)
class RunContext:
    """Everything a step run needs, assembled once per run."""

    project: Project
    store: ProjectStore
    media: MediaTransfer
    settings: AppSettings
    events: EventBus
    cache: CacheIndex
    run_id: str
    #: Resolves a step to the backend it should execute on.
    backend_for: Any
    rng: random.Random


class StepRunner:
    def __init__(self, ctx: RunContext):
        self.ctx = ctx

    async def run(
        self,
        shot: Shot,
        step: Step,
        *,
        upstream: dict[str, Artifact],
        pinned: dict[str, PinnedInput] | None = None,
        force: bool = False,
    ) -> StepRun:
        """Execute one step.

        ``upstream`` maps this step's input port key to the artifact feeding it; ``pinned`` maps it to a
        value supplied directly by a value node on the canvas.
        """
        pinned = pinned or {}
        ctx = self.ctx
        project = ctx.project
        step_run = StepRun(step_id=step.id, status="pending")

        workflow = project.workflow(step.workflow_id)
        if workflow is None:
            step_run.status = "error"
            step_run.error = f"Step {step.name!r} references a workflow that is not in this project."
            return step_run

        try:
            api_prompt = ctx.store.read_workflow(project.id, workflow.id, "api")
        except Exception as exc:  # noqa: BLE001
            step_run.status = "error"
            step_run.error = (
                f"No executable graph stored for workflow {workflow.name!r}. "
                f"Open it in ComfyUI and save it back to ComfyWebStudio. ({exc})"
            )
            return step_run

        seed_mode = step.seed_mode or ctx.settings.execution.default_seed_mode
        resolved = resolve_param_values(workflow, step.param_overrides)
        step_run.resolved_params = resolved

        # -- cache -------------------------------------------------------------------------------
        # A randomised seed makes the result different by definition, so caching it would be wrong.
        cacheable = ctx.settings.execution.enable_cache and not force and not (
            seed_mode == "randomize" and any(p.is_seed for p in workflow.params)
        )
        cache_key = compute_cache_key(
            workflow_hash=workflow.hash or prompt_hash(api_prompt),
            resolved_params=resolved,
            upstream={
                **{key: artifact.sha256 for key, artifact in upstream.items()},
                **{key: pin.fingerprint for key, pin in pinned.items()},
            },
            output_ports=[p.key for p in workflow.outputs],
        )
        step_run.cache_key = cache_key

        if cacheable:
            hit = ctx.cache.lookup(cache_key)
            if hit is not None:
                logger.info("Cache hit for step %s (%s)", step.name, cache_key[:8])
                step_run.status = "cached"
                step_run.cached = True
                step_run.outputs = hit.outputs
                step_run.started = step_run.finished = utcnow()
                step_run.progress = 1.0
                ctx.events.emit(
                    "step.finished",
                    project_id=project.id, run_id=ctx.run_id, step_id=step.id,
                    data={"status": "cached", "outputs": [a.model_dump(mode="json") for a in hit.outputs]},
                )
                return step_run

        backend: ComfyBackend = await ctx.backend_for(step)

        step_run.status = "running"
        step_run.started = utcnow()
        ctx.events.emit(
            "step.started",
            project_id=project.id, run_id=ctx.run_id, step_id=step.id,
            data={"name": step.name, "workflow": workflow.name},
        )

        try:
            staged = await self._stage_inputs(backend, workflow, step, upstream, pinned)
            injected = prepare_prompt(
                api_prompt,
                workflow,
                overrides={**step.param_overrides, **staged.scalar_overrides},
                staged_inputs=staged.media_sources,
                run_key=f"{ctx.run_id}/{step.id}",
                seed_mode=seed_mode,
                rng=ctx.rng,
            )
            step_run.resolved_params = injected.resolved_params
            step_run.logs.extend(injected.warnings)

            if not injected.output_node_ids:
                raise StepFailed(
                    f"Workflow {workflow.name!r} has no ComfyWebStudio output nodes, so this step cannot "
                    "produce anything the framework can chain or preview. Add a WS *Output node in ComfyUI."
                )

            outputs = await self._execute(backend, injected.prompt, step_run, workflow, step)
            step_run.outputs = outputs
            step_run.status = "success"
            step_run.progress = 1.0
            if cacheable:
                ctx.cache.record(cache_key, ctx.run_id, step.id)

            ctx.events.emit(
                "step.finished",
                project_id=project.id, run_id=ctx.run_id, step_id=step.id,
                data={"status": "success", "outputs": [a.model_dump(mode="json") for a in outputs]},
            )

        except asyncio.CancelledError:
            step_run.status = "cancelled"
            step_run.error = "Cancelled"
            if step_run.prompt_id:
                await self._abort(backend, step_run.prompt_id)
            raise
        except StepFailed as exc:
            step_run.status = "error"
            step_run.error = str(exc)
            step_run.error_node = exc.node_id
            ctx.events.emit(
                "step.failed",
                project_id=project.id, run_id=ctx.run_id, step_id=step.id,
                data={"error": str(exc), "node_id": exc.node_id},
            )
        except (ComfyError, OSError, ValueError) as exc:
            step_run.status = "error"
            step_run.error = str(exc)
            ctx.events.emit(
                "step.failed",
                project_id=project.id, run_id=ctx.run_id, step_id=step.id,
                data={"error": str(exc)},
            )
        finally:
            step_run.finished = utcnow()

        return step_run

    # -- inputs ------------------------------------------------------------------------------------

    @dataclass(slots=True)
    class _Staged:
        media_sources: dict[str, str]
        scalar_overrides: dict[str, Any]

    async def _stage_inputs(
        self,
        backend: ComfyBackend,
        workflow: WorkflowRef,
        step: Step,
        upstream: dict[str, Artifact],
        pinned: dict[str, PinnedInput],
    ) -> _Staged:
        """Turn everything feeding this step into values its input nodes can read.

        Three sources, in the order they win: an upstream step's artifact, a value node on the canvas, and
        finally an asset the user assigned to the port by hand. The first two are mutually exclusive by
        validation — an input driven by two links is refused when the link is created.
        """
        ctx = self.ctx
        media_sources: dict[str, str] = {}
        scalar_overrides: dict[str, Any] = {}
        run_key = f"{ctx.run_id}/{step.id}"

        for port in workflow.inputs:
            scalar_port = port.kind in SCALAR_KINDS
            artifact = upstream.get(port.key)

            if artifact is not None:
                if scalar_port:
                    # A chained scalar overwrites the step's own parameter value.
                    scalar_overrides[port.key] = ctx.media.scalar_value(
                        ctx.project.id, artifact, port.kind
                    )
                else:
                    media_sources[port.key] = await ctx.media.stage_for_input(
                        ctx.project.id, backend, artifact, target_kind=port.kind, run_key=run_key
                    )
                continue

            pin = pinned.get(port.key)
            if pin is not None:
                source = pin.media_path
                if scalar_port:
                    scalar_overrides[port.key] = pin.scalar
                elif source is None:
                    raise StepFailed(
                        f"Input {port.display_name!r} is connected to {pin.label!r}, which has nothing to "
                        "give it yet — choose its media, or run the shot it comes from."
                    )
                else:
                    media_sources[port.key] = await self._stage_file(
                        backend, source, port.kind, run_key,
                        what=f"{pin.label!r}, feeding input {port.display_name!r}",
                    )
                continue

            # Nothing linked. Media inputs may still have an asset assigned to the port by hand.
            assigned = step.param_overrides.get(port.key)
            if assigned and not scalar_port:
                media_sources[port.key] = await self._stage_file(
                    backend, str(assigned), port.kind, run_key,
                    what=f"input {port.display_name!r}",
                )

        return self._Staged(media_sources=media_sources, scalar_overrides=scalar_overrides)

    async def _stage_file(
        self, backend: ComfyBackend, relative: str, kind: str, run_key: str, *, what: str
    ) -> str:
        """Put a project file where this backend can read it, naming what wanted it if it is gone."""
        path = self.ctx.store.resolve(self.ctx.project.id, relative)
        if not path.is_file():
            raise StepFailed(f"{what} points at a file that is missing: {relative}")
        return (await backend.stage(path, run_key=run_key, kind=kind)).source

    # -- execution ---------------------------------------------------------------------------------

    async def _execute(
        self,
        backend: ComfyBackend,
        prompt: dict[str, Any],
        step_run: StepRun,
        workflow: WorkflowRef,
        step: Step,
    ) -> list[Artifact]:
        ctx = self.ctx
        # ComfyUI validates this as a UUID, so it cannot carry our run/step ids; StepRun.prompt_id
        # is what correlates it back.
        prompt_id = new_uuid()
        step_run.prompt_id = prompt_id

        if not await backend.ws.wait_connected(timeout=15.0):
            raise StepFailed(
                f"Not connected to ComfyUI at {backend.config.base_url}. "
                f"{backend.ws.last_error or 'Check that it is running and reachable.'}"
            )

        timeout = ctx.settings.execution.step_timeout_s

        # Subscribe before submitting: ComfyUI may start executing before POST /prompt returns.
        async with backend.ws.subscribe(prompt_id) as events:
            try:
                await backend.http.post_prompt(
                    prompt,
                    client_id=backend.client_id,
                    prompt_id=prompt_id,
                    extra_data=self._extra_data(step, workflow),
                )
            except ComfyPromptRejected as exc:
                raise StepFailed(_describe_rejection(exc), node_id=_first_error_node(exc)) from exc

            try:
                await asyncio.wait_for(self._consume(events, step_run, step), timeout=timeout)
            except TimeoutError:
                await self._abort(backend, prompt_id)
                raise StepFailed(
                    f"Step {step.name!r} exceeded the {timeout:.0f}s timeout and was interrupted."
                ) from None

        history = await backend.http.history(prompt_id)
        if history is None:
            raise StepFailed(
                "ComfyUI finished but reported no history for this prompt, so its outputs cannot be read."
            )

        return await self._ingest(backend, history, workflow, step_run)

    def _extra_data(self, step: Step, workflow: WorkflowRef) -> dict[str, Any]:
        """Attach the UI graph so saved media stays openable in ComfyUI, plus our own provenance."""
        extra: dict[str, Any] = {
            "extra_pnginfo": {
                "webstudio": {
                    "project_id": self.ctx.project.id,
                    "run_id": self.ctx.run_id,
                    "step_id": step.id,
                    "step_name": step.name,
                }
            }
        }
        try:
            ui_graph = self.ctx.store.read_workflow(self.ctx.project.id, workflow.id, "ui")
        except Exception:  # noqa: BLE001 - optional metadata; never block a run over it
            return extra
        extra["extra_pnginfo"]["workflow"] = ui_graph
        return extra

    async def _consume(self, events, step_run: StepRun, step: Step) -> None:
        """Follow one prompt's events until it terminates."""
        ctx = self.ctx
        async for event in events:
            if event.type == "executing":
                step_run.current_node = event.node_id
            elif event.type == "progress_state":
                nodes = event.data.get("nodes") or {}
                finished = sum(1 for n in nodes.values() if n.state == "finished")
                running = [n for n in nodes.values() if n.state == "running"]
                fraction = running[0].fraction if running else 0.0
                total = max(len(nodes), 1)
                step_run.progress = min(0.99, (finished + fraction) / total)
                ctx.events.emit(
                    "step.progress",
                    project_id=ctx.project.id, run_id=ctx.run_id, step_id=step.id,
                    data={"progress": step_run.progress, "node": step_run.current_node},
                )
            elif event.type == "progress" and "text" in event.data:
                step_run.logs.append(str(event.data["text"]))
            elif event.type == "preview":
                ctx.events.emit(
                    "step.preview",
                    project_id=ctx.project.id, run_id=ctx.run_id, step_id=step.id,
                    data={"mime": event.data.get("mime"), "size": len(event.data.get("image") or b"")},
                )
            elif event.type == "execution_error":
                raise StepFailed(
                    f"{event.data.get('node_type') or 'A node'} failed: {event.data.get('message')}",
                    node_id=event.node_id,
                )
            elif event.type == "execution_interrupted":
                raise StepFailed("Execution was interrupted on the ComfyUI server.", node_id=event.node_id)
            elif event.type == "prompt_done":
                return

    async def _abort(self, backend: ComfyBackend, prompt_id: str) -> None:
        """Stop a prompt whether it is running or merely queued."""
        try:
            await backend.http.interrupt(prompt_id)
            await backend.http.cancel_queued([prompt_id])
        except ComfyError as exc:
            logger.warning("Could not abort prompt %s: %s", prompt_id, exc)

    # -- outputs -----------------------------------------------------------------------------------

    async def _ingest(
        self,
        backend: ComfyBackend,
        history: dict[str, Any],
        workflow: WorkflowRef,
        step_run: StepRun,
    ) -> list[Artifact]:
        """Read our structured payloads out of history and file each artifact into the project."""
        ctx = self.ctx
        outputs = history.get("outputs") or {}
        ports_by_node = {p.node_id: p for p in workflow.outputs}

        artifacts: list[Artifact] = []
        seen_ports: set[str] = set()

        for node_id, payload in outputs.items():
            for entry in (payload or {}).get(UI_KEY) or []:
                port_key = str(entry.get("port_name") or "")
                kind = str(entry.get("kind") or "file")
                port = ports_by_node.get(str(node_id))
                if port is not None:
                    port_key, kind = port.key, port.kind

                meta = dict(entry.get("meta") or {})
                for ref_dict in entry.get("files") or []:
                    artifact = await ctx.media.ingest_output(
                        ctx.project.id,
                        backend,
                        kind=kind,
                        port_key=port_key,
                        ref=ComfyFileRef.from_dict(ref_dict),
                        meta=meta,
                    )
                    artifacts.append(artifact)
                    seen_ports.add(port_key)

        missing = [p.key for p in workflow.outputs if p.key not in seen_ports]
        if missing:
            # Usually a bypassed or muted node in the graph. Worth saying out loud: downstream steps that
            # depend on those ports will fail with a less obvious message.
            step_run.logs.append(
                "These output ports produced nothing: " + ", ".join(missing)
            )

        if not artifacts:
            status = (history.get("status") or {}).get("status_str")
            raise StepFailed(
                f"The workflow completed ({status or 'no status'}) but wrote no ComfyWebStudio outputs. "
                "Check that its WS output nodes are connected and not bypassed."
            )

        return artifacts


def _describe_rejection(exc: ComfyPromptRejected) -> str:
    """Turn ComfyUI's validation error into something that points at the problem."""
    if not exc.node_errors:
        return f"ComfyUI rejected the workflow: {exc}"
    parts = []
    for node_id, info in list(exc.node_errors.items())[:4]:
        class_type = info.get("class_type", "node")
        messages = "; ".join(
            str(e.get("message") or e.get("type")) for e in (info.get("errors") or [])[:3]
        )
        parts.append(f"{class_type} (node {node_id}): {messages}")
    return "ComfyUI rejected the workflow — " + " | ".join(parts)


def _first_error_node(exc: ComfyPromptRejected) -> str | None:
    return next(iter(exc.node_errors), None)
