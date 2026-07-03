"""Logging setup — one plain stdlib configuration used across the project."""

from __future__ import annotations

import logging

from .config import get_settings

_CONFIGURED = False


def configure_logging(level: str | None = None) -> None:
    """Configure root logging once. Idempotent."""
    global _CONFIGURED
    resolved = (level or get_settings().log_level).upper()
    logging.basicConfig(
        level=resolved,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger, configuring logging on first use."""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)
