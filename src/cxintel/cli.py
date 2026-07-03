"""Command-line interface for the Conversation Intelligence Platform.

Phase 1 wires up the full command surface. ``version``, ``db health`` and
``serve`` are live; the pipeline-stage commands are honest stubs that will be
implemented in later phases (they exit non-zero so scripts don't mistake a
placeholder for a completed run). Installed as the ``app`` console script.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import typer

from . import __version__
from .logging import configure_logging

app = typer.Typer(
    help="Conversation Intelligence Platform CLI.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="Database utilities.", no_args_is_help=True)
app.add_typer(db_app, name="db")


def _not_implemented(stage_key: str) -> None:
    """Report a not-yet-implemented stage, sourced from the pipeline registry."""
    from .pipeline.orchestrator import get_stage

    stage = get_stage(stage_key)
    phase = stage.planned_phase or "a later phase"
    typer.secho(
        f"'{stage.label}' is not implemented yet (planned for {phase}).",
        fg=typer.colors.YELLOW,
    )
    raise typer.Exit(code=1)


@app.callback()
def _root() -> None:
    """Root callback — configure logging before any command runs."""
    configure_logging()


@app.command()
def version() -> None:
    """Print the application version."""
    typer.echo(__version__)


@db_app.command("health")
def db_health() -> None:
    """Check database connectivity and pgvector availability."""
    from .db import check_health

    health = check_health()
    if not health.connected:
        typer.secho(f"database: unreachable ({health.error})", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    typer.secho(f"database: connected (server {health.server_version})", fg=typer.colors.GREEN)
    if health.pgvector_installed:
        typer.secho("pgvector: installed", fg=typer.colors.GREEN)
        raise typer.Exit(code=0)
    typer.secho("pgvector: MISSING", fg=typer.colors.RED)
    raise typer.Exit(code=1)


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply database migrations (alembic upgrade head)."""
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    typer.secho("migrations: up to date", fg=typer.colors.GREEN)


@app.command()
def ingest() -> None:
    """Import the raw ticket dataset into PostgreSQL (idempotent)."""
    from .pipeline.orchestrator import run_stage

    try:
        summary = run_stage("ingest", progress=typer.echo, trigger="cli")
    except Exception as exc:
        typer.secho(f"ingestion failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.secho(summary, fg=typer.colors.GREEN)


@app.command()
def stats() -> None:
    """Report ingestion statistics — verifies the import completed."""
    from .db import get_session_factory
    from .repositories import ConversationRepository, MessageRepository

    try:
        with get_session_factory()() as session:
            conversations = ConversationRepository(session)
            total = conversations.count()
            by_status = conversations.count_by_status()
            date_range = conversations.date_range()
            messages = MessageRepository(session).count()
    except Exception as exc:
        typer.secho(f"stats unavailable: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    if total == 0:
        typer.secho("No conversations found — run 'make ingest' first.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    typer.echo(f"Total conversations : {total}")
    typer.echo(f"Total messages      : {messages}")
    for status in ("resolved", "open", "pending", "escalated"):
        typer.echo(f"  {status:<18}: {by_status.get(status, 0)}")
    if date_range is not None:
        typer.echo(f"Dataset date range  : {date_range[0].date()} → {date_range[1].date()}")


@app.command()
def understand(
    full: bool = typer.Option(
        False, "--full", help="Process the full dataset (default: sample of 100)."
    ),
) -> None:
    """Run LLM conversation understanding (resumable; skips analyzed conversations)."""
    from .pipeline.orchestrator import run_stage

    try:
        summary = run_stage(
            "understand",
            progress=typer.echo,
            trigger="cli",
            option="full" if full else "sample",
        )
    except Exception as exc:
        typer.secho(f"understanding failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.secho(summary, fg=typer.colors.GREEN)


@app.command()
def analyze() -> None:
    """Detect operational anomalies vs the Day-1 baseline (deterministic)."""
    from .pipeline.orchestrator import run_stage

    try:
        summary = run_stage("anomaly", progress=typer.echo, trigger="cli")
    except Exception as exc:
        typer.secho(f"anomaly detection failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.secho(summary, fg=typer.colors.GREEN)


@app.command()
def report() -> None:
    """Print the anomaly report (generated from persisted anomalies)."""
    from .anomaly.reporting import render_report
    from .db import get_session_factory
    from .repositories import AnomalyRepository

    try:
        with get_session_factory()() as session:
            anomalies = AnomalyRepository(session).all()
    except Exception as exc:
        typer.secho(f"report unavailable: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    if not anomalies:
        typer.secho("No anomalies recorded — run 'app analyze' first.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    typer.echo(render_report(anomalies))


@app.command("build-kb")
def build_kb() -> None:
    """Embed resolved conversations into the knowledge base (Phase 5)."""
    _not_implemented("knowledge_base")


@app.command()
def chat() -> None:
    """Interactive Resolution Assistant (Phase 6)."""
    _not_implemented("resolution_assistant")


@app.command()
def pipeline() -> None:
    """Run every incomplete pipeline stage in dependency order."""
    from .pipeline.orchestrator import run_remaining

    try:
        summary = run_remaining(progress=typer.echo, trigger="cli")
    except Exception as exc:
        typer.secho(f"pipeline failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.secho(summary, fg=typer.colors.GREEN)


@app.command()
def runs(limit: int = typer.Option(20, help="Maximum number of runs to show.")) -> None:
    """Show the pipeline audit trail — recent stage runs, newest first."""
    from .db import check_health
    from .pipeline.orchestrator import recent_runs

    health = check_health()
    if not health.connected:
        typer.secho(f"run history unavailable: database unreachable ({health.error})", fg="red")
        raise typer.Exit(code=1)

    records = recent_runs(limit=limit)
    if not records:
        typer.secho("No pipeline runs recorded yet.", fg=typer.colors.YELLOW)
        return

    for run in records:
        started = run.started_at.strftime("%Y-%m-%d %H:%M:%S")
        duration = f"{run.duration_seconds:.1f}s" if run.duration_seconds is not None else "—"
        detail = run.error if run.status == "failed" else (run.summary or "")
        color = {"succeeded": typer.colors.GREEN, "failed": typer.colors.RED}.get(
            run.status, typer.colors.YELLOW
        )
        typer.secho(
            f"{started}  {run.stage_key:<20} {run.status:<9} {run.trigger:<4} "
            f"{duration:>7}  {detail}",
            fg=color,
        )


@app.command()
def bottlenecks(
    limit: int = typer.Option(20, help="Maximum number of observations to show."),
    sort: str = typer.Option(
        "total_seconds",
        help=(
            "Sort by total_seconds, llm_seconds, load_seconds, prompt_seconds, "
            "persist_seconds, retry_count, or started_at."
        ),
    ),
    pipeline_run_id: Annotated[
        uuid.UUID | None, typer.Option(help="Restrict observations to one pipeline run id.")
    ] = None,
) -> None:
    """Show slow per-conversation LLM observations for bottleneck diagnosis."""
    from .db import check_health
    from .pipeline.orchestrator import llm_observations

    health = check_health()
    if not health.connected:
        typer.secho(f"bottlenecks unavailable: database unreachable ({health.error})", fg="red")
        raise typer.Exit(code=1)

    try:
        records = llm_observations(limit=limit, sort=sort, pipeline_run_id=pipeline_run_id)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    if not records:
        typer.secho("No LLM observations recorded yet.", fg=typer.colors.YELLOW)
        return

    typer.echo(
        "conversation              status      total     llm    load  prompt persist "
        "retry msgs prompt"
    )
    for record in records:
        label = record.conversation_external_id or str(record.conversation_id)
        error = f"  error={record.error[:80]}" if record.error else ""
        typer.echo(
            f"{label:<24} {record.status:<9} "
            f"{record.total_seconds:>6.2f}s {record.llm_seconds:>6.2f}s "
            f"{record.load_seconds:>6.2f}s {record.prompt_seconds:>6.2f}s "
            f"{record.persist_seconds:>6.2f}s {record.retry_count:>5} "
            f"{record.message_count:>4} {record.prompt_characters:>6}{error}"
        )


@app.command()
def serve() -> None:
    """Run the FastAPI service."""
    import uvicorn

    from .config import get_settings

    settings = get_settings()
    uvicorn.run("cxintel.api.app:app", host=settings.api_host, port=settings.api_port)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
