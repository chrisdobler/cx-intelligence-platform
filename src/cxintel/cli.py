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


def _not_implemented(stage: str, phase: str) -> None:
    typer.secho(
        f"'{stage}' is not implemented yet (planned for {phase}).",
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
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from .config import get_settings
    from .db import get_session_factory
    from .ingestion.service import IngestionService

    settings = get_settings()
    path = Path(settings.raw_data_path)
    if not path.exists():
        typer.secho(f"dataset not found: {path}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    try:
        command.upgrade(Config("alembic.ini"), "head")
        with get_session_factory()() as session:
            result = IngestionService(session).ingest(path)
    except Exception as exc:
        typer.secho(f"ingestion failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    conv_skipped = result.conversations_seen - result.conversations_inserted
    msg_skipped = result.messages_seen - result.messages_inserted
    typer.secho(
        f"Ingested {result.conversations_seen} conversations ({conv_skipped} skipped), "
        f"{result.messages_seen} messages ({msg_skipped} skipped).",
        fg=typer.colors.GREEN,
    )


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
    _not_implemented("understand", "Phase 3")


@app.command()
def analyze() -> None:
    """Detect emerging issue clusters and emit Slack alerts (Phase 4)."""
    _not_implemented("analyze", "Phase 4")


@app.command("build-kb")
def build_kb() -> None:
    """Embed resolved conversations into the knowledge base (Phase 5)."""
    _not_implemented("build-kb", "Phase 5")


@app.command()
def chat() -> None:
    """Interactive Resolution Assistant (Phase 6)."""
    _not_implemented("chat", "Phase 6")


@app.command()
def pipeline() -> None:
    """Run the full ingest -> understand -> build-kb pipeline (Phase 8)."""
    _not_implemented("pipeline", "Phase 8")


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
