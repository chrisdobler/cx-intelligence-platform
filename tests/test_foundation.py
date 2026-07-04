"""Foundation smoke tests — no database required."""

from __future__ import annotations

import pytest
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
    assert Settings.model_fields["understand_concurrency"].default == 32
    assert str(Settings.model_fields["database_url"].default).startswith("postgresql")


def test_understand_concurrency_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERSTAND_CONCURRENCY", "7")
    get_settings.cache_clear()
    try:
        assert get_settings().understand_concurrency == 7
    finally:
        get_settings.cache_clear()


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_chat_without_ai_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    # chat needs a configured AI provider; without one it exits 1 before
    # touching the DB or any API. (understand/build-kb are live and must not
    # be invoked here — they would run real AI calls against the dev database.)
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    get_settings.cache_clear()
    try:
        result = runner.invoke(app, ["chat", "my pod is leaking"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 1
    assert "resolution assistant unavailable" in result.output
