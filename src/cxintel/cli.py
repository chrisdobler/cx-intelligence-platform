"""Command-line interface for the Conversation Intelligence Platform.

Phase 1 wires up the full command surface. ``version``, ``db health`` and
``serve`` are live; the pipeline-stage commands are honest stubs that will be
implemented in later phases (they exit non-zero so scripts don't mistake a
placeholder for a completed run). Installed as the ``app`` console script.
"""

from __future__ import annotations

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
        summary = run_stage("ingest", progress=typer.echo)
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
def understand() -> None:
    """Run LLM conversation understanding (Phase 3)."""
    _not_implemented("understand")


@app.command()
def analyze() -> None:
    """Detect emerging issue clusters and emit Slack alerts (Phase 4)."""
    _not_implemented("anomaly")


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
        summary = run_remaining(progress=typer.echo)
    except Exception as exc:
        typer.secho(f"pipeline failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.secho(summary, fg=typer.colors.GREEN)


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
