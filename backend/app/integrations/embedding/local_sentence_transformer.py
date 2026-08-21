"""Local Sentence Transformers embedding provider ([D1], specification §11).

V1 embeds in-process rather than calling a hosted API. That is the whole point of
[D1]: no per-request cost, no rate limit, no third party seeing customer questions or
company documents.

The trade is operational. The model and PyTorch are large, the first load takes
seconds, and the weights must be present in the image — `Dockerfile.worker` records
that they are baked in at build time rather than downloaded on a cold start, because a
Cloud Run instance fetching 90 MB before serving its first request is both slow and
dependent on network at boot.

**Silent truncation is the failure mode to know about.** `all-MiniLM-L6-v2` accepts 256
word-pieces and quietly discards the rest, so an over-long chunk is embedded from its
opening words alone with no error raised. The chunker keeps inputs well inside that
window; this provider reports when something arrives too long anyway, so the problem is
visible rather than silently degrading retrieval.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

from app.core.errors import NonRetryableProviderError, RetryableProviderError
from app.core.logging import get_logger
from app.integrations.embedding.provider import EmbeddingProvider, Vector

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = get_logger(__name__)


class LocalSentenceTransformerProvider(EmbeddingProvider):
    """Embeds text with a Sentence Transformers model loaded in this process."""

    name = "local_sentence_transformer"

    def __init__(
        self,
        *,
        model_name: str,
        dimension: int,
        batch_size: int = 32,
        model: Any | None = None,
    ) -> None:
        self._model_name = model_name
        self._dimension = dimension
        self._batch_size = batch_size
        self._model: SentenceTransformer | None = model
        # Loading is not thread-safe and is expensive; the lock ensures concurrent
        # first requests load once rather than racing to load several copies of a
        # multi-hundred-megabyte model.
        self._lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_documents(self, texts: list[str]) -> list[Vector]:
        if not texts:
            return []
        return await self._encode(texts)

    async def embed_query(self, text: str) -> Vector:
        vectors = await self._encode([text])
        return vectors[0]

    # ---------------------------------------------------------------- internals

    async def _encode(self, texts: list[str]) -> list[Vector]:
        """Run the model without blocking the event loop.

        Encoding is CPU-bound and can take hundreds of milliseconds per batch. Left on
        the loop it would stall every other coroutine in the worker, so it runs in a
        thread.
        """
        try:
            vectors = await asyncio.to_thread(self._encode_sync, texts)
        except NonRetryableProviderError:
            raise
        except MemoryError as exc:
            # Worth retrying: another task may have finished and freed memory.
            raise RetryableProviderError(
                "Ran out of memory while embedding", provider=self.name
            ) from exc
        except Exception as exc:  # noqa: BLE001 - torch raises a wide variety
            raise RetryableProviderError(f"Embedding failed: {exc}", provider=self.name) from exc

        if vectors and len(vectors[0]) != self._dimension:
            # [D1] forbids mixing vectors of different widths. A mismatch means the
            # configured model and dimension disagree, and storing the result would
            # corrupt the column rather than fail cleanly.
            raise NonRetryableProviderError(
                f"Model {self._model_name!r} produced {len(vectors[0])}-dimensional "
                f"vectors but configuration expects {self._dimension}",
                provider=self.name,
            )

        return vectors

    def _encode_sync(self, texts: list[str]) -> list[Vector]:
        model = self._load()

        max_length = getattr(model, "max_seq_length", None)
        if max_length:
            # Approximate, but enough to surface a chunking regression. Silent
            # truncation is otherwise invisible until retrieval quality degrades.
            budget = max_length * 4
            for index, text in enumerate(texts):
                if len(text) > budget:
                    logger.warning(
                        "embedding_input_may_truncate",
                        index=index,
                        characters=len(text),
                        model_max_tokens=max_length,
                    )

        encoded = model.encode(
            texts,
            batch_size=self._batch_size,
            # Normalised, so cosine distance is the meaningful comparison and the
            # pgvector HNSW index built for cosine behaves as intended.
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in vector] for vector in encoded]

    def _load(self) -> SentenceTransformer:
        """Load the model once, on first use.

        Lazy rather than at construction so importing the application — in the API
        service, in a CLI command, in a test — does not pay for a model it will never
        call.
        """
        if self._model is not None:
            return self._model

        with self._lock:
            # Re-checked inside the lock: another thread may have loaded it while
            # this one waited. Annotated explicitly because mypy has narrowed the
            # attribute to None from the check above and cannot model the race.
            cached: SentenceTransformer | None = self._model
            if cached is not None:
                return cached

            logger.info("embedding_model_loading", model=self._model_name)
            try:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(self._model_name)
            except Exception as exc:  # noqa: BLE001 - import or download failure
                raise NonRetryableProviderError(
                    f"Could not load embedding model {self._model_name!r}: {exc}",
                    provider=self.name,
                ) from exc

            logger.info(
                "embedding_model_loaded",
                model=self._model_name,
                max_seq_length=getattr(self._model, "max_seq_length", None),
            )
            return self._model
