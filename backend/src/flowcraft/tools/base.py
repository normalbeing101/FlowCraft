"""Pluggable tool interface.

A tool is a trusted in-process callable with a STRICT, declared contract:
  - input schema (validated before invocation)
  - output schema (validated after invocation; malformed output = failure)
  - required configuration (validated at workflow-import time)
  - permission requirements (declared; deployment may grant/deny)
  - idempotency declaration (drives the retry contract -- see errors.py)
  - expected failure behavior documented per tool

Third-party tools register via `register_tool()` from their own package; no
core file needs editing. Untrusted/isolated execution is NOT provided yet --
see docs/security.md trust model.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Literal

from ..errors import FlowcraftError, FailureClass


@dataclass
class ToolContext:
    """What the engine hands to a tool. Tools cannot reach the DB directly."""

    run_id: str
    step_id: str
    attempt: int                      # 1-based
    workspace_id: str
    providers: dict                   # name -> ProviderAdapter
    network_policy: Any               # security.network.NetworkPolicy
    settings: Any                     # config.Settings
    idempotency_key: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    name: str
    description: str
    version: str = "1"
    input_schema: dict[str, Any] = field(default_factory=dict)   # JSON Schema
    output_schema: dict[str, Any] = field(default_factory=dict)  # JSON Schema
    config_schema: dict[str, Any] = field(default_factory=dict)
    permissions: list[str] = field(default_factory=list)         # e.g. ["network.outbound"]
    idempotent: bool = False             # True => safe to replay on retry
    supports_idempotency_key: bool = False  # True => engine passes key for dedupe
    unknown_effect_class: FailureClass = FailureClass.GUARDED
    side_effects: Literal["none", "read", "write"] = "read"

    def validate_config(self, config: dict[str, Any]) -> None:
        errs = _jsonschema_errors(config, self.config_schema) if self.config_schema else []
        if errs:
            raise FlowcraftError(
                f"tool {self.name!r} config invalid: {'; '.join(errs)}",
                code="tool_config_invalid", failure_class=FailureClass.CONFIG)


def _jsonschema_errors(instance: Any, schema: dict[str, Any]) -> list[str]:
    """Small built-in validator for the subset of JSON Schema we use
    (type, properties, required, enum, items). Avoids a heavy dependency."""
    errs: list[str] = []
    t = schema.get("type")
    py_types = {"object": dict, "array": list, "string": str,
                "number": (int, float), "integer": int, "boolean": bool}
    if t and t in py_types and not isinstance(instance, py_types[t]):
        errs.append(f"expected type {t}, got {type(instance).__name__}")
        return errs
    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errs.append(f"missing required property {req!r}")
        props = schema.get("properties", {})
        for k, v in instance.items():
            if k in props:
                errs.extend(f"{k}: {e}" for e in _jsonschema_errors(v, props[k]))
            elif schema.get("additionalProperties") is False:
                errs.append(f"unexpected property {k!r}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errs.extend(f"[{i}]: {e}" for e in _jsonschema_errors(item, schema["items"]))
    if "enum" in schema and instance not in schema["enum"]:
        errs.append(f"value {instance!r} not in enum {schema['enum']}")
    return errs


class Tool(abc.ABC):
    spec: ToolSpec

    @abc.abstractmethod
    async def run(self, inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Execute and return a structured output dict. Raise FlowcraftError
        with an explicit FailureClass on failure."""


_REGISTRY: dict[str, Tool] = {}


def register_tool(tool: Tool, *, replace: bool = False) -> None:
    if tool.spec.name in _REGISTRY and not replace:
        raise FlowcraftError(f"tool {tool.spec.name!r} already registered",
                             code="duplicate_tool", failure_class=FailureClass.CONFIG)
    _REGISTRY[tool.spec.name] = tool


def get_tool(name: str) -> Tool:
    if name not in _REGISTRY:
        raise FlowcraftError(
            f"unknown step type {name!r}; registered tools: {sorted(_REGISTRY)}",
            code="unknown_tool", failure_class=FailureClass.CONFIG)
    return _REGISTRY[name]


def list_tools() -> dict[str, Tool]:
    return dict(_REGISTRY)
