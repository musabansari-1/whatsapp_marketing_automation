"""Local embedding provider tests (specification §11, [D1]).

No real model is loaded here: a stub stands in for SentenceTransformer so the
provider's contract — dimension checking, error classification, truncation warning — is
tested without pulling PyTorch into the unit suite.
"""

from __future__ import annotations

from typing import Any

import pytest
from structlog.testing import capture_logs

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.integrations.embedding.local_sentence_transformer import (
    LocalSentenceTransformerProvider,
)


class StubModel:
    """Stands in for SentenceTransformer."""

    def __init__(
        self, *, dimension: int = 4, max_seq_length: int = 256, error: Exception | None = None
    ) -> None:
        self.dimension = dimension
        self.max_seq_length = max_seq_length
        self.error = error
        self.calls: list[list[str]] = []
        self.last_kwargs: dict[str, Any] = {}

    def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        if self.error is not None:
            raise self.error
        self.calls.append(list(texts))
        self.last_kwargs = kwargs
        return [[0.5] * self.dimension for _ in texts]


def provider_with(model: StubModel, *, dimension: int = 4) -> LocalSentenceTransformerProvider:
    return LocalSentenceTransformerProvider(
        model_name="stub-model", dimension=dimension, model=model
    )


class TestEmbeddingProvider:
    @pytest.mark.asyncio
    async def test_documents_are_embedded(self) -> None:
        model = StubModel()
        vectors = await provider_with(model).embed_documents(["one", "two"])

        assert len(vectors) == 2
        assert all(len(vector) == 4 for vector in vectors)
        assert model.calls == [["one", "two"]]

    @pytest.mark.asyncio
    async def test_a_query_returns_a_single_vector(self) -> None:
        assert len(await provider_with(StubModel()).embed_query("question")) == 4

    @pytest.mark.asyncio
    async def test_no_texts_means_no_model_call(self) -> None:
        model = StubModel()
        assert await provider_with(model).embed_documents([]) == []
        assert model.calls == []

    @pytest.mark.asyncio
    async def test_vectors_are_normalized(self) -> None:
        """Cosine distance is only the intended comparison when vectors are unit
        length, and the pgvector HNSW index is built for cosine."""
        model = StubModel()
        await provider_with(model).embed_documents(["text"])
        assert model.last_kwargs["normalize_embeddings"] is True

    @pytest.mark.asyncio
    async def test_a_dimension_mismatch_is_terminal(self) -> None:
        """[D1]: storing a wrong-width vector corrupts the column instead of failing
        cleanly, and no retry will change the model's output width."""
        provider = provider_with(StubModel(dimension=8), dimension=384)

        with pytest.raises(NonRetryableProviderError, match="384"):
            await provider.embed_documents(["text"])

    @pytest.mark.asyncio
    async def test_running_out_of_memory_is_retryable(self) -> None:
        """Another task may free memory before the retry."""
        provider = provider_with(StubModel(error=MemoryError("out of memory")))

        with pytest.raises(RetryableProviderError):
            await provider.embed_documents(["text"])

    @pytest.mark.asyncio
    async def test_an_unexpected_model_error_is_retryable(self) -> None:
        provider = provider_with(StubModel(error=RuntimeError("CUDA hiccup")))

        with pytest.raises(RetryableProviderError):
            await provider.embed_documents(["text"])

    @pytest.mark.asyncio
    async def test_over_long_input_is_reported(self) -> None:
        """The model truncates past its window without raising, so the only way this
        becomes visible is a warning."""
        with capture_logs() as logs:
            await provider_with(StubModel(max_seq_length=10)).embed_documents(["x" * 500])

        assert any(entry["event"] == "embedding_input_may_truncate" for entry in logs)

    @pytest.mark.asyncio
    async def test_normal_input_is_not_reported(self) -> None:
        with capture_logs() as logs:
            await provider_with(StubModel(max_seq_length=256)).embed_documents(["short text"])

        assert not any(entry["event"] == "embedding_input_may_truncate" for entry in logs)

    def test_the_model_identity_is_exposed(self) -> None:
        """Recorded per chunk so [D1]'s ban on mixing models is enforceable."""
        provider = provider_with(StubModel())
        assert provider.model_id == "stub-model"
        assert provider.dimension == 4

    @pytest.mark.asyncio
    async def test_a_missing_model_fails_terminally(self) -> None:
        """A model that cannot be loaded will not load on the next attempt either."""
        provider = LocalSentenceTransformerProvider(
            model_name="definitely/not-a-real-model-name", dimension=4
        )

        with pytest.raises(NonRetryableProviderError, match="Could not load"):
            await provider.embed_documents(["text"])


class TestWarmup:
    @pytest.mark.asyncio
    async def test_warmup_loads_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fresh instance pays the load at boot, not mid-turn — and only once,
        even if lifespan and a turn race it (the lock lives inside `_load`)."""
        provider = LocalSentenceTransformerProvider(model_name="m", dimension=4)
        loads: list[int] = []

        def fake_load() -> Any:
            loads.append(1)
            monkeypatch.setattr(provider, "_model", StubModel())
            return provider._model

        monkeypatch.setattr(provider, "_load", fake_load)
        await provider.warmup()
        await provider.warmup()

        assert loads == [1]

    @pytest.mark.asyncio
    async def test_warmup_failure_propagates_to_the_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`warmup` reports; the lifespan decides. Only the caller knows whether
        anything else on the service still works without embeddings."""
        provider = LocalSentenceTransformerProvider(model_name="m", dimension=4)

        def fail() -> Any:
            raise RuntimeError("weights missing")

        monkeypatch.setattr(provider, "_load", fail)
        with pytest.raises(RuntimeError, match="weights missing"):
            await provider.warmup()
