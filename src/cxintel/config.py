"""Application configuration.

A single, typed settings object loaded from environment variables and an
optional ``.env`` file. Every stage of the pipeline reads configuration through
:func:`get_settings`, so behaviour is consistent and overridable without code
changes. Field names map to upper-case environment variables (for example
``database_url`` reads ``DATABASE_URL``).
"""

from __future__ import annotations

from functools import lru_cache

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

    # --- Anomaly alerts — used from Phase 4 ------------------------------
    slack_webhook_url: str | None = Field(default=None)

    # --- Pipeline knobs ---------------------------------------------------
    raw_data_path: str = Field(default="data/raw/sample_tickets_v6.json")
    understand_limit: int | None = Field(
        default=None, description="Cap conversations processed by `understand` (None = all)."
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
            return self.google_api_key is not None
        return False  # other providers add their own key check when introduced


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
