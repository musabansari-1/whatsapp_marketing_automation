"""Embedding integration package."""

from app.integrations.embedding.fake import FAKE_EMBEDDING_MODEL, FakeEmbeddingProvider
from app.integrations.embedding.provider import EmbeddingProvider, Vector

__all__ = [
    "FAKE_EMBEDDING_MODEL",
    "EmbeddingProvider",
    "FakeEmbeddingProvider",
    "Vector",
]
