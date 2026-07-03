"""Tests for the in-UI GOOGLE_API_KEY onboarding flow.

Covers the ``.env`` writer helper and the ``POST /api/config/google-key``
endpoint that the landing page's "Enable AI Capabilities" card calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cxintel.api.app import app
from cxintel.config import get_settings, set_env_key


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Run each test in a temp CWD with no key set and a fresh settings cache.

    ``monkeypatch`` snapshots the environment, so a key the endpoint sets via
    ``os.environ`` is removed again on teardown.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- set_env_key ----------------------------------------------------------


def test_set_env_key_creates_file_when_missing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    set_env_key("GOOGLE_API_KEY", "abc123", env_file=env_file)
    assert env_file.read_text(encoding="utf-8") == "GOOGLE_API_KEY=abc123\n"


def test_set_env_key_replaces_existing_line(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LOG_LEVEL=DEBUG\nGOOGLE_API_KEY=old\nBATCH_SIZE=5\n")
    set_env_key("GOOGLE_API_KEY", "new-key", env_file=env_file)
    assert env_file.read_text() == "LOG_LEVEL=DEBUG\nGOOGLE_API_KEY=new-key\nBATCH_SIZE=5\n"


def test_set_env_key_replaces_commented_line(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# GOOGLE_API_KEY=your-key-here\nLOG_LEVEL=INFO\n")
    set_env_key("GOOGLE_API_KEY", "real-key", env_file=env_file)
    assert env_file.read_text() == "GOOGLE_API_KEY=real-key\nLOG_LEVEL=INFO\n"


def test_set_env_key_appends_when_absent(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LOG_LEVEL=INFO\n")
    set_env_key("GOOGLE_API_KEY", "abc", env_file=env_file)
    assert env_file.read_text() == "LOG_LEVEL=INFO\nGOOGLE_API_KEY=abc\n"


def test_set_env_key_does_not_touch_other_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# MY_GOOGLE_API_KEY_NOTE=keep\nOTHER=1\n")
    set_env_key("GOOGLE_API_KEY", "abc", env_file=env_file)
    content = env_file.read_text()
    assert "# MY_GOOGLE_API_KEY_NOTE=keep\n" in content
    assert "OTHER=1\n" in content
    assert "GOOGLE_API_KEY=abc\n" in content


# --- POST /api/config/google-key ------------------------------------------


def test_save_key_writes_env_and_enables_ai(tmp_path: Path) -> None:
    client = TestClient(app)
    assert client.get("/api/status").json()["ai"]["configured"] is False

    response = client.post("/api/config/google-key", json={"api_key": "test-key-123"})
    assert response.status_code == 200
    payload = response.json()
    assert payload == {"saved": True, "ai_configured": True}
    # The key itself must never appear in the response.
    assert "test-key-123" not in response.text

    # Persisted to the local .env (CWD is tmp_path via fixture).
    assert "GOOGLE_API_KEY=test-key-123" in Path(".env").read_text(encoding="utf-8")

    # Live immediately — no restart needed.
    assert client.get("/api/status").json()["ai"]["configured"] is True
    assert client.get("/api/config").json()["google_api_key_set"] is True


def test_save_key_strips_surrounding_whitespace(tmp_path: Path) -> None:
    client = TestClient(app)
    response = client.post("/api/config/google-key", json={"api_key": "  abc123  "})
    assert response.status_code == 200
    assert "GOOGLE_API_KEY=abc123\n" in Path(".env").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "bad_key",
    ["", "   ", "abc\ndef", "abc def", "abc\tdef", "li\rne"],
)
def test_save_key_rejects_invalid_keys(bad_key: str) -> None:
    client = TestClient(app)
    response = client.post("/api/config/google-key", json={"api_key": bad_key})
    assert response.status_code == 422
    assert not Path(".env").exists()
