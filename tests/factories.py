"""Builders for test fixtures, so graph and store tests need no ComfyUI."""

from __future__ import annotations

from comfywebstudio.core.models import ParamSpec, PortSpec, WorkflowRef


def make_workflow(name: str, *, inputs=(), outputs=(), params=()) -> WorkflowRef:
    """A workflow whose ports are declared directly rather than discovered."""
    ports = [
        PortSpec(key=key, direction="in", kind=kind, node_id=f"in_{key}") for key, kind in inputs
    ] + [PortSpec(key=key, direction="out", kind=kind, node_id=f"out_{key}") for key, kind in outputs]
    param_specs = [
        ParamSpec(key=key, kind=kind, default=default, node_id=f"p_{key}", input_name="value")
        for key, kind, default in params
    ]
    return WorkflowRef(name=name, ports=ports, params=param_specs)
