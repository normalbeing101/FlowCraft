"""FlowCraft CLI — same engine as the HTTP API, no web server required.

Run with: python -m flowcraft <cmd>  (or `flowcraft` after `pip install -e .`)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

cli = typer.Typer(help="FlowCraft workflow engine CLI", no_args_is_help=True)
console = Console()


def _settings():
    from ..config import get_settings

    return get_settings()


async def _engine():
    from ..engine.executor import Engine
    from ..persistence.store import init_schema, make_store

    st = _settings()
    engine_obj, sf = await make_store(st.database_url)
    await init_schema(engine_obj)
    return engine_obj, Engine(sf, st)


def _run(coro):
    return asyncio.run(coro)


def _serialize(r) -> str:
    d = {"run_id": r.run_id, "workflow": r.workflow_id, "status": r.status,
         "steps": [{"step": s.step_id, "tool": s.tool, "status": s.status,
                    "attempts": s.attempts, "output": s.output,
                    "error": s.error, "failure_class": s.failure_class}
                   for s in r.steps],
         "outputs": r.outputs, "error": r.error}
    return json.dumps(d, indent=2, default=str)


@cli.command("validate")
def validate(path: Path = typer.Argument(..., help="Workflow YAML/JSON file")):
    """Validate a workflow definition file."""
    async def go():
        _, eng = await _engine()
        try:
            wf = await eng.validate_definition(path.read_text())
        except Exception as e:
            console.print(f"[red]INVALID:[/] {e}")
            raise typer.Exit(1)
        console.print(f"[green]VALID:[/] {wf.id} v{wf.metadata.version} "
                      f"({len(wf.steps)} steps)")
    _run(go())


@cli.command("import")
def import_wf(path: Path = typer.Argument(...)):
    """Import (upsert) a workflow into the database."""
    async def go():
        engine_obj, eng = await _engine()
        try:
            rec = await eng.import_workflow(path.read_text(),
                                            workspace_id=_settings().workspace_id)
            console.print(f"[green]Imported[/] {rec.workflow_id} v{rec.version}")
        finally:
            await engine_obj.dispose()
    _run(go())


@cli.command("list")
def list_wfs():
    """List stored workflows."""
    async def go():
        from sqlalchemy import select

        from ..persistence.models import WorkflowRecord

        engine_obj, eng = await _engine()
        try:
            async with eng.sf() as s:
                rows = (await s.scalars(select(WorkflowRecord).where(
                    WorkflowRecord.workspace_id == _settings().workspace_id))).all()
            t = Table("workflow", "version", "name", "tags")
            for r in rows:
                t.add_row(r.workflow_id, str(r.version),
                          r.definition["metadata"]["name"],
                          ",".join(r.definition["metadata"].get("tags", [])))
            console.print(t if rows else "[dim]no workflows imported yet[/]")
        finally:
            await engine_obj.dispose()
    _run(go())


@cli.command("export")
def export(workflow_id: str, out: Path = typer.Argument(Path(".")),
           version: int | None = None):
    """Export a stored workflow back to a YAML file."""
    async def go():
        import yaml
        from sqlalchemy import select

        from ..persistence.models import WorkflowRecord

        engine_obj, eng = await _engine()
        try:
            async with eng.sf() as s:
                q = select(WorkflowRecord).where(
                    WorkflowRecord.workspace_id == _settings().workspace_id,
                    WorkflowRecord.workflow_id == workflow_id)
                if version:
                    q = q.where(WorkflowRecord.version == version)
                rec = await s.scalar(q.order_by(WorkflowRecord.version.desc()).limit(1))
                if rec is None:
                    console.print("[red]not found[/]")
                    raise typer.Exit(1)
                dest = out if out.suffix else out / f"{workflow_id}-v{rec.version}.yaml"
                dest.write_text(yaml.safe_dump(rec.definition, sort_keys=False))
                console.print(f"[green]Exported to[/] {dest}")
        finally:
            await engine_obj.dispose()
    _run(go())


@cli.command("run")
def run(workflow_id: str,
        input_json: str = typer.Option("{}", "--input", "-i",
                                       help="JSON object of run inputs"),
        idem_key: str = typer.Option(None, "--idempotency-key")):
    """Execute a workflow and print the resulting run record."""
    async def go():
        engine_obj, eng = await _engine()
        try:
            inputs = json.loads(input_json)
            run_rec = await eng.execute(workflow_id,
                                        workspace_id=_settings().workspace_id,
                                        inputs=inputs, idempotency_key=idem_key)
            console.print(_serialize(run_rec))
            color = {"succeeded": "green", "failed": "red",
                     "rejected": "yellow", "awaiting_approval": "cyan"}.get(
                         run_rec.status, "white")
            console.print(f"[{color}]status: {run_rec.status}[/{color}]")
            if run_rec.status == "failed":
                raise typer.Exit(2)
        finally:
            await engine_obj.dispose()
    _run(go())


@cli.command("runs")
def runs(limit: int = 20, status: str | None = None):
    """List recent runs."""
    async def go():
        from sqlalchemy import select

        from ..persistence.models import RunRecord

        engine_obj, eng = await _engine()
        try:
            async with eng.sf() as s:
                q = select(RunRecord).where(
                    RunRecord.workspace_id == _settings().workspace_id)
                if status:
                    q = q.where(RunRecord.status == status)
                rows = (await s.scalars(q.order_by(RunRecord.started_at.desc())
                                        .limit(limit))).all()
            t = Table("run_id", "workflow", "status", "started")
            for r in rows:
                t.add_row(r.run_id[:8], r.workflow_id, r.status,
                          r.started_at.strftime("%H:%M:%S") if r.started_at else "-")
            console.print(t if rows else "[dim]no runs yet[/]")
        finally:
            await engine_obj.dispose()
    _run(go())


@cli.command("inspect")
def inspect(run_id: str):
    """Show full step-by-step detail for one run."""
    async def go():
        from ..persistence.models import RunRecord

        engine_obj, eng = await _engine()
        try:
            async with eng.sf() as s:
                r = await s.get(RunRecord, run_id)
            if r is None or r.workspace_id != _settings().workspace_id:
                console.print("[red]run not found in workspace[/]")
                raise typer.Exit(1)
            console.print(_serialize(r))
        finally:
            await engine_obj.dispose()
    _run(go())


@cli.command("approve")
def approve(run_id: str, decided_by: str = typer.Option(..., "--by"),
            comment: str = "",
            reject: bool = typer.Option(False, "--reject",
                                       help="Reject instead of approve")):
    """Decide a pending human-approval gate and resume/reject the run."""
    async def go():
        engine_obj, eng = await _engine()
        try:
            r = await eng.decide_approval(run_id,
                                          workspace_id=_settings().workspace_id,
                                          approved=not reject,
                                          decided_by=decided_by, comment=comment)
            color = "green" if r.status == "succeeded" else "yellow"
            console.print(f"[{color}]status: {r.status}[/{color}]")
        finally:
            await engine_obj.dispose()
    _run(go())


@cli.command("cancel")
def cancel(run_id: str):
    """Request cancellation of a run."""
    async def go():
        from ..engine.executor import request_cancel as rc
        from ..persistence.models import RunRecord, utcnow

        engine_obj, eng = await _engine()
        try:
            live = rc(run_id)
            if not live:
                async with eng.sf() as s:
                    r = await s.get(RunRecord, run_id)
                    if r is None or r.workspace_id != _settings().workspace_id:
                        console.print("[red]run not found[/]")
                        raise typer.Exit(1)
                    r.status = "cancelled"
                    r.finished_at = utcnow()
                    await s.commit()
            console.print("cancellation applied")
        finally:
            await engine_obj.dispose()
    _run(go())


def main():
    cli()


if __name__ == "__main__":
    main()
