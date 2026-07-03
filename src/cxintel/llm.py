"""LLM provider abstraction (ADR-009).

The platform never talks to a vendor SDK directly: it asks a provider to
extract a Pydantic model from a prompt, and the provider translates its native
structured-output mechanism into that contract. Prompts carry semantics only —
the schema is supplied to the provider natively, never embedded in prompt
text. Nothing is returned until it validates; invalid data can therefore never
be persisted.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from google.genai import Client

T = TypeVar("T", bound=BaseModel)
RetryCallback = Callable[[int, Exception], None]

_MAX_ATTEMPTS = 3  # malformed / validation-failed responses
_MAX_TRANSIENT_ATTEMPTS = 5  # rate limits and 5xx — cheap to wait out
_TRANSIENT_CODES = {429, 500, 503}


class LLMExtractionError(Exception):
    """Raised when a provider cannot produce a valid structured response."""


class LLMProvider(Protocol):
    """The single capability the platform needs from an LLM vendor."""

    def extract(
        self, prompt: str, schema: type[T], on_retry: RetryCallback | None = None
    ) -> T:
        """Run the prompt and return a validated instance of ``schema``."""
        ...


_RETRY_DELAY_PATTERN = re.compile(r"retry in ([0-9.]+)s", re.IGNORECASE)
_MAX_RETRY_SLEEP = 65.0


def _suggested_delay(error: Exception) -> float | None:
    """The server's 'Please retry in Xs' hint from a rate-limit error, if any."""
    match = _RETRY_DELAY_PATTERN.search(str(error))
    return min(float(match.group(1)), _MAX_RETRY_SLEEP) if match else None


class GoogleProvider:
    """Google AI Studio (Gemini) provider using native structured output.

    The Pydantic model is passed directly as the SDK ``response_schema`` —
    Gemini constrains its own decoding to the schema (including enums) — and
    the raw response text is still validated with Pydantic before anything is
    returned. Invalid or malformed responses are retried up to
    ``_MAX_ATTEMPTS`` times. Transient API errors (429/500/503) get a larger
    budget (``_MAX_TRANSIENT_ATTEMPTS``) and honour the server's suggested
    retry delay when present (free-tier rate limits say 'retry in Xs' —
    sleeping less than that just burns attempts), falling back to exponential
    backoff.
    """

    def __init__(self, client: Client, model: str, backoff_seconds: float = 2.0) -> None:
        self._client = client
        self._model = model
        self._backoff_seconds = backoff_seconds

    def extract(
        self, prompt: str, schema: type[T], on_retry: RetryCallback | None = None
    ) -> T:
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0,
        )
        last_error: Exception | None = None
        validation_failures = 0
        transient_failures = 0
        while validation_failures < _MAX_ATTEMPTS and transient_failures < _MAX_TRANSIENT_ATTEMPTS:
            try:
                response = self._client.models.generate_content(
                    model=self._model, contents=prompt, config=config
                )
                return schema.model_validate_json(response.text or "")
            except ValidationError as exc:
                last_error = exc
                validation_failures += 1
                if validation_failures < _MAX_ATTEMPTS and on_retry is not None:
                    on_retry(validation_failures + 1, exc)
            except errors.APIError as exc:
                if exc.code not in _TRANSIENT_CODES:
                    raise LLMExtractionError(f"Google AI request failed: {exc}") from exc
                last_error = exc
                transient_failures += 1
                if transient_failures < _MAX_TRANSIENT_ATTEMPTS:
                    if on_retry is not None:
                        on_retry(transient_failures + 1, exc)
                    delay = _suggested_delay(exc)
                    if delay is None:
                        delay = self._backoff_seconds * (2 ** (transient_failures - 1))
                    time.sleep(delay)
        attempts = validation_failures + transient_failures
        raise LLMExtractionError(
            f"No valid {schema.__name__} after {attempts} attempts: {last_error}"
        ) from last_error


def get_llm_provider() -> LLMProvider:
    """The configured provider (test seam — monkeypatch this in tests)."""
    from .config import get_settings

    settings = get_settings()
    if settings.llm_provider != "google":
        raise LLMExtractionError(f"Unsupported LLM provider '{settings.llm_provider}'.")
    if not settings.ai_configured:
        raise LLMExtractionError(
            "Google AI is not configured — set GOOGLE_API_KEY (see the AI Capabilities card)."
        )
    from google import genai

    return GoogleProvider(
        client=genai.Client(api_key=settings.google_api_key), model=settings.llm_model
    )
