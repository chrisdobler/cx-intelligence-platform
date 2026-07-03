"""Application configuration.

A single, typed settings object loaded from environment variables and an
optional ``.env`` file. Every stage of the pipeline reads configuration through
:func:`get_settings`, so behaviour is consistent and overridable without code
changes. Field names map to upper-case environment variables (for example
``database_url`` reads ``DATABASE_URL``).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings, sourced from the environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database ---------------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg://cx:cx@localhost:5432/cx",
        description="SQLAlchemy URL for the PostgreSQL + pgvector store.",
    )

    # --- LLM (Google AI Studio) — used from Phase 3 ----------------------
    llm_provider: str = Field(default="google", description="LLM provider (pluggable).")
    google_api_key: str | None = Field(default=None)
    llm_model: str = Field(default="gemini-2.5-flash")

    # --- Embeddings — used from Phase 5 ----------------------------------
    embedding_provider: str = Field(
        default="google", description="google | local | voyage | openai"
    )
    embedding_model: str = Field(default="gemini-embedding-001")
    embedding_dim: int = Field(default=3072)

    # --- Anomaly detection — Phase 4 --------------------------------------
    slack_webhook_url: str | None = Field(default=None)
    anomaly_spike_threshold_pct: float = Field(
        default=50.0, description="Min % volume increase vs the Day 1 baseline for a spike."
    )
    anomaly_drift_threshold: float = Field(
        default=0.15, description="Min absolute share change for severity/resolution drift."
    )
    anomaly_min_count: int = Field(
        default=5, description="Min current-day occurrences for spike/drift signals."
    )
    anomaly_report_path: str = Field(
        default="reports/anomaly-report.md",
        description="Where the generated anomaly report is written.",
    )

    # --- Pipeline knobs ---------------------------------------------------
    raw_data_path: str = Field(default="data/raw/sample_tickets_v6.json")
    understand_limit: int | None = Field(
        default=None, description="Cap conversations processed by `understand` (None = all)."
    )
    understand_sample_size: int = Field(
        default=100, description="Conversations processed by the 'Run Sample' action."
    )
    understand_concurrency: int = Field(
        default=8, description="Worker threads for conversation understanding (per day)."
    )
    batch_size: int = Field(default=100)

    # --- Runtime ----------------------------------------------------------
    log_level: str = Field(default="INFO")
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000)

    @property
    def ai_configured(self) -> bool:
        """Whether the active LLM provider has the credentials it needs."""
        if self.llm_provider == "google":
            # Truthiness (not just "is set"): an empty env var means unconfigured.
            return bool(self.google_api_key)
        return False  # other providers add their own key check when introduced


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def set_env_key(name: str, value: str, env_file: Path = Path(".env")) -> None:
    """Persist ``name=value`` to the local ``.env`` file.

    Replaces the first existing line for ``name`` (including a commented-out
    ``# NAME=...`` placeholder) and preserves everything else; appends when the
    key is absent. Creates the file if it does not exist. Callers should clear
    the :func:`get_settings` cache afterwards for the change to take effect.
    """
    pattern = re.compile(rf"^\s*#?\s*{re.escape(name)}\s*=")
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    new_line = f"{name}={value}"
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
