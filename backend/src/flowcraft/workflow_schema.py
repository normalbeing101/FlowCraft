"""Versioned workflow definition format (YAML/JSON).

This schema is intentionally independent of any frontend or database.
`WORKFLOW_SCHEMA_VERSION` is part of every file; unknown versions are rejected.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WORKFLOW_SCHEMA_VERSION = "1"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _valid_id(v: str) -> str:
    if not _ID_RE.match(v):
        raise ValueError(
            "identifiers must match ^[a-z0-9][a-z0-9_-]{0,63}$ (got %r)" % v
        )
    return v


class RetryPolicy(BaseModel):
    """Bounded retry policy. Retries happen only when BOTH the failure class
    is retryable and the tool/idempotency rules allow replay."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=1, ge=1, le=10,
                              description="Total attempts including the first.")
    backoff_seconds: float = Field(default=1.0, ge=0, le=60)
    backoff_multiplier: float = Field(default=2.0, ge=1, le=5)
    max_backoff_seconds: float = Field(default=30.0, ge=0, le=300)


class StepCondition(BaseModel):
    """Deterministic branch condition evaluated against the step output.

    Compares a JSON-pointer-ish dotted path in the step's structured output
    to a literal value. No arbitrary expressions are supported by design.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Dotted path into the step output, e.g. 'verdict'")
    equals: Any = None
    in_: Optional[list[Any]] = Field(default=None, alias="in")

    model_config["populate_by_name"] = True


class ApprovalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
    timeout_seconds: int = Field(default=86_400, ge=1, le=7 * 86_400)
    on_timeout: Literal["fail", "reject"] = "fail"


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: str = Field(description="Registered tool name, e.g. 'http.request'")
    config: dict[str, Any] = Field(default_factory=dict)
    input: dict[str, Any] = Field(
        default_factory=dict,
        description="Map of tool-input name -> reference/literal. "
                    "References use '${...}' syntax.",
    )
    output_to: Optional[str] = Field(
        default=None, description="Optional dotted run-context path to store output under."
    )
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_seconds: float = Field(default=60.0, gt=0, le=900)
    on_failure: Literal["fail_run", "continue"] = "fail_run"
    approve: Optional[ApprovalConfig] = None
    branches: Optional[dict[str, str]] = Field(
        default=None,
        description="Optional map of condition-name -> next step id. Requires 'when'.",
    )
    when: Optional[StepCondition] = Field(
        default=None, description="Evaluate against this step's output to pick a branch."
    )

    @field_validator("id")
    @classmethod
    def _v_id(cls, v: str) -> str:
        return _valid_id(v)

    @model_validator(mode="after")
    def _v_branches(self) -> "Step":
        if self.branches is not None and self.when is None:
            raise ValueError("step with 'branches' requires a 'when' condition")
        if self.approve is not None and self.type != "approval.request":
            raise ValueError("'approve' config is only valid on approval.request steps")
        return self


class WorkflowMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    version: int = Field(default=1, ge=1)
    tags: list[str] = Field(default_factory=list)


class ExecutionLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps_executed: int = Field(default=100, ge=1, le=1000)
    max_run_seconds: int = Field(default=600, ge=1, le=86_400)


class Workflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    id: str
    metadata: WorkflowMeta
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)
    entry: str
    steps: list[Step]

    @field_validator("schema_version")
    @classmethod
    def _v_schema(cls, v: str) -> str:
        if v != WORKFLOW_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {v!r}; this build supports {WORKFLOW_SCHEMA_VERSION!r}"
            )
        return v

    @field_validator("id")
    @classmethod
    def _v_id(cls, v: str) -> str:
        return _valid_id(v)

    @model_validator(mode="after")
    def _v_graph(self) -> "Workflow":
        ids = [s.id for s in self.steps]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate step ids: {sorted(dupes)}")
        idset = set(ids)
        if self.entry not in idset:
            raise ValueError(f"entry step {self.entry!r} not found in steps")
        for s in self.steps:
            if s.branches:
                for target in s.branches.values():
                    if target not in idset:
                        raise ValueError(
                            f"step {s.id!r} branch targets unknown step {target!r}"
                        )
        # Linear fallthrough targets must exist too (branch map covers that);
        # cycles are rejected: MVP supports sequential + conditional branching.
        self._reject_cycles(idset)
        return self

    def _reject_cycles(self, idset: set[str]) -> None:
        nxt: dict[str, list[str]] = {}
        ordered = {s.id: s for s in self.steps}
        idx = {s.id: i for i, s in enumerate(self.steps)}
        for s in self.steps:
            targets = list(s.branches.values()) if s.branches else []
            if not s.branches and idx[s.id] + 1 < len(self.steps):
                targets.append(self.steps[idx[s.id] + 1].id)
            nxt[s.id] = targets
        state: dict[str, int] = {}

        def visit(n: str) -> None:
            if state.get(n) == 1:
                raise ValueError(f"cycle detected at step {n!r}; loops are not supported in schema v1")
            if state.get(n) == 2:
                return
            state[n] = 1
            for m in nxt.get(n, []):
                visit(m)
            state[n] = 2

        for n in ordered:
            visit(n)


def parse_workflow(raw: str) -> Workflow:
    """Parse YAML or JSON text into a validated Workflow."""
    import yaml

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        from .errors import ValidationError

        raise ValidationError(f"could not parse workflow file: {e}") from e
    from .errors import ValidationError

    if not isinstance(data, dict):
        raise ValidationError("workflow file must contain a mapping at the top level")
    try:
        return Workflow.model_validate(data)
    except Exception as e:
        raise ValidationError(f"workflow schema violation: {e}", details={"error": str(e)}) from e
