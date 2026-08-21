"""Document parsing and text cleaning (specification §11).

Turns an uploaded file into clean text. Nothing here embeds or stores; that is the
ingestion handler's job.

Supported formats are plain text, Markdown and PDF — the shapes company knowledge
actually arrives in. Other formats are rejected explicitly rather than parsed as
garbage, because a silently mis-parsed document produces plausible-looking chunks
that poison retrieval in ways nobody notices.
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


class DocumentFormat(StrEnum):
    TEXT = "text"
    MARKDOWN = "markdown"
    PDF = "pdf"


class DocumentParseError(Exception):
    """The upload could not be read as a supported document."""


class UnsupportedFormatError(DocumentParseError):
    """The file extension is not one this platform parses."""


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    text: str
    format: DocumentFormat
    # PDFs report their page count; other formats leave it None.
    page_count: int | None = None


_EXTENSIONS = {
    ".txt": DocumentFormat.TEXT,
    ".text": DocumentFormat.TEXT,
    ".md": DocumentFormat.MARKDOWN,
    ".markdown": DocumentFormat.MARKDOWN,
    ".pdf": DocumentFormat.PDF,
}

SUPPORTED_EXTENSIONS = tuple(sorted(_EXTENSIONS))

# Markdown constructs that carry no meaning once the text is a retrieval chunk.
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_FENCE = re.compile(r"^```.*$", re.MULTILINE)
_MD_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)")
_MD_BLOCKQUOTE = re.compile(r"^>\s?", re.MULTILINE)

_MULTI_BLANK = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
# Zero-width and control characters. A PDF copy-paste is full of them, and they
# survive into chunks where they waste embedding tokens and break exact matching.
_INVISIBLE = re.compile(r"[​-‏‪-‮﻿]")


def format_for_filename(filename: str) -> DocumentFormat:
    """Determine the format from the file extension.

    Extension rather than sniffing: the client controls the filename, and a
    misdetected format is worse than a rejected upload.
    """
    lowered = filename.lower()
    for extension, document_format in _EXTENSIONS.items():
        if lowered.endswith(extension):
            return document_format
    raise UnsupportedFormatError(
        f"Unsupported file type. Supported: {', '.join(SUPPORTED_EXTENSIONS)}"
    )


def parse_document(content: bytes, *, filename: str) -> ParsedDocument:
    """Extract text from an uploaded document."""
    document_format = format_for_filename(filename)

    if document_format is DocumentFormat.PDF:
        return _parse_pdf(content)

    text = _decode(content)
    if document_format is DocumentFormat.MARKDOWN:
        text = strip_markdown(text)

    cleaned = clean_text(text)
    if not cleaned:
        raise DocumentParseError("The document contains no readable text")
    return ParsedDocument(text=cleaned, format=document_format)


def _decode(content: bytes) -> str:
    """Decode bytes as text, tolerating the encodings real files arrive in."""
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    # latin-1 cannot fail, so reaching here means the content was not text at all.
    raise DocumentParseError("The document could not be decoded as text")


def _parse_pdf(content: bytes) -> ParsedDocument:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
    except DocumentParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises a variety of types
        raise DocumentParseError("The file could not be read as a PDF") from exc

    cleaned = clean_text("\n\n".join(pages))
    if not cleaned:
        # A scanned PDF is images with no text layer. Accepting it would create a
        # document that looks ingested but can never be retrieved.
        raise DocumentParseError(
            "No text could be extracted. A scanned PDF needs OCR before upload."
        )
    return ParsedDocument(text=cleaned, format=DocumentFormat.PDF, page_count=len(pages))


def strip_markdown(text: str) -> str:
    """Reduce Markdown to its prose.

    Retrieval matches on meaning, and syntax characters only dilute the embedding.
    Link *text* is kept while the URL is dropped, because the words carry meaning and
    the address does not.
    """
    text = _MD_FENCE.sub("", text)
    text = _MD_IMAGE.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BLOCKQUOTE.sub("", text)
    return _MD_EMPHASIS.sub("", text)


def clean_text(text: str) -> str:
    """Normalize extracted text.

    NFKC folds the typographic variants PDFs are full of — ligatures, non-breaking
    spaces, fullwidth forms — so that "workflow" and "workﬂow" embed as the same word
    rather than as neighbours.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_SPACE.sub("", text)
    # Collapse runs of blank lines to one, so paragraph boundaries stay meaningful
    # for the chunker without carrying the original's spacing quirks.
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()
