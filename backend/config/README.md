# Product configuration

`products.json` is the product catalog: which products exist, their segments, and — as each
value is confirmed — their spreadsheet format, AI policy and non-secret provider identifiers.
It is business configuration, applied by command, and kept apart from schema migrations.

```bash
uv run python -m app.cli configure-products --file config/products.json [--dry-run]
uv run python -m app.cli product-readiness [slug ...] [--json]
uv run python -m app.cli activate-product <slug> [--accept-gaps]
uv run python -m app.cli deactivate-product <slug>
uv run python -m app.cli import-legacy-config <slug> [--overwrite] [--dry-run]
```

None of these calls Meta, Google or HubSpot, and none sends a message.

## What the shipped catalog contains

Three product identities and the initial segments (confirmed 2026-09-12). Academicae also
holds the production values confirmed for first launch (2026-09-14): WhatsApp number and
WABA IDs, Google Calendar ID, school-audience column mapping, slot policy, and AI
configuration (`prompts/academicae.md`, `qualification_mode: ai_judgement`). SustainX still
has identity and segments only. Life Herba carries confirmed AI configuration, segment
overrides, `conversion_mode: external_registration` with registration URL/CTAs, and a
launch audience column schema (Name/Phone/Email/City) — but still invents no WhatsApp
number or calendar.

| Product | Slug | Segments | Launch config in catalog |
|---|---|---|---|
| Academicae | `academicae` (fixed ID `00000000-0000-4000-8000-000000000001`) | none | yes — inactive until readiness + explicit activation |
| SustainX | `sustainx` | `government`, `industry` | no |
| Life Herba | `life-herba` | `healthcare-practitioners`, `wellness-centres`, `pharmacies`, `consumers` | partial — AI + external registration + audience schema; no WhatsApp yet |

Slugs are permanent route identifiers (`/p/<slug>`). They are data: nothing in the code
branches on them.

**No product is active until it is ready and approved** (product owner, 2026-09-12). The
Phase-17 migration originally seeded Academicae active; revision `a1b2c3d4e5f6` sets that
row inactive by fixed ID, and `configure-products` never flips `is_active`. Activate a
product only after its real WhatsApp, calendar, audience schema, AI configuration,
templates and READY knowledge are in place, `product-readiness` passes, and activation is
explicitly approved. `--accept-gaps` is not a way around missing templates or knowledge.

## Rules

- **Idempotent.** Products and segments are matched by slug and updated in place. Re-running
  changes nothing; IDs, leads, audiences, campaigns, conversations and bookings survive.
- **Absent leaves alone, `null` clears.** Describe only what is known.
- **Activation is not configuration.** `is_active` is refused. New products start inactive;
  existing products keep their state. Activate with `activate-product`.
- **Unknown keys are refused**, including misspelled audience-schema keys.
- **An active product cannot be made less ready.** A catalog that would add a readiness
  blocker to an active product is refused whole.
- **Segments are never deleted** by the catalog. Unlisted segments are left alone.
- **No secrets.** Tokens stay in Secret Manager; only secret *names* may appear.

## Keys per product

| Key | Meaning |
|---|---|
| `slug`, `display_name` | required |
| `id` | optional; must match the existing row (keeps Academicae's fixed ID) |
| `segments` | `[{slug, display_name, display_order?, is_active?}]` |
| `audience_column_schema` | `[{source, field, required}]` — `field` is one of `name`, `phone`, `email`, `company`, `designation`, `city`; `name` and `phone` must be mapped and required |
| `whatsapp_phone_number_id` | the product's own number under the WABA; unique per product |
| `whatsapp_business_account_id` | the WABA; may be shared by every product |
| `google_calendar_id` | the product's own calendar; unique per product |
| `google_calendar_token_secret_name` | only for a calendar in a different Google account |
| `demo_duration_minutes`, `demo_buffer_minutes`, `demo_working_days`, `demo_working_hours_start`, `demo_working_hours_end`, `demo_booking_horizon_days`, `demo_min_lead_days`, `demo_timezone` | slot-policy overrides; omit to inherit the platform value. `demo_booking_horizon_days` may be `"unbounded"` (stored as `-1`); `null` still clears/inherits. `demo_min_lead_days` is 0 = today still offerable, 1 = earliest tomorrow |
| `conversion_mode` | `calendar_booking` (default) or `external_registration`. Registration-only products do not need a Google Calendar for readiness |
| `registration_url` | shared destination when `conversion_mode` is `external_registration` |
| `ai_configuration` | the product default: `system_prompt` (or `system_prompt_file`, relative to this file), `qualification_mode` (`required_fields` or `ai_judgement`), `qualification_required_fields`, `fallback_message`, and optional operational overrides `model`, `rag_top_k`, `rag_min_similarity`, `rag_max_context_chars`, `history_turns`, `max_tool_rounds` |
| `segment_ai_overrides` | `{segment_slug: {...only the fields that differ...}}`; `null` removes an override |
| segment `cta_label`, `cta_instruction` | optional per-segment CTA copy for external-registration products |

SustainX has one product-level `ai_configuration` and no `segment_ai_overrides` once configured.
Life Herba has one product default, sparse segment prompt overrides, and per-segment CTAs.

## Life Herba planned Meta template names

Names only — **not** approved, **not** seeded, **not** assigned. Flow remains Meta approval →
template sync → explicit Life Herba ownership assignment ([D57]).

| Segment | Planned template name |
|---|---|
| Healthcare Practitioners | `lifeherba_healthcare_practitioner_onboarding` |
| Wellness Centres | `lifeherba_wellness_centre_onboarding` |
| Pharmacies | `lifeherba_pharmacy_onboarding` |
| Consumers | `lifeherba_consumer_outreach` |

Do not invent alternate or shortened planned names.

## Example of a later addition (not real values)

```json
{
  "slug": "sustainx",
  "display_name": "SustainX",
  "whatsapp_phone_number_id": "<from Meta>",
  "whatsapp_business_account_id": "<from Meta>",
  "google_calendar_id": "<from Google>",
  "audience_column_schema": [
    { "source": "<header>", "field": "name", "required": true },
    { "source": "<header>", "field": "phone", "required": true }
  ],
  "ai_configuration": {
    "system_prompt_file": "prompts/sustainx.md",
    "qualification_required_fields": ["<confirmed field>"]
  }
}
```

## Academicae's pre-Phase-27 configuration

Before Phase 27 Academicae's prompt, criteria, spreadsheet format and WhatsApp number came
from `AI_SYSTEM_PROMPT`, `QUALIFICATION_REQUIRED_FIELDS`, `AUDIENCE_COLUMN_SCHEMA`,
`WHATSAPP_PHONE_NUMBER_ID` and `WHATSAPP_BUSINESS_ACCOUNT_ID`. The platform no longer reads
them. Copy them once, in an environment that still has them:

```bash
uv run python -m app.cli import-legacy-config academicae --dry-run
uv run python -m app.cli import-legacy-config academicae
```

An unset variable copies the built-in default the platform ran with, and says so. A value
Academicae already holds that differs is a conflict; nothing is written without `--overwrite`.
