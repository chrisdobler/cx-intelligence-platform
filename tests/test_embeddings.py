"""Tests for the embedding provider abstraction (no network — stubbed client)."""

from __future__ import annotations

from typing import Any

import pytest

from cxintel.llm import (
    GoogleEmbeddingProvider,
    LLMExtractionError,
    PermanentLLMExtractionError,
    RetryableLLMExtractionError,
    get_embedding_provider,
)


class FakeEmbedding:
    def __init__(self, values: list[float]) -> None:
        self.values = values


class FakeEmbedResponse:
    def __init__(self, embeddings: list[FakeEmbedding]) -> None:
        self.embeddings = embeddings


class FakeModels:
    """Stub for google-genai's client.models.embed_content."""

    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._errors = list(errors or [])

    def embed_content(self, *, model: str, contents: list[str], config: Any) -> FakeEmbedResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self._errors:
            raise self._errors.pop(0)
        return FakeEmbedResponse(
            [FakeEmbedding([float(len(text)), 0.5, 0.25]) for text in contents]
        )


class FakeClient:
    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.models = FakeModels(errors)


def make_provider(
    errors: list[Exception] | None = None,
) -> tuple[GoogleEmbeddingProvider, FakeModels]:
    client = FakeClient(errors)
    provider = GoogleEmbeddingProvider(
        client=client,  # type: ignore[arg-type]
        model="gemini-embedding-001",
        dimensions=3,
        backoff_seconds=0.0,
    )
    return provider, client.models


def test_embed_documents_returns_one_vector_per_text() -> None:
    provider, models = make_provider()
    vectors = provider.embed_documents(["short", "a longer text"])
    assert len(vectors) == 2
    assert vectors[0] == [5.0, 0.5, 0.25]
    assert vectors[1] == [13.0, 0.5, 0.25]
    call = models.calls[0]
    assert call["model"] == "gemini-embedding-001"
    assert call["config"].task_type == "RETRIEVAL_DOCUMENT"
    assert call["config"].output_dimensionality == 3


def test_embed_documents_batches_large_inputs() -> None:
    provider, models = make_provider()
    texts = [f"text {i}" for i in range(250)]
    vectors = provider.embed_documents(texts)
    assert len(vectors) == 250
    assert [len(c["contents"]) for c in models.calls] == [100, 100, 50]


def test_embed_documents_empty_input_makes_no_calls() -> None:
    provider, models = make_provider()
    assert provider.embed_documents([]) == []
    assert models.calls == []


def test_embed_query_uses_query_task_type() -> None:
    provider, models = make_provider()
    vector = provider.embed_query("how do I fix a leak?")
    assert len(vector) == 3
    assert models.calls[0]["config"].task_type == "RETRIEVAL_QUERY"


def test_transient_errors_are_retried() -> None:
    from google.genai import errors

    provider, models = make_provider([errors.APIError(429, {"error": {"message": "quota"}})])
    vectors = provider.embed_documents(["text"])
    assert len(vectors) == 1
    assert len(models.calls) == 2


def test_transient_exhaustion_raises_retryable() -> None:
    from google.genai import errors

    transient: list[Exception] = [
        errors.APIError(503, {"error": {"message": "overloaded"}}) for _ in range(5)
    ]
    provider, models = make_provider(transient)
    with pytest.raises(RetryableLLMExtractionError):
        provider.embed_documents(["text"])
    assert len(models.calls) == 5


def test_permanent_error_is_not_retried() -> None:
    from google.genai import errors

    provider, models = make_provider([errors.APIError(400, {"error": {"message": "bad"}})])
    with pytest.raises(PermanentLLMExtractionError):
        provider.embed_documents(["text"])
    assert len(models.calls) == 1


def test_factory_requires_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from cxintel.config import get_settings

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.chdir("/")  # away from the repo's .env
    get_settings.cache_clear()
    try:
        with pytest.raises(LLMExtractionError):
            get_embedding_provider()
    finally:
        get_settings.cache_clear()
