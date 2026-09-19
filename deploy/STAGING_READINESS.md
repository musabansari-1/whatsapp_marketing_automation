# Staging and Production Readiness

Everything required before a deployment that exercises **real** integrations rather than
fakes. Updated for Phase 27: the platform serves three products, so this checklist is in two
parts — what the platform needs once, and what **each product** needs before it may be
activated.

Nothing here is a decision made on the product owner's behalf. Where a value is a business
input it is named and left blank. Where it is a credential, this says who issues it, where it
goes, and how to prove it arrived.

**Status key:** ☐ not supplied · ◐ partially supplied · ☑ verified

> A **fake-provider staging deployment needs none of this.** It runs end to end today and is
> the sensible first use of `deploy/`. This checklist gates the deployment *after* that one —
> the first where a mistake reaches a real person.

> **Nothing in `deploy/` may be executed without explicit approval (§23).** That includes the
> product configuration commands below when they target staging or production.

---

## Part 1 — Global infrastructure (once per environment)

### 1.1 Accounts and hosting

| ☐ | Item | Who | Notes |
|---|---|---|---|
| ☐ | Google Cloud project — **staging separate from production** | Us/client | `deploy/config.sh` |
| ☐ | Billing on that project | Client | Cloud Run, Tasks, Scheduler, Secret Manager all bill |
| ☐ | Neon PostgreSQL + `CREATE EXTENSION vector` | Us | Connection string → secret `database-url` |
| ☐ | Vercel project | Us | `INTERNAL_API_BASE_URL` → the Cloud Run API URL |
| ☐ | Test WhatsApp numbers we control | Us | The only numbers any real staging send may target |

### 1.2 Shared secrets and platform settings

| ☐ | Item | Goes to |
|---|---|---|
| ☐ | `APP_SECRET` (real value; production refuses the placeholder) | secret `app-secret` |
| ☐ | First ADMIN credentials, for `bootstrap-admin` only | `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD` |
| ☐ | Provider selections — `deploy.sh` defaults every external one to `fake` | `*_PROVIDER` |
| ☐ | Dispatch rate from the real messaging tier — D-05 | `QUEUE_MAX_DISPATCHES_PER_SECOND`, `DISPATCH_RATE_PER_SECOND` |
| ☐ | Platform slot-policy defaults confirmed — [D8] | `DEMO_*` (a product overrides only what differs) |
| ☐ | Customer copy reviewed — [D21] | `MSG_RAG_FALLBACK`, `MSG_BOOKING_CONFIRMATION`, `MSG_OPT_OUT_RESPONSE` |

**Retired variables.** `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_BUSINESS_ACCOUNT_ID`,
`GOOGLE_CALENDAR_ID`, `AUDIENCE_COLUMN_SCHEMA`, `QUALIFICATION_REQUIRED_FIELDS`,
`AI_SYSTEM_PROMPT` and `AI_KNOWLEDGE_BASE_NAME` are no longer read. Each product holds its own
values (Part 2). If an environment still sets them, `product-readiness` lists them, and
`import-legacy-config` is their only reader.

### 1.3 Meta App — shared by every product

One Meta Developer App, one shared WhatsApp Business Account today, one phone number per
product ([D40], [D50]).

| ☐ | Item | Goes to | Verified by |
|---|---|---|---|
| ☐ | **Permanent System User access token** (not a 24-hour test token) | secret `whatsapp-access-token` | Template sync returns rows |
| ☐ | **Verify token** (we choose it) | secret `whatsapp-verify-token` | `smoke-test.sh` handshake |
| ☐ | **App Secret** | secret `whatsapp-app-secret` | `smoke-test.sh` rejects an unsigned delivery |
| ☐ | Webhook subscribed to the Cloud Run API URL, field `messages` | Meta dashboard | Handshake succeeds |
| ☐ | Template variable mapping beyond variable 1 — D-02 | Code + configuration | A 3-variable template currently sends 2 empty |
| ☐ | Out-of-window fallback / booking confirmation template names — D-24 | `TEMPLATE_WINDOW_FALLBACK`, `TEMPLATE_BOOKING_CONFIRMATION` | Names only; content is Meta's |

Phone number IDs and business account IDs are **not** here: they are per product (Part 2).

⚠️ Template approval takes days. Start it first.

### 1.4 OpenRouter (LLM)

| ☐ | Item | Goes to | Verified by |
|---|---|---|---|
| ☐ | API key | secret `openrouter-api-key` | A real turn produces a reply |
| ☐ | **Production model** — D-20 | `OPENROUTER_MODEL` | No default; startup fails without it when the provider is `openrouter` |
| ☐ | Benchmark with real prompts and documents — D-20 | — | 3–4 candidates × ~20 conversations per product |
| ☐ | `CONVERSATION_HISTORY_TURNS`, `AI_MAX_TOOL_ROUNDS` reviewed — D-16, D-26 | configuration | A product may override either |
| ☐ | `RAG_MIN_SIMILARITY` tuned with real documents — D-09 | configuration | A product may override it |
| ☐ | Embedding model unchanged — [D1] | `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION` | One model for every product; changing it is a migration plus full re-embedding |

System prompts and qualification criteria are **not** here: they are per product (Part 2).

### 1.5 Google Calendar — shared operations account

| ☐ | Item | Goes to | Verified by |
|---|---|---|---|
| ☐ | Calendar API enabled | — | — |
| ☐ | **OAuth client** (Desktop app, Internal) — one for every product | `GOOGLE_OAUTH_CLIENT_ID`, secret `google-oauth-client-secret` | — |
| ☐ | **Refresh token — one human consent** — D-27 | secret `google-calendar-refresh-token` | `deploy/oauth-consent.md` |
| ☐ | Worker SA holds `secretmanager.secretVersionAdder` | IAM | Otherwise booking fails when Google rotates the token |
| ☐ | Each product's calendar **shared with the operations account** | Google Calendar | That product's slots reflect its diary |

Calendar IDs are per product (Part 2). No new OAuth client or token is needed per product
unless a calendar sits in a different Google account ([D43]).

⚠️ The consent flow cannot be automated or done by a service account.

### 1.6 HubSpot — DEFERRED / DISABLED FOR MULTI-PRODUCT

**Not a launch blocker.** The multi-product CRM design is deferred ([D58], planned Phase 28).
While more than one product row exists the CRM sync task skips every lead and writes nothing,
because the flat `wa_*` properties carry no product. Leave `CRM_PROVIDER=fake`. No HubSpot
token, property or portal work is needed for launch.

### 1.7 Knowledge upload limit

**At most ~700 KB per document** (D-08). V1 has no blob store, so the bytes travel inside the
Cloud Tasks message, and startup refuses a larger `KNOWLEDGE_MAX_FILE_BYTES` with Cloud Tasks
selected. **Check every real document against this before staging.** If a supplied document
exceeds it, stop and report — do not redesign ingestion unannounced.

### 1.8 Schema, catalog and readiness

In order, after `deploy.sh` has migrated (commands in `deploy/README.md`, *Product
configuration*; each needs approval):

```text
# migrate through a1b2c3d4e5f6 first — deactivates Academicae's Phase-17 active seed
configure-products --file config/products.json   products and segments; idempotent; never activates
import-legacy-config academicae                  once; Academicae's pre-Phase-27 environment values
configure-products (with real values added)      as each product's inputs arrive
product-readiness                                must exit 0 before any real provider is enabled
activate-product <slug>                          per product, when READY and approved
```

**No product is active until it is ready and its activation is approved** (product owner,
2026-09-12). Migration `a1b2c3d4e5f6` sets Academicae inactive by fixed ID (the Phase-17 seed
had left it active); `configure-products` never flips `is_active`. Academicae stays inactive,
alongside SustainX and Life Herba, until its real configuration passes `product-readiness`
and activation is explicitly approved. An inactive product receives no routed messages and
starts no campaign, reply or booking.

---

## Part 2 — Per product

### 2.1 How a product goes live

```text
create the product (catalog)        → inactive
configure segments                  → catalog
configure WhatsApp                  → phone number ID (unique) + business account ID (may be shared)
configure calendar                  → its own calendar ID
configure AI                        → product default; sparse segment overrides only where needed
configure audience schema           → the confirmed spreadsheet headers
sync and assign templates           → Meta-approved MARKETING templates, assigned explicitly
load knowledge                      → READY documents, each ≤ ~700 KB
product-readiness <slug>            → READY, or READY WITH GAPS accepted knowingly
activate-product <slug>             → explicit; never a side effect
```

Nothing missing falls back to another product. The runtime refuses regardless of readiness: an
unknown number is not routed, a product without policy does not answer, without a calendar it
does not book, without its own number it does not send, an unassigned template is unusable.

### 2.2 Checklist

| Item | Academicae | SustainX | Life Herba |
|---|---|---|---|
| Row, slug, display name | ☑ `academicae` — existing row, fixed ID | ◐ `sustainx` — in the catalog, created on first apply | ◐ `life-herba` — in the catalog, created on first apply |
| Active | **inactive (intended)** — seeded active; deactivate first | inactive | inactive |
| Segments | none — valid | ☑ `industries`, `institutions`, `government-bodies`, `students`, `professionals-executives` | ☑ `healthcare-practitioners`, `wellness-centres`, `pharmacies`, `consumers` |
| Audience column schema | ☐ import from its environment, then confirm the real headers (D-46b) | ☐ not supplied | ☐ not supplied |
| AI system prompt | ☐ import from its environment, then review | ☐ not supplied — one product-level prompt | ☐ not supplied — product default |
| Qualification criteria — [D37] | ☐ | ☐ | ☐ |
| Segment AI overrides | none | **none** — every segment shares one behaviour | ☐ sparse, only where a segment genuinely differs |
| WhatsApp phone number ID | ☐ | ☑ `1355444384311332` (catalog; own number) | ☑ `1340886312439722` (catalog; own number) |
| WhatsApp business account ID | ☐ | ☑ `1107203798409996` (catalog; own WABA) | ☑ `2034571340595521` (catalog; own WABA) |
| Google Calendar ID | ☐ | ☐ | ☐ |
| Slot policy | platform default unless an override is supplied | platform default; one calendar for all segments | platform default; no segment calendars |
| Templates: synced, assigned, APPROVED, MARKETING | ☐ | ☐ | ☐ |
| Knowledge: READY documents | ☐ | ☐ one shared product knowledge base | ☐ one shared ecosystem knowledge base (not one per segment) |
| `product-readiness` today | NOT READY | NOT READY | NOT READY |
| CRM | deferred | deferred | deferred |

### 2.3 Templates

1. Synchronize templates from Meta (the sync reads each business account a product is
   configured under).
2. Review the unassigned templates on any product's Templates page.
3. Assign each usable template to the product it belongs to — ADMIN, explicit, confirmed.
   Nothing infers ownership ([D57]).
4. Confirm Meta shows it APPROVED and MARKETING.
5. Only then create campaigns with it.

A product with no assigned, approved marketing template is **not campaign-ready**
(`templates.none_usable`). Campaign sending also re-checks the stored state at confirmation.
Templates Meta later deletes are not removed by the sync ([D57]); a send with one fails at
Meta and is recorded as a failed recipient.

### 2.4 Knowledge

Only the product owner's real documents. Nothing is generated or invented. One shared
knowledge base per product is the normal shape. Upload is ADMIN-only (D-07). A product with no
READY document answers factual questions only with the fallback (`knowledge.none_ready`).

### 2.5 Activation

`activate-product <slug>` runs the readiness check and refuses a NOT READY product. A product
READY WITH GAPS — typically no template or no knowledge yet — is activated only with
`--accept-gaps`. `deactivate-product <slug>` stops new customer activity; records stay
readable.

**Activation gate (product owner, 2026-09-12).** No product is activated until all of these
hold, and then only with explicit approval:

- its real WhatsApp phone number ID and business account are configured
- its Google Calendar is configured
- its audience column schema is confirmed
- its AI configuration is present
- its templates are synced and assigned
- its knowledge is uploaded and READY
- `product-readiness` passes for it

Templates and knowledge are part of the gate, so `--accept-gaps` is not a way around them for
these products.

---

## Part 3 — Business decisions

| ☐ | Decision | ID | Status |
|---|---|---|---|
| ☐ | **Qualification criteria, per product** | D-19 / [D37] | Open. Empty criteria mean no lead can reach QUALIFIED |
| ☑ | Pause semantics | D-01 | **Deferred** ([D61]). Cancel is the stop |
| ☑ | Knowledge upload permissions | D-07 | **ADMIN-only** ([D61]) |
| ◐ | Analytics | D-33 | Visibility decided: all internal users ([D61]). Analytics beyond the Overview not built |
| ☐ | Rescheduling behaviour | D-29 | Unimplemented. "Can we move it?" produces a second booking |
| ☐ | What closes a conversation | D-14 / D-24b | `CLOSED` honoured, never taken |
| ☐ | Out-of-24-hour-window behaviour | D-18 | Recorded; nothing sent |
| ☐ | Data retention windows | D-44 | Nothing purges message content |
| ☐ | Dashboard metric definitions | D-34 | Unconfirmed with the client |
| ☐ | May a human change a lead's status | D-13 | Allowed, validated and audited |

---

## Part 4 — Order of work

```text
Meta template submission, per product ─────┐   (days of review — start first)
Meta phone numbers, one per product ───────┤
Google OAuth consent (once) ───────────────┤
Calendar IDs, one per product ─────────────┤
Spreadsheet headers, per product ──────────┤
Prompts and criteria, per product ─────────┤
Knowledge documents, per product ──────────┘
                                           ↓
                   configure-products · import-legacy-config academicae
                                           ↓
                        OpenRouter benchmark (D-20) · D-09 tuning
                                           ↓
                        product-readiness — exit 0, reviewed
                                           ↓
                    First real-provider staging deployment (approval)
                                           ↓
               The E2E plan, one provider at a time, one product at a time
```

**Turn providers on one at a time, and products on one at a time.** WhatsApp first, then the
LLM, then Calendar. HubSpot stays deferred.

---

## Part 5 — Actions that need explicit approval

- Running anything in `deploy/` — infrastructure, secrets, deploy, sweeps, alerts, smoke test.
- Running the product configuration commands against staging or production databases.
- Activating a product in staging or production.
- Deactivating Academicae there: it is a configuration command, run at onboarding.
- Enabling any real provider (`WHATSAPP_PROVIDER=meta`, `LLM_PROVIDER=openrouter`,
  `CALENDAR_PROVIDER=google`).
- Subscribing the Meta webhook, submitting templates, or any Meta Business Manager change.
- The Google OAuth consent, and any real calendar event.
- Sending any real WhatsApp message.
