"""Foundation smoke tests — no database required."""

from __future__ import annotations

from typer.testing import CliRunner

from cxintel import __version__
from cxintel.cli import app
from cxintel.config import Settings, get_settings

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_settings_defaults() -> None:
    assert Settings.model_fields["llm_provider"].default == "google"
    assert Settings.model_fields["llm_model"].default == "gemini-2.5-flash"
    assert str(Settings.model_fields["database_url"].default).startswith("postgresql")


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_stub_command_exits_nonzero() -> None:
    # build-kb is the Phase 5 stub; it exits 1 without touching the DB or any
    # API. (understand is live as of Phase 3 and must not be invoked here —
    # it would run real LLM calls against the dev database.)
    result = runner.invoke(app, ["build-kb"])
    assert result.exit_code == 1
    assert "Phase 5" in result.output
