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
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from requests import exceptions as requests_exceptions

if TYPE_CHECKING:
    from google.genai import Client

T = TypeVar("T", bound=BaseModel)
RetryCallback = Callable[[int, Exception], None]

_MAX_ATTEMPTS = 3  # malformed / validation-failed responses
_MAX_TRANSIENT_ATTEMPTS = 5  # rate limits and 5xx — cheap to wait out
_TRANSIENT_CODES = {429, 500, 503}


class LLMExtractionError(Exception):
    """Raised when a provider cannot produce a valid structured response."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        category: str = "permanent",
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.category = category
        self.attempts = attempts


class LLMFailureCategory(StrEnum):
    """Stable categories callers can use for persistence/resume decisions."""

    VALIDATION = "validation"
    PERMANENT_API = "permanent_api"
    TRANSIENT_API = "transient_api"
    TRANSPORT = "transport"
    CONFIGURATION = "configuration"


class PermanentLLMExtractionError(LLMExtractionError):
    """Raised for failures normal reruns should not keep retrying."""

    def __init__(
        self,
        message: str,
        *,
        category: LLMFailureCategory | str = LLMFailureCategory.PERMANENT_API,
        attempts: int = 0,
    ) -> None:
        super().__init__(
            message,
            retryable=False,
            category=str(category),
            attempts=attempts,
        )


class RetryableLLMExtractionError(LLMExtractionError):
    """Raised after a bounded transient retry budget is exhausted."""

    def __init__(
        self,
        message: str,
        *,
        category: LLMFailureCategory | str = LLMFailureCategory.TRANSIENT_API,
        attempts: int = 0,
    ) -> None:
        super().__init__(
            message,
            retryable=True,
            category=str(category),
            attempts=attempts,
        )


class LLMProvider(Protocol):
    """The single capability the platform needs from an LLM vendor."""

    def extract(
        self, prompt: str, schema: type[T], on_retry: RetryCallback | None = None
    ) -> T:
        """Run the prompt and return a validated instance of ``schema``."""
        ...


_RETRY_DELAY_PATTERN = re.compile(r"retry in ([0-9.]+)s", re.IGNORECASE)
_MAX_RETRY_SLEEP = 65.0


def _suggested_delay(error: Exception, cap: float = _MAX_RETRY_SLEEP) -> float | None:
    """The server's 'Please retry in Xs' hint from a rate-limit error, if any."""
    match = _RETRY_DELAY_PATTERN.search(str(error))
    return min(float(match.group(1)), cap) if match else None


def _is_transport_error(error: Exception) -> bool:
    """SDK transport failures that are safe to retry with bounded backoff."""
    return isinstance(
        error,
        (
            httpx.TransportError,
            requests_exceptions.RequestException,
        ),
    )


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
    backoff. Transport errors from the SDK's HTTP layer share that transient
    budget.
    """

    def __init__(
        self,
        client: Client,
        model: str,
        backoff_seconds: float = 2.0,
        max_transient_attempts: int = _MAX_TRANSIENT_ATTEMPTS,
        max_retry_sleep: float = _MAX_RETRY_SLEEP,
    ) -> None:
        self._client = client
        self._model = model
        self._backoff_seconds = backoff_seconds
        # Callers with a deterministic fallback (e.g. Prompt-3 Slack alerts)
        # shrink these so a quota outage degrades in seconds, not minutes.
        self._max_transient_attempts = max_transient_attempts
        self._max_retry_sleep = max_retry_sleep

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
        max_transient = self._max_transient_attempts
        while validation_failures < _MAX_ATTEMPTS and transient_failures < max_transient:
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
                    raise PermanentLLMExtractionError(
                        f"Google AI request failed: {exc}",
                        category=LLMFailureCategory.PERMANENT_API,
                        attempts=validation_failures + transient_failures + 1,
                    ) from exc
                last_error = exc
                transient_failures += 1
                if transient_failures < max_transient:
                    if on_retry is not None:
                        on_retry(transient_failures + 1, exc)
                    delay = _suggested_delay(exc, cap=self._max_retry_sleep)
                    if delay is None:
                        delay = self._backoff_seconds * (2 ** (transient_failures - 1))
                    time.sleep(delay)
            except Exception as exc:
                if not _is_transport_error(exc):
                    raise
                last_error = exc
                transient_failures += 1
                if transient_failures < max_transient:
                    if on_retry is not None:
                        on_retry(transient_failures + 1, exc)
                    time.sleep(self._backoff_seconds * (2 ** (transient_failures - 1)))
        attempts = validation_failures + transient_failures
        if transient_failures >= max_transient:
            category = (
                LLMFailureCategory.TRANSPORT
                if last_error is not None and _is_transport_error(last_error)
                else LLMFailureCategory.TRANSIENT_API
            )
            raise RetryableLLMExtractionError(
                f"No valid {schema.__name__} after {attempts} attempts: {last_error}",
                category=category,
                attempts=attempts,
            ) from last_error
        raise PermanentLLMExtractionError(
            f"No valid {schema.__name__} after {attempts} attempts: {last_error}",
            category=LLMFailureCategory.VALIDATION,
            attempts=attempts,
        ) from last_error


class EmbeddingProvider(Protocol):
    """The single embedding capability the platform needs (Phase 5)."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed retrieval documents; one vector per input text."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed one retrieval query."""
        ...


_EMBED_BATCH_SIZE = 100  # texts per embed_content request


class GoogleEmbeddingProvider:
    """Google AI Studio embeddings (gemini-embedding-001).

    Documents and queries use their matching task types so the model places
    them in the same retrieval space. Transient API errors (429/500/503) and
    transport failures share the same bounded retry budget as
    :class:`GoogleProvider`, honouring the server's suggested retry delay.
    """

    def __init__(
        self, client: Client, model: str, dimensions: int, backoff_seconds: float = 2.0
    ) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions
        self._backoff_seconds = backoff_seconds

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[start : start + _EMBED_BATCH_SIZE]
            vectors.extend(self._embed(batch, task_type="RETRIEVAL_DOCUMENT"))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], task_type="RETRIEVAL_QUERY")[0]

    def _embed(self, texts: list[str], *, task_type: str) -> list[list[float]]:
        from google.genai import errors, types

        config = types.EmbedContentConfig(
            task_type=task_type, output_dimensionality=self._dimensions
        )
        last_error: Exception | None = None
        transient_failures = 0
        while transient_failures < _MAX_TRANSIENT_ATTEMPTS:
            try:
                response = self._client.models.embed_content(
                    model=self._model, contents=texts, config=config
                )
                return [list(e.values or []) for e in response.embeddings or []]
            except errors.APIError as exc:
                if exc.code not in _TRANSIENT_CODES:
                    raise PermanentLLMExtractionError(
                        f"Google AI embedding request failed: {exc}",
                        category=LLMFailureCategory.PERMANENT_API,
                        attempts=transient_failures + 1,
                    ) from exc
                last_error = exc
                transient_failures += 1
                if transient_failures < _MAX_TRANSIENT_ATTEMPTS:
                    delay = _suggested_delay(exc)
                    if delay is None:
                        delay = self._backoff_seconds * (2 ** (transient_failures - 1))
                    time.sleep(delay)
            except Exception as exc:
                if not _is_transport_error(exc):
                    raise
                last_error = exc
                transient_failures += 1
                if transient_failures < _MAX_TRANSIENT_ATTEMPTS:
                    time.sleep(self._backoff_seconds * (2 ** (transient_failures - 1)))
        category = (
            LLMFailureCategory.TRANSPORT
            if last_error is not None and _is_transport_error(last_error)
            else LLMFailureCategory.TRANSIENT_API
        )
        raise RetryableLLMExtractionError(
            f"No embeddings after {transient_failures} attempts: {last_error}",
            category=category,
            attempts=transient_failures,
        ) from last_error


def get_embedding_provider() -> EmbeddingProvider:
    """The configured embedding provider (test seam — monkeypatch this in tests)."""
    from .config import get_settings

    settings = get_settings()
    if settings.embedding_provider != "google":
        raise PermanentLLMExtractionError(
            f"Unsupported embedding provider '{settings.embedding_provider}'.",
            category=LLMFailureCategory.CONFIGURATION,
        )
    if not settings.ai_configured:
        raise PermanentLLMExtractionError(
            "Google AI is not configured — set GOOGLE_API_KEY (see the AI Capabilities card).",
            category=LLMFailureCategory.CONFIGURATION,
        )
    from google import genai

    return GoogleEmbeddingProvider(
        client=genai.Client(api_key=settings.google_api_key),
        model=settings.embedding_model,
        dimensions=settings.embedding_dim,
    )


def get_llm_provider(
    *,
    max_transient_attempts: int = _MAX_TRANSIENT_ATTEMPTS,
    max_retry_sleep: float = _MAX_RETRY_SLEEP,
) -> LLMProvider:
    """The configured provider (test seam — monkeypatch this in tests).

    Callers whose output has a deterministic fallback can pass a small retry
    budget so quota outages degrade in seconds rather than minutes.
    """
    from .config import get_settings

    settings = get_settings()
    if settings.llm_provider != "google":
        raise PermanentLLMExtractionError(
            f"Unsupported LLM provider '{settings.llm_provider}'.",
            category=LLMFailureCategory.CONFIGURATION,
        )
    if not settings.ai_configured:
        raise PermanentLLMExtractionError(
            "Google AI is not configured — set GOOGLE_API_KEY (see the AI Capabilities card).",
            category=LLMFailureCategory.CONFIGURATION,
        )
    from google import genai

    return GoogleProvider(
        client=genai.Client(api_key=settings.google_api_key),
        model=settings.llm_model,
        max_transient_attempts=max_transient_attempts,
        max_retry_sleep=max_retry_sleep,
    )
