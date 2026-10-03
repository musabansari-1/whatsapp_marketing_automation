"""Retrieval policy and context assembly (specification §11).

Two rules from §11 shape everything here.

**Retrieved documents are untrusted data.** A knowledge document is uploaded by a
human and may contain anything, including text engineered to look like an
instruction. Nothing this module produces is ever an instruction: `build_context`
emits a clearly delimited block of *quoted material*, and the caller in Phase 10 is
responsible for keeping it in a data position. Instruction-shaped lines are additionally
neutralised so they cannot terminate the block or impersonate the system.

**Insufficient evidence must not become invention.** Retrieval reports how good its
matches are; when nothing clears the threshold the caller uses the approved fallback
rather than letting the model improvise. That decision lives here, not in a prompt.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

# pgvector cosine distance runs 0 (identical) to 2 (opposite); similarity is 1 - d.
# The default threshold is deliberately permissive: over-filtering makes the assistant
# claim ignorance about things it was told, which reads worse than a slightly loose
# answer and is harder to diagnose. Tune against real questions in Phase 10.
DEFAULT_MIN_SIMILARITY = 0.25
DEFAULT_TOP_K = 5
DEFAULT_MAX_CONTEXT_CHARS = 4000

# Reciprocal-rank-fusion constant. Standard 60: a rank-1 hit outscores two
# rank-10 hits, and depth past ~20 contributes noise rather than signal. A
# ranking constant, not a tunable threshold — it shapes order, and order is
# what fusion is for.
RRF_K = 60

# The delimiter that fences retrieved material. Chosen to be something a document is
# vanishingly unlikely to contain, and stripped from content regardless.
CONTEXT_OPEN = "<<<KNOWLEDGE_BASE_EXCERPT>>>"
CONTEXT_CLOSE = "<<<END_KNOWLEDGE_BASE_EXCERPT>>>"

# Lines that try to look like a new instruction or a role change. Neutralised rather
# than dropped: removing text silently would hide a document's real content from the
# operators reviewing why an answer was wrong.
_INSTRUCTION_SHAPED = re.compile(
    r"^\s*(system|assistant|user|developer)\s*[:>]",
    re.IGNORECASE | re.MULTILINE,
)
_DELIMITER_LOOKALIKE = re.compile(r"<<<[^>]*>>>")


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One chunk returned by similarity search.

    `keyword_rank` is the position in the term-match ranking (0-based), or None
    when the chunk did not term-match. Set by fusion, never by either search
    alone: a chunk's two signals travel together from there on.
    """

    chunk_id: str
    document_id: str
    document_title: str
    content: str
    similarity: float
    keyword_rank: int | None = None


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    """How retrieval behaves. Configuration, not literals."""

    top_k: int = DEFAULT_TOP_K
    min_similarity: float = DEFAULT_MIN_SIMILARITY
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS

    def __post_init__(self) -> None:
        if self.top_k < 1:
            raise ValueError("top_k must be at least 1")
        if not -1.0 <= self.min_similarity <= 1.0:
            raise ValueError("min_similarity must be between -1 and 1")
        if self.max_context_chars < 100:
            raise ValueError("max_context_chars must be at least 100")


@dataclass(slots=True)
class RetrievalResult:
    """What retrieval found, and whether it is enough to answer from."""

    chunks: list[RetrievedChunk] = field(default_factory=list)
    context: str = ""
    has_sufficient_evidence: bool = False

    @property
    def cited_chunk_ids(self) -> list[str]:
        """For the `ai_runs` audit trail: which knowledge informed the answer."""
        return [chunk.chunk_id for chunk in self.chunks]


def similarity_from_distance(distance: float) -> float:
    """Convert pgvector cosine distance to a similarity in [-1, 1]."""
    return 1.0 - distance


def select_chunks(
    candidates: list[RetrievedChunk], *, policy: RetrievalPolicy
) -> list[RetrievedChunk]:
    """Keep the chunks worth showing the model.

    Fused order is preserved when the candidates carry it (keyword ranks from
    fusion); otherwise similarity order. Either way a chunk survives only by
    clearing the dense threshold or by term-matching: a weak dense match is
    worse than no match, but a term match is its own evidence — dense vectors
    systematically underweight exact entities (names, prices, dates), which is
    what the keyword path exists to catch.

    Ordered by the ranking that produced them and cut at top_k: the union of two
    top-k lists, so taking more would re-admit what both rankings rejected.
    """
    if any(chunk.keyword_rank is not None for chunk in candidates):
        ranked = list(candidates)
    else:
        ranked = sorted(candidates, key=lambda chunk: chunk.similarity, reverse=True)
    kept = [
        chunk
        for chunk in ranked
        if chunk.similarity >= policy.min_similarity or chunk.keyword_rank is not None
    ]
    return kept[: policy.top_k]


def build_context(chunks: list[RetrievedChunk], *, policy: RetrievalPolicy | None = None) -> str:
    """Assemble retrieved chunks into one bounded, clearly-quoted block (§11).

    Bounded because §11 requires it and because an unbounded context is both expensive
    and prone to burying the relevant passage. Chunks are added in similarity order
    until the budget is spent, so the best evidence survives truncation.

    Every chunk is sanitised and attributed to its document, which gives operators a
    trail when an answer is wrong and gives the model a reason to treat the text as a
    quotation rather than as something addressed to it.
    """
    policy = policy or RetrievalPolicy()
    if not chunks:
        return ""

    parts: list[str] = []
    budget = policy.max_context_chars

    for chunk in chunks:
        body = sanitize_for_context(chunk.content)
        entry = f"[Source: {sanitize_for_context(chunk.document_title)}]\n{body}"
        if len(entry) > budget:
            break
        parts.append(entry)
        budget -= len(entry)

    if not parts:
        return ""

    inner = "\n\n".join(parts)
    return f"{CONTEXT_OPEN}\n{inner}\n{CONTEXT_CLOSE}"


def sanitize_for_context(text: str) -> str:
    """Neutralise text that could escape its quoted block (§11).

    Two specific dangers, both from documents an employee uploaded in good faith and
    an attacker may have authored:

    - a delimiter lookalike would close the block early and let the remainder read as
      though it came from the system
    - a line beginning "System:" or "Assistant:" imitates a role marker

    Both are defanged in place rather than deleted, so an operator reviewing a bad
    answer still sees what the document actually said.
    """
    cleaned = _DELIMITER_LOOKALIKE.sub("[redacted-delimiter]", text)
    cleaned = _INSTRUCTION_SHAPED.sub(lambda match: match.group(0).replace(":", "∶"), cleaned)
    return cleaned.strip()


def has_sufficient_evidence(chunks: list[RetrievedChunk], *, policy: RetrievalPolicy) -> bool:
    """Whether the retrieved material can support an answer (§11).

    A dense match at or above the threshold, or a term match: `plainto_tsquery`
    ANDs the query's terms, so a matched chunk contains every one of them, which
    is evidence no matter what the vector says. When this is False the caller
    must use the approved fallback message rather than answering — §11 is
    explicit that insufficient evidence must not become invention.
    """
    return any(
        chunk.similarity >= policy.min_similarity or chunk.keyword_rank is not None
        for chunk in chunks
    )


def fuse_rankings(
    dense: list[RetrievedChunk], keyword: list[RetrievedChunk], *, rrf_k: int = RRF_K
) -> list[RetrievedChunk]:
    """Reciprocal-rank fusion of the dense and keyword rankings.

    Each list contributes 1/(k + rank) per chunk; absent from a list contributes
    nothing. Dense similarity rides along untouched — fusion reorders, it never
    re-scores — and keyword positions are stamped on for selection and evidence
    downstream. Ties break by similarity, then ID, so the order is deterministic.
    """
    dense_rank = {chunk.chunk_id: index for index, chunk in enumerate(dense)}
    keyword_rank = {chunk.chunk_id: index for index, chunk in enumerate(keyword)}
    scores: dict[str, float] = {}
    for chunk_id, index in dense_rank.items():
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + index + 1)
    for chunk_id, index in keyword_rank.items():
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + index + 1)
    by_id = {chunk.chunk_id: chunk for chunk in (*dense, *keyword)}
    ordered = sorted(
        scores,
        key=lambda chunk_id: (
            -scores[chunk_id],
            -by_id[chunk_id].similarity,
            chunk_id,
        ),
    )
    return [
        replace(by_id[chunk_id], keyword_rank=keyword_rank.get(chunk_id)) for chunk_id in ordered
    ]


def retrieve(
    candidates: list[RetrievedChunk],
    keyword: Sequence[RetrievedChunk] = (),
    *,
    policy: RetrievalPolicy | None = None,
) -> RetrievalResult:
    """Apply the full retrieval policy to raw search candidates.

    With a keyword list, dense and keyword rankings fuse first (reciprocal
    rank); without one, the dense ranking stands alone — the legacy path, kept
    so callers without a term index behave exactly as before.
    """
    policy = policy or RetrievalPolicy()
    ranked = fuse_rankings(list(candidates), list(keyword)) if keyword else candidates
    selected = select_chunks(ranked, policy=policy)

    return RetrievalResult(
        chunks=selected,
        context=build_context(selected, policy=policy),
        has_sufficient_evidence=has_sufficient_evidence(selected, policy=policy),
    )
