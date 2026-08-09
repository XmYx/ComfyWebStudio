"""Write values into an API-format prompt before submitting it.

Everything a run varies — parameter overrides, chained inputs, seeds, the run key that decides where output
lands — is applied here, on a deep copy. The stored workflow is never mutated, so what the user sees in the
editor always matches what is on disk.
"""

from __future__ import annotations

import copy
import logging
import random
from dataclasses import dataclass, field
from typing import Any

from ..core.models import ParamSpec, PortSpec, SeedMode, WorkflowRef

logger = logging.getLogger(__name__)

MAX_SEED = 0xFFFFFFFFFFFFFFFF

#: The widget our output nodes read to decide where to write.
RUN_KEY_INPUT = "run_key"


@dataclass(slots=True)
class InjectionResult:
    prompt: dict[str, Any]
    resolved_params: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: Output node ids, so a run can ask ComfyUI to execute only what we care about.
    output_node_ids: list[str] = field(default_factory=list)


def prepare_prompt(
    api_prompt: dict[str, Any],
    workflow: WorkflowRef,
    *,
    overrides: dict[str, Any] | None = None,
    staged_inputs: dict[str, str] | None = None,
    run_key: str = "",
    seed_mode: SeedMode = "fixed",
    rng: random.Random | None = None,
) -> InjectionResult:
    """Produce the exact graph to submit for one step run.

    ``staged_inputs`` maps input port key to the string a ``WS*Input`` node should read — an absolute path
    on a shared filesystem, or an uploaded name on a remote instance.
    """
    prompt = copy.deepcopy(api_prompt)
    result = InjectionResult(prompt=prompt)

    _apply_params(result, workflow, overrides or {}, seed_mode, rng or random.Random())
    _apply_staged_inputs(result, workflow, staged_inputs or {})
    _apply_run_key(result, workflow, run_key)

    return result


def _apply_params(
    result: InjectionResult,
    workflow: WorkflowRef,
    overrides: dict[str, Any],
    seed_mode: SeedMode,
    rng: random.Random,
) -> None:
    for param in workflow.params:
        value = overrides.get(param.key, param.default)
        if param.is_seed:
            value = _resolve_seed(value, seed_mode, rng)
        coerced = _coerce(value, param)

        # A promoted subgraph input can drive several node inputs at once; all of them get the value.
        written = 0
        for target in param.all_targets:
            node = result.prompt.get(target.node_id)
            if not isinstance(node, dict):
                continue
            node.setdefault("inputs", {})[target.input_name] = coerced
            written += 1

        if not written:
            result.warnings.append(
                f"Parameter {param.display_name!r} points at node {param.node_id} which is no longer in "
                "the workflow; it was ignored."
            )
            continue

        result.resolved_params[param.key] = coerced


def _resolve_seed(value: Any, seed_mode: SeedMode, rng: random.Random) -> int:
    try:
        current = int(value)
    except (TypeError, ValueError):
        current = 0
    if seed_mode == "randomize":
        return rng.randint(0, MAX_SEED)
    if seed_mode == "increment":
        return (current + 1) % (MAX_SEED + 1)
    return current


def _apply_staged_inputs(
    result: InjectionResult, workflow: WorkflowRef, staged: dict[str, str]
) -> None:
    by_key = {p.key: p for p in workflow.inputs}
    for port_key, source in staged.items():
        port = by_key.get(port_key)
        if port is None:
            result.warnings.append(f"No input port {port_key!r} in this workflow; the link was ignored.")
            continue
        node = result.prompt.get(port.node_id)
        if not isinstance(node, dict):
            result.warnings.append(
                f"Input port {port.display_name!r} points at node {port.node_id} which is no longer in "
                "the workflow; the link was ignored."
            )
            continue
        node.setdefault("inputs", {})[_value_input_of(port)] = source


def _value_input_of(port: PortSpec) -> str:
    return str(port.meta.get("value_input") or ("value" if port.kind in {"string", "int", "float", "boolean"} else "source"))


def _apply_run_key(result: InjectionResult, workflow: WorkflowRef, run_key: str) -> None:
    """Point every output node at this run's directory.

    Doing this by writing a widget — rather than relying on hidden inputs — keeps the submitted graph fully
    self-describing: what ComfyUI receives is exactly what the artifact paths will reflect. It also means a
    re-run always executes the output node (its inputs changed) while leaving upstream nodes cached.
    """
    for port in workflow.outputs:
        node = result.prompt.get(port.node_id)
        if not isinstance(node, dict):
            result.warnings.append(
                f"Output port {port.display_name!r} points at node {port.node_id} which is no longer in "
                "the workflow; it will produce nothing."
            )
            continue
        node.setdefault("inputs", {})[RUN_KEY_INPUT] = run_key
        result.output_node_ids.append(port.node_id)


def _coerce(value: Any, param: ParamSpec) -> Any:
    """Best-effort conversion to the widget's type.

    ComfyUI validates strictly, so sending ``"7"`` where an INT is expected fails the whole prompt. Better
    to convert here and keep going than to reject a value the user clearly meant.
    """
    if value is None:
        return param.default

    try:
        if param.kind == "int":
            return int(float(value))
        if param.kind == "float":
            return float(value)
        if param.kind == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
        if param.kind == "string":
            return value if isinstance(value, str) else str(value)
        if param.kind == "choice":
            text = str(value)
            if param.choices and text not in param.choices:
                logger.debug("Value %r not in choices for %s; sending it anyway", text, param.key)
            return text
    except (TypeError, ValueError):
        logger.warning("Could not coerce %r for parameter %s; using its default", value, param.key)
        return param.default

    return value


def resolve_param_values(workflow: WorkflowRef, overrides: dict[str, Any]) -> dict[str, Any]:
    """Effective values for every parameter, without touching a prompt.

    Used by the cache key and by the UI's "what will actually run" display.
    """
    return {p.key: _coerce(overrides.get(p.key, p.default), p) for p in workflow.params}
