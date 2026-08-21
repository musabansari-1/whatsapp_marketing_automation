"""Text chunking for retrieval (specification §11, [D1]).

Chunk size is not a free parameter. `all-MiniLM-L6-v2` truncates input at 256
word-pieces and does so **silently** — a chunk longer than that is embedded from its
opening words alone, and the rest becomes unretrievable without any error to show for
it. The default target here sits well inside that limit.

Boundaries are chosen at paragraph, then sentence, then word, in that order. Splitting
mid-sentence produces chunks whose embedding represents a fragment rather than an
idea, which is how retrieval starts returning text that looks related but answers
nothing.

Overlap exists so a fact spanning a boundary survives in at least one chunk whole.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Roughly four characters per word-piece for English prose. Used only to keep the
# default target safely inside the model's window; the real guard is the token count
# the embedding provider reports.
CHARS_PER_TOKEN = 4

DEFAULT_CHUNK_CHARS = 800
DEFAULT_OVERLAP_CHARS = 120
MIN_CHUNK_CHARS = 50

_PARAGRAPH = re.compile(r"\n\s*\n")
# Sentence end followed by whitespace and a capital or quote. Deliberately simple:
# an over-clever splitter mangles abbreviations and decimals more often than it helps.
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'“‘])")


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable piece of a document."""

    index: int
    content: str

    @property
    def estimated_tokens(self) -> int:
        return max(1, len(self.content) // CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class ChunkingPolicy:
    """How a document is split. Configuration, not literals."""

    max_chars: int = DEFAULT_CHUNK_CHARS
    overlap_chars: int = DEFAULT_OVERLAP_CHARS
    min_chars: int = MIN_CHUNK_CHARS

    def __post_init__(self) -> None:
        if self.max_chars < 100:
            raise ValueError("max_chars must be at least 100")
        if self.overlap_chars < 0:
            raise ValueError("overlap_chars cannot be negative")
        if self.overlap_chars >= self.max_chars:
            # Otherwise each chunk would re-include the whole of its predecessor and
            # the walk would never advance.
            raise ValueError("overlap_chars must be smaller than max_chars")


def chunk_text(text: str, *, policy: ChunkingPolicy | None = None) -> list[Chunk]:
    """Split cleaned text into overlapping chunks.

    Returns an empty list for empty input rather than a single empty chunk, so an
    unreadable document produces no rows and stays visibly un-ingested.
    """
    policy = policy or ChunkingPolicy()
    stripped = text.strip()
    if not stripped:
        return []

    pieces = _split_to_size(stripped, policy)
    merged = _merge_small(pieces, policy)
    overlapped = _apply_overlap(merged, policy)

    return [Chunk(index=index, content=content) for index, content in enumerate(overlapped)]


def _split_to_size(text: str, policy: ChunkingPolicy) -> list[str]:
    """Split on the largest natural boundary that fits."""
    if len(text) <= policy.max_chars:
        return [text]

    parts: list[str] = []
    for paragraph in _PARAGRAPH.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= policy.max_chars:
            parts.append(paragraph)
        else:
            parts.extend(_split_sentences(paragraph, policy))
    return parts


def _split_sentences(paragraph: str, policy: ChunkingPolicy) -> list[str]:
    """Group sentences into chunks that fit."""
    parts: list[str] = []
    current = ""

    for sentence in _SENTENCE.split(paragraph):
        sentence = sentence.strip()
        if not sentence:
            continue

        if len(sentence) > policy.max_chars:
            # A single sentence longer than the window — a table or a run-on. Flush
            # what we have and fall back to word splitting.
            if current:
                parts.append(current)
                current = ""
            parts.extend(_split_words(sentence, policy))
            continue

        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= policy.max_chars:
            current = candidate
        else:
            parts.append(current)
            current = sentence

    if current:
        parts.append(current)
    return parts


def _split_words(sentence: str, policy: ChunkingPolicy) -> list[str]:
    """Last resort: split on words so no chunk exceeds the window.

    Words rather than characters, so a chunk never ends mid-word — a truncated token
    embeds as noise.
    """
    parts: list[str] = []
    current = ""

    for word in sentence.split():
        candidate = f"{current} {word}".strip() if current else word
        if len(candidate) <= policy.max_chars:
            current = candidate
        else:
            if current:
                parts.append(current)
            # A single word longer than the window is pathological; hard-split it.
            while len(word) > policy.max_chars:
                parts.append(word[: policy.max_chars])
                word = word[policy.max_chars :]
            current = word

    if current:
        parts.append(current)
    return parts


def _merge_small(parts: list[str], policy: ChunkingPolicy) -> list[str]:
    """Fold undersized fragments into their neighbour.

    A heading or one-line paragraph on its own embeds to almost nothing useful; joined
    to the text it introduces, it becomes retrievable.
    """
    merged: list[str] = []
    for part in parts:
        if merged and len(part) < policy.min_chars:
            candidate = f"{merged[-1]}\n{part}"
            if len(candidate) <= policy.max_chars:
                merged[-1] = candidate
                continue
        if merged and len(merged[-1]) < policy.min_chars:
            candidate = f"{merged[-1]}\n{part}"
            if len(candidate) <= policy.max_chars:
                merged[-1] = candidate
                continue
        merged.append(part)
    return merged


def _apply_overlap(parts: list[str], policy: ChunkingPolicy) -> list[str]:
    """Prefix each chunk with the tail of its predecessor.

    A fact split across a boundary — "Our refund window is" | "30 days" — is
    unretrievable from either half. The overlap makes at least one chunk contain it
    whole. The tail is taken at a word boundary so the prefix reads as language.
    """
    if policy.overlap_chars == 0 or len(parts) < 2:
        return parts

    result = [parts[0]]
    for previous, current in zip(parts, parts[1:], strict=False):
        tail = previous[-policy.overlap_chars :]
        # Drop a leading partial word so the chunk starts cleanly.
        if " " in tail:
            tail = tail.split(" ", 1)[1]
        result.append(f"{tail} {current}".strip() if tail else current)
    return result
