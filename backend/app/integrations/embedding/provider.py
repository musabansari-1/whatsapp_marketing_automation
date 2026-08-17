"""Embedding provider interface (specification §11, [D1]).

    EmbeddingProvider
     ├── LocalSentenceTransformerProvider  (Phase 8)
     ├── FakeEmbeddingProvider
     ├── GeminiEmbeddingProvider           (future)
     └── VoyageEmbeddingProvider           (future)

Model name and dimension are configuration, and `model_id` is exposed so stored
vectors can be attributed to the model that produced them. [D1] forbids mixing
vectors from incompatible models or dimensions; recording the model is what lets
Phase 8 enforce that.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

Vector = list[float]


class EmbeddingProvider(ABC):
    name: str

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Identifier of the embedding model, stored alongside vectors."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Vector width. Must match the pgvector column dimension (§15)."""

    @abstractmethod
    async def embed_documents(self, texts: list[str]) -> list[Vector]:
        """Embed document chunks for storage."""

    @abstractmethod
    async def embed_query(self, text: str) -> Vector:
        """Embed a single customer question for retrieval."""
