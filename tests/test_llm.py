"""Tests for the LLM provider abstraction (no network — stubbed client)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import BaseModel, Field

from cxintel.llm import (
    GoogleProvider,
    LLMExtractionError,
    LLMFailureCategory,
    PermanentLLMExtractionError,
    RetryableLLMExtractionError,
    get_llm_provider,
)


class Extraction(BaseModel):
    """Minimal schema for provider tests."""

    name: str = Field(description="A name.")
    score: float = Field(ge=0.0, le=1.0, description="A score.")


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeModels:
    """Stub for google-genai's client.models, returning scripted responses."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, *, model: str, contents: str, config: Any) -> FakeResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


class FakeClient:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.models = FakeModels(responses)


def make_provider(responses: list[str | Exception]) -> tuple[GoogleProvider, FakeModels]:
    client = FakeClient(responses)
    provider = GoogleProvider(client=client, model="gemini-2.5-flash", backoff_seconds=0.0)  # type: ignore[arg-type]
    return provider, client.models


def test_extract_returns_validated_model_and_uses_native_schema() -> None:
    provider, models = make_provider(['{"name": "leak", "score": 0.9}'])
    result = provider.extract("prompt text", Extraction)
    assert result == Extraction(name="leak", score=0.9)

    call = models.calls[0]
    assert call["model"] == "gemini-2.5-flash"
    # Schema is supplied natively through the SDK config — never in the prompt.
    assert call["config"].response_schema is Extraction
    assert call["config"].response_mime_type == "application/json"
    assert "score" not in call["contents"]  # no schema text leaked into the prompt
    assert call["contents"] == "prompt text"


def test_extract_retries_invalid_response_then_succeeds() -> None:
    provider, models = make_provider(
        [
            "not json at all",
            '{"name": "leak", "score": 7.5}',  # valid JSON, fails validation (score > 1)
            '{"name": "leak", "score": 0.5}',
        ]
    )
    retries: list[int] = []
    result = provider.extract(
        "p", Extraction, on_retry=lambda attempt, _exc: retries.append(attempt)
    )
    assert result.score == 0.5
    assert len(models.calls) == 3
    assert retries == [2, 3]


def test_extract_raises_after_max_attempts() -> None:
    provider, models = make_provider(["nope", "nope", "nope"])
    with pytest.raises(LLMExtractionError) as excinfo:
        provider.extract("p", Extraction)
    assert len(models.calls) == 3
    assert "3 attempts" in str(excinfo.value)


def test_extract_retries_transient_api_errors() -> None:
    from google.genai import errors

    transient = errors.APIError(503, {"error": {"message": "overloaded"}})
    provider, models = make_provider([transient, '{"name": "ok", "score": 0.1}'])
    assert provider.extract("p", Extraction).name == "ok"
    assert len(models.calls) == 2


def test_extract_retries_transport_errors_then_succeeds() -> None:
    provider, models = make_provider(
        [httpx.TimeoutException("timeout"), '{"name": "ok", "score": 0.1}']
    )
    retries: list[int] = []
    assert provider.extract("p", Extraction, on_retry=lambda a, _e: retries.append(a)).name == "ok"
    assert len(models.calls) == 2
    assert retries == [2]


def test_rate_limit_honours_server_suggested_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """429s carry 'Please retry in Xs' — sleeping less just burns attempts."""
    from google.genai import errors

    sleeps: list[float] = []
    monkeypatch.setattr("cxintel.llm.time.sleep", lambda s: sleeps.append(s))

    rate_limited = errors.APIError(
        429,
        {
            "error": {
                "message": (
                    "You exceeded your current quota. Please retry in 16.248725877s."
                ),
                "status": "RESOURCE_EXHAUSTED",
            }
        },
    )
    provider, models = make_provider(
        [rate_limited, rate_limited, '{"name": "ok", "score": 0.1}']
    )
    assert provider.extract("p", Extraction).name == "ok"
    assert len(models.calls) == 3
    # Both sleeps honoured the server's suggestion instead of the tiny backoff.
    assert all(s >= 16.0 for s in sleeps)


def test_rate_limit_gets_more_attempts_than_validation_failures() -> None:
    """Transient quota errors retry longer than malformed-output errors."""
    from google.genai import errors

    rate_limited = errors.APIError(429, {"error": {"message": "quota. Please retry in 0.01s."}})
    responses: list[str | Exception] = [rate_limited, rate_limited, rate_limited, rate_limited]
    responses.append('{"name": "ok", "score": 0.1}')
    provider, models = make_provider(responses)
    assert provider.extract("p", Extraction).name == "ok"
    assert len(models.calls) == 5


def test_transient_exhaustion_is_retryable_failure() -> None:
    from google.genai import errors

    rate_limited = errors.APIError(429, {"error": {"message": "quota. Please retry in 0s."}})
    provider, models = make_provider([rate_limited] * 5)
    with pytest.raises(RetryableLLMExtractionError) as excinfo:
        provider.extract("p", Extraction)

    assert len(models.calls) == 5
    assert excinfo.value.retryable is True
    assert excinfo.value.category == LLMFailureCategory.TRANSIENT_API
    assert excinfo.value.attempts == 5


def test_transient_budget_is_configurable_for_fast_fallback() -> None:
    """Callers with a deterministic fallback (Prompt 3) use a small budget."""
    from google.genai import errors

    rate_limited = errors.APIError(429, {"error": {"message": "quota. Please retry in 0s."}})
    client = FakeClient([rate_limited] * 5)
    provider = GoogleProvider(
        client=client,  # type: ignore[arg-type]
        model="gemini-2.5-flash",
        backoff_seconds=0.0,
        max_transient_attempts=2,
    )
    with pytest.raises(RetryableLLMExtractionError):
        provider.extract("p", Extraction)
    assert len(client.models.calls) == 2


def test_retry_sleep_cap_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A small sleep cap overrides huge server-suggested delays."""
    from google.genai import errors

    sleeps: list[float] = []
    monkeypatch.setattr("cxintel.llm.time.sleep", lambda s: sleeps.append(s))
    rate_limited = errors.APIError(
        429, {"error": {"message": "quota. Please retry in 40.5s."}}
    )
    client = FakeClient([rate_limited, '{"name": "ok", "score": 0.1}'])
    provider = GoogleProvider(
        client=client,  # type: ignore[arg-type]
        model="gemini-2.5-flash",
        backoff_seconds=0.0,
        max_retry_sleep=8.0,
    )
    assert provider.extract("p", Extraction).name == "ok"
    assert sleeps == [8.0]


def test_non_transient_api_error_is_permanent_without_retry() -> None:
    from google.genai import errors

    bad_request = errors.APIError(400, {"error": {"message": "bad request"}})
    provider, models = make_provider([bad_request])
    with pytest.raises(PermanentLLMExtractionError) as excinfo:
        provider.extract("p", Extraction)

    assert len(models.calls) == 1
    assert excinfo.value.retryable is False
    assert excinfo.value.category == LLMFailureCategory.PERMANENT_API


def test_factory_requires_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from cxintel.config import get_settings

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.chdir("/")  # away from the repo's .env
    get_settings.cache_clear()
    try:
        with pytest.raises(LLMExtractionError):
            get_llm_provider()
    finally:
        get_settings.cache_clear()


# --- Phase 7 observability side-channel -------------------------------------


class _Usage:
    prompt_token_count = 90
    candidates_token_count = 10
    total_token_count = 100


class RichFakeResponse(FakeResponse):
    """A response carrying usage metadata and a response-level model version."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.usage_metadata = _Usage()
        self.model_version = "gemini-2.5-flash-042"


def test_extract_captures_usage_and_model_version() -> None:
    provider, models = make_provider([])
    rich = RichFakeResponse('{"name": "leak", "score": 0.9}')
    models.generate_content = lambda *, model, contents, config: rich  # type: ignore[method-assign]
    assert provider.last_usage is None
    assert provider.last_model_version is None

    provider.extract("p", Extraction)
    usage = provider.last_usage
    assert usage is not None
    assert (usage.prompt_tokens, usage.output_tokens, usage.total_tokens) == (90, 10, 100)
    assert provider.last_model_version == "gemini-2.5-flash-042"


def test_extract_without_usage_metadata_degrades_to_none_tokens() -> None:
    provider, _ = make_provider(['{"name": "leak", "score": 0.9}'])
    provider.extract("p", Extraction)
    usage = provider.last_usage
    assert usage is not None
    assert usage.prompt_tokens is None and usage.total_tokens is None
    # No response-level version → the configured model is the fallback.
    assert provider.last_model_version == "gemini-2.5-flash"
