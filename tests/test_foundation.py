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
    assert Settings.model_fields["llm_model"].default == "claude-opus-4-8"
    assert str(Settings.model_fields["database_url"].default).startswith("postgresql")


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_stub_command_exits_nonzero() -> None:
    result = runner.invoke(app, ["ingest"])
    assert result.exit_code == 1
