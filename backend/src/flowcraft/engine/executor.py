"""The execution engine.

Run state machine (explicit transitions persisted in RunRecord.status):
  pending -> running -> {succeeded, failed, rejected, cancelled, timed_out}
  running <-> awaiting_approval   (approval gate pauses before side effects)

Guarantees vs best-effort (see docs/architecture.md):
  * Deterministic sequential step order; no step concurrency.
  * Retry happens only when failure_class.retryable_by_class AND the step is
    replay-safe: tool declares idempotent=True OR an idempotency key exists
    for a tool that supports one. Otherwise GUARDED (hard stop, recorded).
  * Duplicate execute requests with the same (workspace, idempotency_key)
    return the existing run instead of starting another.
  * NOT exactly-once. A crash between side effect and persistence can cause
    duplicates on manual re-run unless the downstream honored the key.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..errors import FailureClass, FlowcraftError, ValidationError
from ..persistence.models import (
    ApprovalRecord, RunRecord, RunStatus, StepRecord, StepStatus, WorkflowRecord, utcnow,
)
from ..providers.registry import build_providers
from ..security.network import NetworkPolicy
from ..security.redaction import redact
from ..tools import ai_tool as _ai, approval_tool as _appr, http_tool as _http, transform_tool as _xform  # noqa: F401 (register built-ins)
from ..tools.base import ToolContext, _jsonschema_errors, get_tool, list_tools
from ..workflow_schema import Step, Workflow, parse_workflow


class CancelToken:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


_ACTIVE: dict[str, CancelToken] = {}


def request_cancel(run_id: str) -> bool:
    """Cancel a live in-process run at the next step boundary or after the
    current attempt finishes. Returns False if the run is not active here."""
    tok = _ACTIVE.get(run_id)
    if tok is None:
        return False
    tok.cancel()
    return True


class Engine:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession],
                 settings: Settings) -> None:
        self.sf = session_factory
        self.settings = settings
        self.providers = build_providers()
        self.network_policy = NetworkPolicy(
            allow_private=settings.http_allow_private,
            allow_localhost=settings.http_allow_localhost,
            allowed_hosts=settings.allowed_hosts_list(),
            denied_hosts=settings.denied_hosts_list(),
        )

    # ---------- workflow management ----------

    async def validate_definition(self, raw: str) -> Workflow:
        wf = parse_workflow(raw)
        self._validate_tools(wf)
        return wf

    def _validate_tools(self, wf: Workflow) -> None:
        tools = list_tools()
        for step in wf.steps:
            if step.type not in tools:
                raise ValidationError(
                    f"step {step.id!r}: unknown tool type {step.type!r}; "
                    f"registered: {sorted(tools)}")
            spec = tools[step.type].spec
            try:
                spec.validate_config(step.config)
            except FlowcraftError as e:
                raise ValidationError(str(e)) from e
            if step.type == "ai.generate":
                pname = step.config.get("provider")
                if pname and pname not in self.providers:
                    raise ValidationError(
                        f"step {step.id!r}: provider profile {pname!r} not configured; "
                        f"available: {sorted(self.providers)}")
        if wf.limits.max_run_seconds > self.settings.max_run_seconds_cap:
            raise ValidationError(
                f"max_run_seconds {wf.limits.max_run_seconds} exceeds deployment cap "
                f"{self.settings.max_run_seconds_cap}")

    async def import_workflow(self, raw: str, *, workspace_id: str) -> WorkflowRecord:
        wf = await self.validate_definition(raw)
        async with self.sf() as s:
            existing = await s.scalar(select(WorkflowRecord).where(
                WorkflowRecord.workspace_id == workspace_id,
                WorkflowRecord.workflow_id == wf.id,
                WorkflowRecord.version == wf.metadata.version))
            if existing:
                existing.definition = wf.model_dump(mode="json")
                await s.commit()
                return existing
            rec = WorkflowRecord(workspace_id=workspace_id, workflow_id=wf.id,
                                 version=wf.metadata.version,
                                 definition=wf.model_dump(mode="json"))
            s.add(rec)
            await s.commit()
            await s.refresh(rec)
            return rec

    # ---------- execution ----------

    async def execute(self, workflow_id: str, *, workspace_id: str,
                      inputs: dict[str, Any] | None = None,
                      idempotency_key: str | None = None,
                      version: int | None = None) -> RunRecord:
        async with self.sf() as s:
            if idempotency_key:
                dup = await s.scalar(select(RunRecord).where(
                    RunRecord.workspace_id == workspace_id,
                    RunRecord.idempotency_key == idempotency_key))
                if dup is not None:
                    return dup

            q = select(WorkflowRecord).where(
                WorkflowRecord.workspace_id == workspace_id,
                WorkflowRecord.workflow_id == workflow_id)
            q = q.order_by(WorkflowRecord.version.desc())
            if version is not None:
                q = q.where(WorkflowRecord.version == version)
            wrec = await s.scalar(q.limit(1))
            if wrec is None:
                raise ValidationError(f"workflow {workflow_id!r} not found in workspace")

            wf = Workflow.model_validate(wrec.definition)
            self._validate_tools(wf)

            run = RunRecord(run_id=str(uuid.uuid4()), workspace_id=workspace_id,
                            workflow_id=workflow_id,
                            workflow_version=wrec.version,
                            status=RunStatus.RUNNING.value,
                            inputs=redact(inputs or {}),
                            idempotency_key=idempotency_key,
                            started_at=utcnow())
            s.add(run)
            try:
                await s.flush()
            except Exception:
                await s.rollback()
                if idempotency_key:
                    dup = await s.scalar(select(RunRecord).where(
                        RunRecord.workspace_id == workspace_id,
                        RunRecord.idempotency_key == idempotency_key))
                    if dup is not None:
                        return dup
                raise
            await s.commit()
            run_id = run.run_id

        await self._continue_pipeline(run_id, wf, inputs or {}, idempotency_key,
                                      start_at=wf.entry,
                                      context={"run": {"inputs": inputs or {}}})
        async with self.sf() as s:
            return await s.get(RunRecord, run_id)  # type: ignore[return-value]

    # ---------- approval handling ----------

    async def decide_approval(self, run_id: str, *, workspace_id: str, approved: bool,
                              decided_by: str, comment: str = "") -> RunRecord:
        """Human decision recorded via API/CLI. Authorization lives HERE, in
        deterministic application code — never in model output."""
        async with self.sf() as s:
            run = await s.get(RunRecord, run_id)
            if run is None or run.workspace_id != workspace_id:
                raise ValidationError("run not found in workspace")
            if run.status != RunStatus.AWAITING_APPROVAL.value:
                raise ValidationError(f"run is {run.status!r}, not awaiting approval")
            appr = await s.scalar(select(ApprovalRecord).where(
                ApprovalRecord.run_id == run_id,
                ApprovalRecord.decided_at.is_(None)))
            if appr is None:
                raise ValidationError("no pending approval for this run")
            appr.approved = approved
            appr.decided_by = decided_by[:128]
            appr.comment = comment[:2000]
            appr.decided_at = utcnow()
            wf_rec = await s.scalar(select(WorkflowRecord).where(
                WorkflowRecord.workspace_id == workspace_id,
                WorkflowRecord.workflow_id == run.workflow_id,
                WorkflowRecord.version == run.workflow_version))
            wf = Workflow.model_validate(wf_rec.definition)
            resume_from = self._next_after(wf, appr.step_id)
            inputs = dict(run.inputs or {})
            key = run.idempotency_key
            if not approved:
                run.status = RunStatus.REJECTED.value
                run.finished_at = utcnow()
                run.current_step = None
                run.approval_payload = None
                run.error = {"code": "approval_rejected", "class": "permanent",
                             "message": f"approval step {appr.step_id!r} rejected "
                                        f"by {decided_by}"[:500]}
                sr = await s.scalar(select(StepRecord).where(
                    StepRecord.run_id == run_id, StepRecord.step_id == appr.step_id))
                if sr is not None:
                    sr.status = StepStatus.FAILED.value
                    sr.output = {"approved": False, "decided_by": decided_by,
                                 "comment": comment}
                    sr.failure_class = FailureClass.PERMANENT.value
                    sr.finished_at = utcnow()
                await s.commit()
                return run
            run.status = RunStatus.RUNNING.value
            run.approval_payload = None
            await s.commit()
        context = await self._build_context(run_id, wf)
        await self._continue_pipeline(run_id, wf, inputs, key,
                                      start_at=resume_from, context=context)
        async with self.sf() as s:
            return await s.get(RunRecord, run_id)  # type: ignore[return-value]

    @staticmethod
    def _next_after(wf: Workflow, done_step_id: str) -> str | None:
        ids = [s.id for s in wf.steps]
        idx = ids.index(done_step_id)
        step = wf.steps[idx]
        if step.branches:
            return step.branches.get("default")
        if idx + 1 < len(ids):
            return ids[idx + 1]
        return None

    # ---------- pipeline ----------

    async def _continue_pipeline(self, run_id: str, wf: Workflow,
                                 inputs: dict[str, Any],
                                 idempotency_key: str | None,
                                 start_at: str | None,
                                 context: dict[str, Any]) -> None:
        order = [st.id for st in wf.steps]
        steps_by_id = {st.id: st for st in wf.steps}
        if start_at is None:
            async with self.sf() as s:
                await self._finish(s, run_id, RunStatus.SUCCEEDED, context=context)
            return
        token = CancelToken()
        _ACTIVE[run_id] = token
        deadline = time.monotonic() + min(wf.limits.max_run_seconds,
                                          self.settings.max_run_seconds_cap)
        executed = 0
        next_id = start_at
        try:
            while next_id is not None:
                if token.cancelled:
                    async with self.sf() as s:
                        await self._finish(s, run_id, RunStatus.CANCELLED,
                                           error={"code": "cancelled",
                                                  "class": FailureClass.CANCELLED.value,
                                                  "message": "cancelled by operator"},
                                           context=context)
                    return
                if time.monotonic() > deadline:
                    async with self.sf() as s:
                        await self._finish(s, run_id, RunStatus.TIMED_OUT,
                                           error={"code": "run_timeout",
                                                  "class": FailureClass.CONFIG.value,
                                                  "message": "run exceeded max_run_seconds"},
                                           context=context)
                    return
                if executed >= wf.limits.max_steps_executed:
                    async with self.sf() as s:
                        await self._finish(s, run_id, RunStatus.FAILED,
                                           error={"code": "step_limit_exceeded",
                                                  "class": FailureClass.CONFIG.value,
                                                  "message": f"limit {wf.limits.max_steps_executed}"},
                                           context=context)
                    return
                step = steps_by_id[next_id]
                executed += 1
                outcome = await self._execute_step(step, run_id, context,
                                                   idempotency_key, token, order)
                if outcome["kind"] == "await":
                    return  # stays awaiting_approval until decide_approval()
                if outcome["kind"] == "fail_run":
                    async with self.sf() as s:
                        await self._finish(s, run_id, RunStatus.FAILED,
                                           error=outcome.get("error"), context=context)
                    return
                next_id = outcome.get("next")
            async with self.sf() as s:
                await self._finish(s, run_id, RunStatus.SUCCEEDED, context=context)
        finally:
            _ACTIVE.pop(run_id, None)

    # ---------- step execution with retry contract ----------

    async def _execute_step(self, step: Step, run_id: str, context: dict,
                            run_idem_key: str | None, token: CancelToken,
                            order: list[str]) -> dict:
        tool = get_tool(step.type)
        spec = tool.spec
        try:
            resolved = self._resolve_inputs(step.input, context)
        except FlowcraftError as e:
            await self._record_step_failure(run_id, step, e, attempts=0)
            return {"kind": "fail_run", "error": e.to_dict()}

        errs = _jsonschema_errors(resolved, spec.input_schema) if spec.input_schema else []
        if errs:
            e = FlowcraftError(f"input schema violation: {'; '.join(errs)}",
                               code="input_invalid", failure_class=FailureClass.CONFIG)
            await self._record_step_failure(run_id, step, e, attempts=0,
                                            input_=redact(resolved))
            return {"kind": "fail_run", "error": e.to_dict()}

        # Approval gate: pause BEFORE executing anything consequential.
        if step.approve is not None:
            async with self.sf() as s:
                run = await s.get(RunRecord, run_id)
                pending = await s.scalar(select(ApprovalRecord).where(
                    ApprovalRecord.run_id == run_id,
                    ApprovalRecord.step_id == step.id,
                    ApprovalRecord.decided_at.is_(None)))
                if pending is None:
                    s.add(StepRecord(run_id=run_id, step_id=step.id, tool=step.type,
                                     status=StepStatus.AWAITING_APPROVAL.value,
                                     input=redact(resolved), started_at=utcnow()))
                    s.add(ApprovalRecord(run_id=run_id, step_id=step.id))
                    run.status = RunStatus.AWAITING_APPROVAL.value
                    run.current_step = step.id
                    run.approval_payload = redact({
                        "reason": step.approve.reason,
                        "summary": resolved.get("summary"),
                        "payload": resolved.get("payload"),
                        "timeout_seconds": step.approve.timeout_seconds})
                    await s.commit()
                return {"kind": "await"}

        step_idem_key = None
        if spec.supports_idempotency_key:
            base = run_idem_key or run_id
            step_idem_key = f"{base}:{step.id}"

        timeout = min(step.timeout_seconds, self.settings.max_step_timeout_cap)
        last_err: FlowcraftError | None = None
        attempt_log: list[dict] = []
        max_attempts = max(1, step.retry.max_attempts)
        attempt = 0

        for attempt in range(1, max_attempts + 1):
            if token.cancelled:
                return {"kind": "fail_run",
                        "error": {"code": "cancelled", "class": "cancelled",
                                  "message": "cancelled before attempt"}}
            ctx = ToolContext(run_id=run_id, step_id=step.id, attempt=attempt,
                              workspace_id=self.settings.workspace_id,
                              providers=self.providers,
                              network_policy=self.network_policy,
                              settings=self.settings,
                              idempotency_key=step_idem_key,
                              extras={"config": step.config})
            started = time.monotonic()
            try:
                out = await asyncio.wait_for(tool.run(resolved, ctx), timeout=timeout)
                verrs = (_jsonschema_errors(out, spec.output_schema)
                         if spec.output_schema else [])
                if verrs:
                    raise FlowcraftError(
                        f"tool produced malformed output: {'; '.join(verrs)}",
                        code="malformed_output",
                        failure_class=FailureClass.TRANSIENT if spec.idempotent
                        else FailureClass.GUARDED)
                duration = round((time.monotonic() - started) * 1000, 1)
                await self._record_step_success(run_id, step, out, resolved,
                                                attempt_log, attempt, duration)
                redacted_out = redact(out)
                if step.output_to:
                    self._set_path(context, step.output_to, redacted_out)
                else:
                    context[step.id] = redacted_out
                nxt = self._next_step_id(step, redacted_out, order)
                return {"kind": "ok", "next": nxt}
            except asyncio.TimeoutError:
                cls = FailureClass.TRANSIENT if spec.idempotent else FailureClass.GUARDED
                last_err = FlowcraftError(f"step timed out after {timeout}s",
                                          code="step_timeout", failure_class=cls)
            except FlowcraftError as e:
                last_err = e
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_err = FlowcraftError(
                    f"unexpected tool error: {type(e).__name__}",
                    code="tool_crash",
                    failure_class=FailureClass.TRANSIENT if spec.idempotent
                    else FailureClass.GUARDED,
                    details={"exception": str(e)[:200]})

            attempt_log.append(redact(last_err.to_dict()))
            # ---- retry contract ----
            if not last_err.failure_class.retryable_by_class:
                break
            if not (spec.idempotent or step_idem_key):
                last_err = FlowcraftError(
                    last_err.message + " — retry refused: tool is not idempotent "
                    "and no idempotency key was declared",
                    code="retry_refused", failure_class=FailureClass.GUARDED,
                    details={"original_code": last_err.code})
                attempt_log[-1] = redact(last_err.to_dict())
                break
            if attempt < max_attempts:
                delay = min(step.retry.backoff_seconds *
                            (step.retry.backoff_multiplier ** (attempt - 1)),
                            step.retry.max_backoff_seconds)
                if last_err.retry_after:
                    delay = max(delay, min(last_err.retry_after,
                                           step.retry.max_backoff_seconds))
                remaining = max(0.0, deadline - time.monotonic())
                await asyncio.sleep(min(delay, remaining))

        guarded = last_err.failure_class == FailureClass.GUARDED
        if step.on_failure == "continue" and not guarded:
            await self._record_step_failure(run_id, step, last_err, attempt,
                                             input_=redact(resolved), log=attempt_log)
            context[step.id] = {"failed": True, "error": redact(last_err.to_dict())}
            return {"kind": "ok", "next": self._next_step_id(step, {}, order)}
        await self._record_step_failure(
            run_id, step, last_err, attempt, input_=redact(resolved), log=attempt_log,
            status=(StepStatus.BLOCKED_GUARDED.value if guarded
                    else StepStatus.FAILED.value))
        return {"kind": "fail_run", "error": redact(last_err.to_dict())}

    # ---------- helpers ----------

    def _resolve_inputs(self, mapping: dict, context: dict) -> dict:
        return {k: self._resolve_value(v, context) for k, v in mapping.items()}

    def _resolve_value(self, v: Any, context: dict) -> Any:
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            path = v[2:-1].strip()
            try:
                return self._get_path(context, path)
            except KeyError as e:
                raise FlowcraftError(f"unresolved reference {v!r}: {e}",
                                     code="reference_error",
                                     failure_class=FailureClass.CONFIG) from e
        if isinstance(v, dict):
            return {k: self._resolve_value(x, context) for k, x in v.items()}
        if isinstance(v, list):
            return [self._resolve_value(x, context) for x in v]
        return v

    @staticmethod
    def _get_path(ctx: dict, dotted: str) -> Any:
        cur: Any = ctx
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                raise KeyError(dotted)
        return cur

    @staticmethod
    def _set_path(ctx: dict, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        cur = ctx
        for p in parts[:-1]:
            if not isinstance(cur, dict):
                raise FlowcraftError(f"cannot write into non-object at {dotted!r}",
                                     code="output_path_error",
                                     failure_class=FailureClass.CONFIG)
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value

    def _next_step_id(self, step: Step, output: dict, order: list[str]) -> str | None:
        """Deterministic branch selection against the validated step output."""
        if step.branches and step.when:
            val = None
            try:
                val = self._get_path(output, step.when.path)
            except KeyError:
                pass
            if step.when.in_ is not None:
                matched = val in step.when.in_
            elif step.when.equals is not None:
                matched = val == step.when.equals
            else:
                matched = bool(val)
            if matched:
                for name in (str(val), "true", "match"):
                    if name in step.branches:
                        return step.branches[name]
            return step.branches.get("default")
        idx = order.index(step.id)
        if idx + 1 < len(order):
            return order[idx + 1]
        return None

    async def _record_step_success(self, run_id, step, out, resolved, log,
                                   attempt, duration) -> None:
        async with self.sf() as s2:
            prior = await s2.scalar(select(StepRecord).where(
                StepRecord.run_id == run_id, StepRecord.step_id == step.id))
            if prior is not None:
                await s2.delete(prior)
            s2.add(StepRecord(
                run_id=run_id, step_id=step.id, tool=step.type,
                status=StepStatus.SUCCEEDED.value, attempts=attempt,
                input=redact(resolved), output=redact(out),
                attempt_log=log or None,
                provider_meta=redact(out.get("meta")) if isinstance(out, dict) else None,
                started_at=utcnow(), finished_at=utcnow(), duration_ms=duration))
            await s2.commit()

    async def _record_step_failure(self, run_id, step, err, attempts, *,
                                   input_=None, log=None,
                                   status=StepStatus.FAILED.value) -> None:
        async with self.sf() as s2:
            prior = await s2.scalar(select(StepRecord).where(
                StepRecord.run_id == run_id, StepRecord.step_id == step.id))
            if prior is not None:
                await s2.delete(prior)
            s2.add(StepRecord(
                run_id=run_id, step_id=step.id, tool=step.type,
                status=status, attempts=attempts, input=input_,
                error=redact(err.to_dict()),
                failure_class=err.failure_class.value,
                attempt_log=log or None,
                started_at=utcnow(), finished_at=utcnow()))
            await s2.commit()

    async def _finish(self, s: AsyncSession, run_id: str, status: RunStatus,
                      *, error: dict | None = None,
                      context: dict | None = None) -> None:
        run = await s.get(RunRecord, run_id)
        run.status = status.value
        run.finished_at = utcnow()
        run.current_step = None
        if error is not None:
            run.error = redact(error)
        if context is not None:
            run.outputs = redact({k: v for k, v in context.items() if k != "run"})
        await s.commit()

    async def _build_context(self, run_id: str, wf: Workflow) -> dict:
        async with self.sf() as s:
            run = await s.get(RunRecord, run_id)
            ctx: dict[str, Any] = {"run": {"inputs": dict(run.inputs or {})}}
            for sr in run.steps:
                if sr.status == StepStatus.SUCCEEDED.value and sr.output is not None:
                    step = next((x for x in wf.steps if x.id == sr.step_id), None)
                    if step is None:
                        continue
                    if step.output_to:
                        self._set_path(ctx, step.output_to, sr.output)
                    else:
                        ctx[sr.step_id] = sr.output
            return ctx
