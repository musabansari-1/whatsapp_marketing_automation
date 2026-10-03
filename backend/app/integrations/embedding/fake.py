"""Deterministic embedding provider for automated tests.

Hash-seeded unit vectors: identical text always yields an identical vector, and
different text yields a different one. That is enough to exercise the Phase 8
pgvector round-trip and similarity ordering without loading a real model, which
keeps the test suite free of the PyTorch dependency.

Not semantically meaningful. Retrieval *quality* is evaluated in Phase 8 against the
real model, per [D1].
"""

from __future__ import annotations

import hashlib
import math
import random

from app.core.errors import RetryableProviderError
from app.integrations.embedding.provider import EmbeddingProvider, Vector

FAKE_EMBEDDING_MODEL = "fake/deterministic-embedding"


class FakeEmbeddingProvider(EmbeddingProvider):
    name = "fake"

    def __init__(
        self,
        *,
        dimension: int = 384,
        model_id: str = FAKE_EMBEDDING_MODEL,
        fail_with: Exception | None = None,
    ) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self._dimension = dimension
        self._model_id = model_id
        self.fail_with = fail_with
        self.embedded_documents: list[str] = []
        self.embedded_queries: list[str] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return self._dimension

    def _vector_for(self, text: str) -> Vector:
        """Deterministic unit vector seeded by the text's digest."""
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        rng = random.Random(digest)  # noqa: S311 - deterministic test fixture, not crypto
        raw = [rng.uniform(-1.0, 1.0) for _ in range(self._dimension)]
        norm = math.sqrt(sum(value * value for value in raw))
        if norm == 0.0:  # pragma: no cover - unreachable for any real digest
            return [0.0] * self._dimension
        return [value / norm for value in raw]

    async def embed_documents(self, texts: list[str]) -> list[Vector]:
        if self.fail_with is not None:
            raise self.fail_with
        self.embedded_documents.extend(texts)
        return [self._vector_for(text) for text in texts]

    async def embed_query(self, text: str) -> Vector:
        if self.fail_with is not None:
            raise self.fail_with
        self.embedded_queries.append(text)
        return self._vector_for(text)

    async def warmup(self) -> None:
        """No-op: nothing to preload. Present so consumers (worker lifespan, the
        re-embed command) need no provider check before calling it."""
        return

    # ---------------------------------------------------------------- test helpers

    def reset(self) -> None:
        self.embedded_documents.clear()
        self.embedded_queries.clear()

    def fail_retryably(self, message: str = "simulated embedding failure") -> None:
        self.fail_with = RetryableProviderError(message, provider=self.name)

    def stop_failing(self) -> None:
        self.fail_with = None
