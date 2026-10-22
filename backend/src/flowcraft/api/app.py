"""FastAPI application — versioned HTTP API (/api/v1).

Auth: if FLOWCRAFT_API_AUTH_TOKEN is set, every request must present
`Authorization: Bearer <token>` (constant-time compare). When unset, the API
runs in single-user local mode and says so in /health. This is documented in
SECURITY.md as a deliberate MVP boundary (no user accounts yet; workspace
isolation is enforced at the data layer).
"""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..engine.executor import Engine, request_cancel
from ..errors import FlowcraftError, ValidationError
from ..persistence.models import RunRecord, WorkflowRecord
from ..persistence.store import init_schema, make_store
from ..tools.base import list_tools


class AppState:
    engine: Engine
    session_factory = None


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    engine_obj, sf = await make_store(settings.database_url)
    await init_schema(engine_obj)
    state.session_factory = sf
    state.engine = Engine(sf, settings)
    yield
    await engine_obj.dispose()


app = FastAPI(title="FlowCraft API", version="0.1.0", lifespan=lifespan)


def require_auth(authorization: str | None = Header(default=None)) -> None:
    token = get_settings().api_auth_token
    if not token:
        return  # local single-user mode (documented)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, {"error": {"code": "unauthorized",
                                            "message": "missing bearer token"}})
    given = authorization.split(" ", 1)[1]
    if not hmac.compare_digest(given, token):
        raise HTTPException(401, {"error": {"code": "unauthorized",
                                            "message": "invalid token"}})


def ws_id() -> str:
    return get_settings().workspace_id


@app.exception_handler(FlowcraftError)
async def flowcraft_error_handler(request: Request, exc: FlowcraftError):
    status = 400 if exc.failure_class.value in ("config", "permanent") else 500
    return _err(status, exc.code, exc.message, exc.details)


def _err(status: int, code: str, message: str, details: Any = None):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status, content={
        "error": {"code": code, "message": message,
                  "details": details or {}}})


# ---------- schemas ----------

class ImportReq(BaseModel):
    definition: str = Field(description="YAML or JSON workflow text")


class ExecuteReq(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=128)
    version: int | None = None


class ApprovalReq(BaseModel):
    approved: bool
    decided_by: str = Field(min_length=1, max_length=128)
    comment: str = Field(default="", max_length=2000)


# ---------- serialization ----------

def run_summary(r: RunRecord) -> dict:
    return {"run_id": r.run_id, "workflow_id": r.workflow_id,
            "workflow_version": r.workflow_version, "status": r.status,
            "current_step": r.current_step, "idempotency_key": r.idempotency_key,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None}


def run_detail(r: RunRecord) -> dict:
    d = run_summary(r)
    d.update({
        "inputs": r.inputs, "outputs": r.outputs, "error": r.error,
        "approval_payload": r.approval_payload,
        "steps": [{
            "step_id": s.step_id, "tool": s.tool, "status": s.status,
            "attempts": s.attempts, "input": s.input, "output": s.output,
            "error": s.error, "failure_class": s.failure_class,
            "attempt_log": s.attempt_log, "provider_meta": s.provider_meta,
            "duration_ms": s.duration_ms,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "finished_at": s.finished_at.isoformat() if s.finished_at else None,
        } for s in r.steps],
    })
    return d


# ---------- routes ----------

@app.get("/health")
async def health():
    s = get_settings()
    return {"status": "ok", "app": s.app_name, "version": app.version,
            "auth_enabled": bool(s.api_auth_token),
            "providers": sorted(state.engine.providers) if state.engine else [],
            "tools": sorted(list_tools())}


@app.post("/api/v1/workflows/validate", dependencies=[Depends(require_auth)])
async def validate_workflow(req: ImportReq):
    try:
        wf = await state.engine.validate_definition(req.definition)
    except ValidationError as e:
        return _err(422, "validation_error", e.message, e.details)
    return {"valid": True, "workflow_id": wf.id, "version": wf.metadata.version,
            "steps": len(wf.steps)}


@app.post("/api/v1/workflows", dependencies=[Depends(require_auth)])
async def import_workflow(req: ImportReq):
    try:
        rec = await state.engine.import_workflow(req.definition,
                                                 workspace_id=ws_id())
    except ValidationError as e:
        return _err(422, "validation_error", e.message, e.details)
    return {"workflow_id": rec.workflow_id, "version": rec.version,
            "imported": True}


@app.get("/api/v1/workflows", dependencies=[Depends(require_auth)])
async def list_workflows():
    async with state.session_factory() as s:
        rows = (await s.scalars(select(WorkflowRecord).where(
            WorkflowRecord.workspace_id == ws_id()))).all()
    return [{"workflow_id": r.workflow_id, "version": r.version,
             "name": r.definition["metadata"]["name"],
             "tags": r.definition["metadata"].get("tags", [])} for r in rows]


@app.get("/api/v1/workflows/{workflow_id}", dependencies=[Depends(require_auth)])
async def get_workflow(workflow_id: str, version: int | None = None):
    async with state.session_factory() as s:
        q = select(WorkflowRecord).where(WorkflowRecord.workspace_id == ws_id(),
                                         WorkflowRecord.workflow_id == workflow_id)
        if version:
            q = q.where(WorkflowRecord.version == version)
        rec = await s.scalar(q.order_by(WorkflowRecord.version.desc()).limit(1))
    if rec is None:
        return _err(404, "not_found", f"workflow {workflow_id!r} not found")
    return {"workflow_id": rec.workflow_id, "version": rec.version,
            "definition": rec.definition}


@app.delete("/api/v1/workflows/{workflow_id}", dependencies=[Depends(require_auth)])
async def delete_workflow(workflow_id: str):
    async with state.session_factory() as s:
        rows = (await s.scalars(select(WorkflowRecord).where(
            WorkflowRecord.workspace_id == ws_id(),
            WorkflowRecord.workflow_id == workflow_id))).all()
        for r in rows:
            await s.delete(r)
        await s.commit()
        if not rows:
            return _err(404, "not_found", f"workflow {workflow_id!r} not found")
    return {"deleted": len(rows)}


@app.post("/api/v1/workflows/{workflow_id}/execute",
          dependencies=[Depends(require_auth)])
async def execute_workflow(workflow_id: str, req: ExecuteReq):
    try:
        run = await state.engine.execute(workflow_id, workspace_id=ws_id(),
                                         inputs=req.inputs,
                                         idempotency_key=req.idempotency_key,
                                         version=req.version)
    except ValidationError as e:
        return _err(404 if "not found" in e.message else 422,
                    e.code, e.message, e.details)
    return run_detail(run)


@app.get("/api/v1/runs", dependencies=[Depends(require_auth)])
async def list_runs(limit: int = 50, offset: int = 0, status: str | None = None):
    limit = min(max(limit, 1), 200)
    async with state.session_factory() as s:
        q = select(RunRecord).where(RunRecord.workspace_id == ws_id())
        if status:
            q = q.where(RunRecord.status == status)
        rows = (await s.scalars(q.order_by(RunRecord.started_at.desc())
                                .limit(limit).offset(offset))).all()
    return [run_summary(r) for r in rows]


@app.get("/api/v1/runs/{run_id}", dependencies=[Depends(require_auth)])
async def get_run(run_id: str):
    async with state.session_factory() as s:
        run = await s.get(RunRecord, run_id)
    if run is None or run.workspace_id != ws_id():
        return _err(404, "not_found", "run not found")
    return run_detail(run)


@app.post("/api/v1/runs/{run_id}/cancel", dependencies=[Depends(require_auth)])
async def cancel_run(run_id: str):
    async with state.session_factory() as s:
        run = await s.get(RunRecord, run_id)
        if run is None or run.workspace_id != ws_id():
            return _err(404, "not_found", "run not found")
        if run.status in ("succeeded", "failed", "rejected", "cancelled",
                          "timed_out"):
            return _err(409, "already_finished", f"run is {run.status}")
    live = request_cancel(run_id)
    if not live:
        # Not running in this process: mark cancelled directly (safe because
        # no in-process task can still mutate it here).
        async with state.session_factory() as s:
            run = await s.get(RunRecord, run_id)
            run.status = "cancelled"
            from ..persistence.models import utcnow
            run.finished_at = utcnow()
            run.error = {"code": "cancelled", "class": "cancelled",
                         "message": "cancelled by operator"}
            await s.commit()
    return {"run_id": run_id, "cancellation_requested": True,
            "was_live_in_process": live}


@app.post("/api/v1/runs/{run_id}/approval", dependencies=[Depends(require_auth)])
async def decide_approval(run_id: str, req: ApprovalReq):
    try:
        run = await state.engine.decide_approval(
            run_id, workspace_id=ws_id(), approved=req.approved,
            decided_by=req.decided_by, comment=req.comment)
    except ValidationError as e:
        return _err(409, e.code, e.message, e.details)
    return run_detail(run)


@app.get("/api/v1/tools", dependencies=[Depends(require_auth)])
async def tools():
    return [{"name": t.spec.name, "description": t.spec.description,
             "permissions": t.spec.permissions,
             "idempotent": t.spec.idempotent,
             "supports_idempotency_key": t.spec.supports_idempotency_key,
             "side_effects": t.spec.side_effects,
             "input_schema": t.spec.input_schema,
             "output_schema": t.spec.output_schema,
             "config_schema": t.spec.config_schema}
            for t in list_tools().values()]
