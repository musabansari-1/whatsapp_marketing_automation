"""The product readiness report, as an operator reads it (Phase 27).

Plain text for a terminal, or JSON for a script. Both say the same thing: the platform-wide
facts first, then each product with its verdict and exactly what stands in its way.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.configuration.legacy import LegacyEnvironment
from app.domain.crm import single_product_sync_is_safe
from app.domain.readiness import ProductReadiness, Verdict

if TYPE_CHECKING:
    from app.core.config import Settings


@dataclass(frozen=True, slots=True)
class PlatformNote:
    label: str
    text: str


def platform_notes(
    settings: Settings,
    *,
    product_count: int,
    legacy: LegacyEnvironment | None = None,
) -> list[PlatformNote]:
    """Facts that hold for every product, and so belong above them rather than in each."""
    providers = " · ".join(
        f"{name} {value}"
        for name, value in (
            ("WhatsApp", settings.whatsapp_provider.value),
            ("LLM", settings.llm_provider.value),
            ("Embedding", settings.embedding_provider.value),
            ("Calendar", settings.calendar_provider.value),
            ("CRM", settings.crm_provider.value),
            ("Queue", settings.queue_provider.value),
        )
    )
    crm = (
        f"single-product synchronization ({settings.crm_provider.value})"
        if single_product_sync_is_safe(product_count=product_count)
        else (
            "DEFERRED / DISABLED FOR MULTI-PRODUCT — synchronization is skipped while more "
            "than one product exists ([D58]). Not a launch blocker."
        )
    )
    notes = [
        PlatformNote("Environment", settings.app_env.value),
        PlatformNote("Providers", providers),
        PlatformNote("CRM", crm),
        PlatformNote(
            "Knowledge uploads",
            f"at most {settings.knowledge_max_file_bytes:,} bytes per document: the bytes "
            "travel inside the task message (D-08)",
        ),
        PlatformNote(
            "Retrieval",
            "enabled" if settings.ai_retrieval_enabled else "disabled platform-wide",
        ),
    ]
    still_set = (legacy or LegacyEnvironment()).present()
    if still_set:
        notes.append(
            PlatformNote(
                "Legacy variables",
                f"{', '.join(still_set)} still set but no longer read. "
                "`import-legacy-config <slug>` copies them into a product.",
            )
        )
    return notes


def render_product(report: ProductReadiness) -> str:
    state = "active" if report.is_active else "inactive"
    lines = [f"{report.slug}  ({report.display_name}, {state})  {report.verdict.value}"]
    if not report.findings:
        lines.append("  Nothing missing.")
    for finding in report.findings:
        lines.append(f"  {finding.severity.value:<7}  {finding.area.value}: {finding.message}")
    return "\n".join(lines)


def render_text(reports: Sequence[ProductReadiness], notes: Sequence[PlatformNote]) -> str:
    width = max((len(note.label) for note in notes), default=0)
    lines = ["GLOBAL"]
    lines.extend(f"  {note.label.ljust(width)}  {note.text}" for note in notes)
    for report in reports:
        lines.extend(["", render_product(report)])
    lines.extend(["", summary(reports)])
    return "\n".join(lines)


def summary(reports: Sequence[ProductReadiness]) -> str:
    if not reports:
        return "Summary: no products."
    counts = Counter(report.verdict for report in reports)
    text = "Summary: " + ", ".join(
        f"{counts[verdict]} {verdict.value}" for verdict in Verdict if counts[verdict]
    )
    unsafe = [report.slug for report in reports if report.unsafe]
    if unsafe:
        text += f". UNSAFE — active but not ready: {', '.join(unsafe)}"
    return text + "."


def render_json(reports: Sequence[ProductReadiness], notes: Sequence[PlatformNote]) -> str:
    return json.dumps(
        {
            "global": {note.label: note.text for note in notes},
            "products": [
                {
                    "slug": report.slug,
                    "display_name": report.display_name,
                    "is_active": report.is_active,
                    "verdict": report.verdict.value,
                    "unsafe": report.unsafe,
                    "findings": [
                        {
                            "code": finding.code,
                            "area": finding.area.value,
                            "severity": finding.severity.value,
                            "message": finding.message,
                        }
                        for finding in report.findings
                    ],
                }
                for report in reports
            ],
        },
        indent=2,
    )


__all__ = [
    "PlatformNote",
    "platform_notes",
    "render_json",
    "render_product",
    "render_text",
    "summary",
]
