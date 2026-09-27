"""Product readiness: can this product safely be activated? (Phase 27)

Pure. The caller gathers what the database holds for one product (`ProductFacts`) and the
platform values it inherits (`PlatformInputs`); this decides what is missing, and how much
it matters.

Two severities, and the difference is the activation rule:

    BLOCKER  activating would be unsafe or would simply not work — no voice of its own, no
             number to route by, no calendar, a spreadsheet format nobody confirmed.
             `activate-product` refuses.
    GAP      the product works, but a capability is incomplete — no approved marketing
             template means no campaign; no ready knowledge means every factual question
             gets the fallback. Activation needs an explicit acknowledgement.

**This is configuration readiness, not provider health.** Nothing here calls Meta or
Google. A phone number ID can be present and wrong, and only a real message proves
otherwise. What this promises is narrower and still worth having: nothing a product needs is
absent, invalid, or silently shared with another product.

**Readiness never activates anything**, and it replaces no runtime defence. An unknown number
is still refused, a product without policy still does not answer, a product without a
calendar still does not book.

Nothing here branches on which product it is. The same checks run for every row.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.domain.ai_config import (
    AIConfigurationError,
    ConfigurationRow,
    PlatformDefaults,
    resolve,
)
from app.domain.audience import (
    AudienceSchemaError,
    is_development_placeholder,
    parse_audience_schema,
)
from app.domain.conversion import (
    DEFAULT_CONVERSION_MODE,
    ConversionMode,
    parse_conversion_mode,
    uses_calendar_booking,
)
from app.domain.product import SLUG_MAX_LENGTH, is_valid_slug
from app.domain.qualification import QualificationMode
from app.domain.slots import (
    SlotPolicyError,
    SlotPolicyOverrides,
    SlotPolicySettings,
    resolve_slot_policy,
)


class Severity(StrEnum):
    BLOCKER = "BLOCKER"
    GAP = "GAP"


class Verdict(StrEnum):
    READY = "READY"
    READY_WITH_GAPS = "READY WITH GAPS"
    NOT_READY = "NOT READY"


class Area(StrEnum):
    IDENTITY = "identity"
    AUDIENCE = "audience schema"
    AI = "AI configuration"
    WHATSAPP = "WhatsApp"
    CALENDAR = "calendar"
    SLOTS = "slot policy"
    CONVERSION = "conversion"
    TEMPLATES = "templates"
    KNOWLEDGE = "knowledge"


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing that stands between a product and a clean activation.

    `code` is stable and machine-readable; `message` says what to do about it.
    """

    code: str
    area: Area
    severity: Severity
    message: str


@dataclass(frozen=True, slots=True)
class ProductFacts:
    """Everything readiness needs to know about one product, already read from storage."""

    slug: str
    display_name: str
    is_active: bool
    audience_column_schema: object | None
    whatsapp_phone_number_id: str | None
    whatsapp_business_account_id: str | None
    whatsapp_access_token_secret_name: str | None
    google_calendar_id: str | None
    slot_overrides: SlotPolicyOverrides
    segment_slugs: tuple[str, ...]
    product_ai: ConfigurationRow | None
    segment_ai: Mapping[str, ConfigurationRow]
    #: How interest converts. Default is calendar booking so existing products stay safe.
    conversion_mode: str = DEFAULT_CONVERSION_MODE.value
    registration_url: str | None = None
    #: The product's permanent meeting link (Option 2). None means time-only bookings.
    meeting_url: str | None = None
    #: Other products holding the same phone number ID, or the same calendar, by slug.
    phone_number_also_on: tuple[str, ...] = ()
    calendar_also_on: tuple[str, ...] = ()
    #: Other products holding the same meeting link, by slug. A shared room would put
    #: two products' demos in one call.
    meeting_url_also_on: tuple[str, ...] = ()
    #: Owned templates a campaign could use: Meta-approved and MARKETING ([D6], [D57]).
    usable_marketing_templates: int = 0
    ready_documents: int = 0
    failed_documents: int = 0


@dataclass(frozen=True, slots=True)
class PlatformInputs:
    """The platform values a product inherits, and the environment it is judged for."""

    ai_defaults: PlatformDefaults
    slot_settings: SlotPolicySettings
    retrieval_enabled: bool
    production: bool


@dataclass(frozen=True, slots=True)
class ProductReadiness:
    slug: str
    display_name: str
    is_active: bool
    findings: tuple[Finding, ...]

    @property
    def blockers(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.BLOCKER)

    @property
    def gaps(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.GAP)

    @property
    def blocker_codes(self) -> frozenset[str]:
        return frozenset(f.code for f in self.blockers)

    @property
    def verdict(self) -> Verdict:
        if self.blockers:
            return Verdict.NOT_READY
        if self.gaps:
            return Verdict.READY_WITH_GAPS
        return Verdict.READY

    @property
    def unsafe(self) -> bool:
        """Active while not ready: the one state the readiness command exits non-zero for."""
        return self.is_active and self.verdict is Verdict.NOT_READY


def evaluate(facts: ProductFacts, platform: PlatformInputs) -> ProductReadiness:
    """Judge one product. The order of findings is the order an operator would fix them."""
    findings = [
        *_identity(facts),
        *_audience(facts, platform),
        *_ai(facts, platform),
        *_whatsapp(facts),
        *_conversion(facts),
        *_calendar(facts),
        *_slots(facts, platform),
        *_templates(facts),
        *_knowledge(facts, platform),
    ]
    return ProductReadiness(
        slug=facts.slug,
        display_name=facts.display_name,
        is_active=facts.is_active,
        findings=tuple(findings),
    )


# ---------------------------------------------------------------- checks


def _blocker(code: str, area: Area, message: str) -> Finding:
    return Finding(code=code, area=area, severity=Severity.BLOCKER, message=message)


def _gap(code: str, area: Area, message: str) -> Finding:
    return Finding(code=code, area=area, severity=Severity.GAP, message=message)


def _identity(facts: ProductFacts) -> list[Finding]:
    findings = []
    if not is_valid_slug(facts.slug):
        findings.append(
            _blocker(
                "identity.slug_invalid",
                Area.IDENTITY,
                f"Slug {facts.slug!r} is not a permanent route identifier: use lowercase "
                f"words joined by hyphens, at most {SLUG_MAX_LENGTH} characters.",
            )
        )
    if not facts.display_name.strip():
        findings.append(
            _blocker("identity.name_missing", Area.IDENTITY, "The display name is empty.")
        )
    return findings


def _audience(facts: ProductFacts, platform: PlatformInputs) -> list[Finding]:
    if facts.audience_column_schema is None:
        return [
            _blocker(
                "audience.schema_missing",
                Area.AUDIENCE,
                "No audience column schema is configured, so this product's spreadsheet "
                "uploads are refused. Configure the confirmed column mapping for its files.",
            )
        ]
    try:
        schema = parse_audience_schema(facts.audience_column_schema)
    except AudienceSchemaError as exc:
        return [
            _blocker(
                "audience.schema_invalid",
                Area.AUDIENCE,
                f"The audience column schema is invalid: {exc}",
            )
        ]
    if is_development_placeholder(schema):
        message = (
            "The audience column schema is the development placeholder, not a confirmed "
            "spreadsheet format ([D20])."
        )
        if platform.production:
            return [
                _blocker(
                    "audience.schema_placeholder",
                    Area.AUDIENCE,
                    f"{message} Production refuses uploads with it.",
                )
            ]
        return [_gap("audience.schema_placeholder", Area.AUDIENCE, message)]
    return []


def _ai(facts: ProductFacts, platform: PlatformInputs) -> list[Finding]:
    if facts.product_ai is None or facts.product_ai.system_prompt is None:
        return [
            _blocker(
                "ai.policy_missing",
                Area.AI,
                "No product AI configuration with a system prompt. Its conversations are "
                "refused rather than answered with another product's policy.",
            )
        ]

    try:
        resolved = resolve(segment=None, product=facts.product_ai, defaults=platform.ai_defaults)
    except AIConfigurationError as exc:
        return [
            _blocker(
                "ai.policy_invalid",
                Area.AI,
                f"The product AI configuration does not resolve: {exc}",
            )
        ]

    findings = []
    if (
        resolved.qualification_mode is QualificationMode.REQUIRED_FIELDS
        and not resolved.qualification_required_fields
    ):
        findings.append(
            _gap(
                "ai.qualification_criteria_missing",
                Area.AI,
                "No qualification criteria are configured, so no lead can reach QUALIFIED ([D37]). "
                "Set qualification_required_fields, or use qualification_mode ai_judgement.",
            )
        )

    for slug in sorted(facts.segment_ai):
        try:
            resolve(
                segment=facts.segment_ai[slug],
                product=facts.product_ai,
                defaults=platform.ai_defaults,
            )
        except AIConfigurationError as exc:
            findings.append(
                _blocker(
                    "ai.segment_override_invalid",
                    Area.AI,
                    f"The AI override for segment {slug!r} does not resolve: {exc}",
                )
            )
    return findings


def _whatsapp(facts: ProductFacts) -> list[Finding]:
    findings = []
    if not facts.whatsapp_phone_number_id:
        findings.append(
            _blocker(
                "whatsapp.phone_number_missing",
                Area.WHATSAPP,
                "No WhatsApp phone number ID. Messages to this product cannot be routed, and "
                "nothing can be sent from it.",
            )
        )
    elif facts.phone_number_also_on:
        findings.append(
            _blocker(
                "whatsapp.phone_number_shared",
                Area.WHATSAPP,
                f"Phone number ID {facts.whatsapp_phone_number_id} is also configured on "
                f"{', '.join(facts.phone_number_also_on)}. One number routes to one product "
                "([D40]).",
            )
        )
    if not facts.whatsapp_business_account_id:
        findings.append(
            _blocker(
                "whatsapp.business_account_missing",
                Area.WHATSAPP,
                "No WhatsApp Business Account ID, so this product's templates cannot be "
                "synchronized. Products may share one account.",
            )
        )
    if facts.whatsapp_access_token_secret_name:
        findings.append(
            _blocker(
                "whatsapp.token_override_unsupported",
                Area.WHATSAPP,
                "whatsapp_access_token_secret_name is set, but per-product WhatsApp tokens are "
                "not implemented: every product sends with the shared token. Clear it.",
            )
        )
    return findings


def _conversion(facts: ProductFacts) -> list[Finding]:
    try:
        mode = parse_conversion_mode(facts.conversion_mode)
    except ValueError:
        return [
            _blocker(
                "conversion.mode_invalid",
                Area.CONVERSION,
                f"conversion_mode {facts.conversion_mode!r} is not recognised. Use "
                f"{ConversionMode.CALENDAR_BOOKING.value} or "
                f"{ConversionMode.EXTERNAL_REGISTRATION.value}.",
            )
        ]
    if mode is ConversionMode.EXTERNAL_REGISTRATION and not (
        facts.registration_url and facts.registration_url.strip()
    ):
        return [
            _blocker(
                "conversion.registration_url_missing",
                Area.CONVERSION,
                "conversion_mode is external_registration but no registration_url is set, "
                "so the assistant has nowhere to send interested contacts.",
            )
        ]
    return []


def _calendar(facts: ProductFacts) -> list[Finding]:
    # Booking products need a calendar. Registration-only products do not — unless they
    # still configured one, in which case uniqueness still applies.
    if not facts.google_calendar_id:
        if uses_calendar_booking(facts.conversion_mode):
            return [
                _blocker(
                    "calendar.missing",
                    Area.CALENDAR,
                    "No Google Calendar ID, so the assistant cannot offer or book demos.",
                )
            ]
        return []
    findings: list[Finding] = []
    if facts.calendar_also_on:
        findings.append(
            _blocker(
                "calendar.shared",
                Area.CALENDAR,
                f"Calendar {facts.google_calendar_id} is also configured on "
                f"{', '.join(facts.calendar_also_on)}. Each product books into its own "
                "calendar ([D43]).",
            )
        )
    if facts.meeting_url_also_on:
        findings.append(
            _blocker(
                "calendar.meeting_link_shared",
                Area.CALENDAR,
                f"The meeting link is also configured on "
                f"{', '.join(facts.meeting_url_also_on)}. Each product holds its own "
                "permanent room, or two products' demos land in one call.",
            )
        )
    elif uses_calendar_booking(facts.conversion_mode) and not facts.meeting_url:
        findings.append(
            _gap(
                "calendar.meeting_link_missing",
                Area.CALENDAR,
                "No meeting link is configured, so booked demos confirm with the time "
                "only. Configure the product's permanent Google Meet link to attach "
                "and share one with every demo.",
            )
        )
    return findings


def _slots(facts: ProductFacts, platform: PlatformInputs) -> list[Finding]:
    # Slot policy only matters when booking is in play: calendar-booking mode, or a
    # registration product that nonetheless configured a calendar.
    if not uses_calendar_booking(facts.conversion_mode) and not facts.google_calendar_id:
        return []
    try:
        resolve_slot_policy(platform.slot_settings, facts.slot_overrides)
    except SlotPolicyError as exc:
        return [
            _blocker(
                "slots.policy_invalid",
                Area.SLOTS,
                f"The slot policy does not resolve, so booking would be unavailable: {exc}",
            )
        ]
    return []


def _templates(facts: ProductFacts) -> list[Finding]:
    if facts.usable_marketing_templates == 0:
        return [
            _gap(
                "templates.none_usable",
                Area.TEMPLATES,
                "No Meta-approved MARKETING template is assigned to this product, so it cannot "
                "run campaigns. Synchronize templates, then assign the right ones to it.",
            )
        ]
    return []


def _knowledge(facts: ProductFacts, platform: PlatformInputs) -> list[Finding]:
    if not platform.retrieval_enabled:
        return []
    findings = []
    if facts.ready_documents == 0:
        findings.append(
            _gap(
                "knowledge.none_ready",
                Area.KNOWLEDGE,
                "No READY knowledge document. The assistant answers factual questions only "
                "with the fallback message.",
            )
        )
    if facts.failed_documents:
        findings.append(
            _gap(
                "knowledge.failed_documents",
                Area.KNOWLEDGE,
                f"{facts.failed_documents} knowledge document(s) failed ingestion; the "
                "assistant cannot use them.",
            )
        )
    return findings


__all__ = [
    "Area",
    "Finding",
    "PlatformInputs",
    "ProductFacts",
    "ProductReadiness",
    "Severity",
    "Verdict",
    "evaluate",
]
