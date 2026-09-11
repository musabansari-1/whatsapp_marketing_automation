# Real Provider Staging Readiness

Everything required before the first staging deployment that exercises **real**
integrations rather than fakes.

Nothing here is a decision I have made. Where a value is a business choice it is named,
its checklist ID given, and the space left blank. Where it is a credential, this says
who issues it, which secret or variable receives it, and how to prove it arrived.

**Status key:** ☐ not supplied · ◐ partially supplied · ☑ verified against the real provider

> A **fake-provider staging deployment needs none of this.** It runs end to end today
> and is the sensible first use of `deploy/`. This checklist is the gate for the
> deployment *after* that one — the first where a mistake reaches a real person.

---

## 0. Before any of it — accounts

| ☐ | Item | Who | Notes |
|---|---|---|---|
| ☐ | Google Cloud project (staging) | Us/client | Separate project from production — see `deploy/config.sh` |
| ☐ | Billing enabled on that project | Client | Cloud Run, Tasks, Scheduler and Secret Manager all bill |
| ☐ | Neon PostgreSQL database + `CREATE EXTENSION vector` | Us | Connection string → secret `database-url` |
| ☐ | Vercel project | Us | `INTERNAL_API_BASE_URL` → the Cloud Run API URL |
| ☐ | A test WhatsApp number we control | Us | The only number any real send may target |

---

## 1. META (WhatsApp Cloud API)

Blocks: everything. Nothing is sent or received without it. **[D22]**, checklist D-22.

| ☐ | Item | Goes to | Verified by |
|---|---|---|---|
| ☐ | Meta App + WhatsApp Business Account (WABA) | — | Both IDs visible in the Meta dashboard |
| ☐ | **Access token** (permanent System User token, not a 24-hour test token) | secret `whatsapp-access-token` | Template sync returns rows |
| ☐ | **Phone number ID** | `WHATSAPP_PHONE_NUMBER_ID` | — |
| ☐ | **WABA ID** | `WHATSAPP_BUSINESS_ACCOUNT_ID` | Templates hang off the WABA, not the number ([D6]) |
| ☐ | **Verify token** (we choose the string) | secret `whatsapp-verify-token` | `smoke-test.sh` handshake check |
| ☐ | **App Secret** | secret `whatsapp-app-secret` | `smoke-test.sh` rejects an unsigned delivery |
| ☐ | Webhook subscribed to the Cloud Run API URL, fields `messages` | Meta dashboard | Handshake succeeds |
| ☐ | **Approved MARKETING template(s)** — D-23 | `GET /whatsapp-templates` | **No campaign can be created without one** |
| ☐ | **Approved UTILITY fallback template** (out-of-24-hour window) — D-24 | `TEMPLATE_WINDOW_FALLBACK` | Currently: outside the window, nothing is sent |
| ☐ | **Approved booking-confirmation template** — D-24 | `TEMPLATE_BOOKING_CONFIRMATION` | Needed only if confirmation may fall outside the window |
| ☐ | Template **variable mapping** beyond variable 1 — D-02 | Code + configuration | A 3-variable template currently sends 2 empty ones |
| ☐ | **Confirmed messaging tier / dispatch rate** — D-05 | `QUEUE_MAX_DISPATCHES_PER_SECOND` (queue) and `DISPATCH_RATE_PER_SECOND` (courtesy) | The queue value is the only true global ceiling |

⚠️ Template approval takes days. It is the longest lead time on this page — start it first.

**Business decisions still open here:** the template text itself ([D23] — Meta approves
content, we never invent it), and D-18: what happens to a customer who replies after 24
hours. Today the turn is recorded and nothing is sent.

---

## 2. OPENROUTER (LLM)

Blocks: every AI reply. **[D17]**, checklist D-20.

| ☐ | Item | Goes to | Verified by |
|---|---|---|---|
| ☐ | API key | secret `openrouter-api-key` | Any real turn produces a reply |
| ☐ | **Selected production model** — D-20 | `OPENROUTER_MODEL` | **No default exists; startup fails without it when `LLM_PROVIDER=openrouter`** |
| ☐ | **Benchmark completed** — D-20 | — | 3–4 candidates × ~20 representative conversations |
| ☐ | **System prompt** — D-21 | `AI_SYSTEM_PROMPT` | Customer-facing copy; currently a conservative placeholder |
| ☐ | `CONVERSATION_HISTORY_TURNS` reviewed — D-16 | configuration | Currently 20, a judgement |
| ☐ | `AI_MAX_TOOL_ROUNDS` reviewed — D-26 | configuration | Currently 4, a judgement |

### RAG (must be ready before the benchmark means anything)

| ☐ | Item | Goes to | Verified by |
|---|---|---|---|
| ☐ | **Real knowledge documents** — D-12 | Uploaded via the dashboard | Documents reach READY, not FAILED |
| ☐ | Which knowledge base the AI answers from — D-25 | `AI_KNOWLEDGE_BASE_NAME` | With several bases and this unset, the AI retrieves from none |
| ☐ | `RAG_MIN_SIMILARITY` tuned — D-09 | configuration | Currently 0.25, a guess. Tune with real documents |
| ☐ | Documents fit the ~700 KB upload cap — D-08 | — | A larger file needs object storage first |
| ☐ | Embedding configuration verified | `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION` | Must stay `all-MiniLM-L6-v2` / 384 — changing either is a migration **plus full re-embedding** |

⚠️ The embedding model runs in-process in the worker with the weights baked into the
image ([D1]). It needs **no credential and no third party** — it is the one integration
that is already real.

---

## 3. GOOGLE (Calendar)

Blocks: every booking. **[D7]**, **[D19]**, checklist D-27/D-28.

| ☐ | Item | Goes to | Verified by |
|---|---|---|---|
| ☐ | **Which calendar, per product** — D-28 | `products.google_calendar_id`, one per product | The platform treats *all* busy time on a product's calendar as unavailable for that product; a product with none refuses bookings |
| ☐ | Google Calendar API enabled on the project | — | — |
| ☐ | **OAuth client** (Desktop app, Internal) | `GOOGLE_OAUTH_CLIENT_ID`, secret `google-oauth-client-secret` | — |
| ☐ | **Refresh token — the one-time human consent** — D-27 | secret `google-calendar-refresh-token` | Follow `deploy/oauth-consent.md` |
| ☐ | Worker SA holds `secretmanager.secretVersionAdder` | IAM | Without it everything works until Google rotates the token, then fails at once |
| ☐ | **Timezone** | `DEMO_TIMEZONE` | Currently `Asia/Kolkata` |
| ☐ | **Availability policy** — [D8] | `DEMO_DURATION_MINUTES`, `DEMO_BUFFER_MINUTES`, `DEMO_WORKING_DAYS`, `DEMO_WORKING_HOURS_START/END`, `DEMO_BOOKING_HORIZON_DAYS` | Confirm each with the client; the defaults are ours |
| ☐ | Should the salesperson be an attendee — D-30 | — | Today the event is on the shared calendar and the customer is the only attendee |

⚠️ **The consent flow cannot be automated, scripted, or done by a service account.** A
person clicks Allow, once. Prefer a shared operations account: whoever consents is whose
access the platform inherits, and a deleted account stops booking.

---

## 4. HUBSPOT (CRM)

Blocks: nothing — §14 requires a CRM failure to be invisible to everything else. But
data is lost silently if the properties are missing. Checklist D-39.

| ☐ | Item | Goes to | Verified by |
|---|---|---|---|
| ☐ | Private App access token | secret `hubspot-access-token` | A synced contact appears |
| ☐ | Scopes: read + write contacts | HubSpot app | — |
| ☐ | **All eight custom contact properties created** — D-39 | HubSpot | **Open a synced contact and read the values back** |
| ☐ | Property mappings verified field by field | — | See below |

The eight properties, exactly as written:

```text
wa_lead_status      wa_lead_source      wa_campaign        wa_opted_out
wa_qualification    wa_demo_at          wa_demo_status     wa_last_synced_at
```

⚠️ **HubSpot silently ignores properties it does not recognise.** The write succeeds,
returns 200, and discards the data. There is no error to alert on. This is the single
most likely way an integration appears to work while losing half of what it sends —
reading one contact back after the first sync is the only reliable check.

**Business decisions still open here:** D-40 (our `wa_lead_status` overwriting a
salesperson's own judgement) and D-38 (whether the conversation transcript should ever
go to HubSpot — today it does not, deliberately).

---

## 5. EXCEL (audience)

Blocks: **production startup**, not staging. Checklist D-46b/D-06.

| ☐ | Item | Goes to | Verified by |
|---|---|---|---|
| ☐ | **A real client Excel file** | — | One is enough |
| ☐ | **Final column mapping** — D-46b | `AUDIENCE_COLUMN_SCHEMA` | **Production refuses to start with the development placeholder** |
| ☐ | **Required fields confirmed** — D-06 | — | `name` and `phone` are both required today; a row with a phone and no name is rejected. Confirm that is wanted |
| ☐ | Phone region confirmed | `AUDIENCE_PHONE_DEFAULT_REGION` | Currently `IN` |
| ☐ | A real upload previews cleanly | — | Upload → preview → check the invalid-row report before confirming |

The mapping is a translation exercise, not a design one: the six canonical fields
(`name`, `phone`, `email`, `company`, `designation`, `city`) never change — only the
client's header names do.

---

## 6. BUSINESS DECISIONS

None of these has been made. Each is listed with what the platform does *today*, so a
decision to leave it alone is also a decision.

| ☐ | Decision | ID | Today | Blocks |
|---|---|---|---|---|
| ☐ | **Qualification criteria** | D-19 / [D37] | `QUALIFICATION_REQUIRED_FIELDS` is empty, so **no lead can ever reach QUALIFIED** | Staging being meaningful |
| ☐ | **Analytics scope** | D-33 | Eight of §17's nine sections exist; Analytics' metrics *are* the Overview | The rest of Phase 14 |
| ☐ | **Rescheduling behaviour** | D-29 | Unimplemented. "Can we move it?" produces a **second** booking | Production |
| ☐ | Pause semantics — cancel, or resume | D-01 | No pause endpoint exists; the state is unreachable | Phase 14 completion |
| ☐ | ADMIN-only knowledge upload | D-07 | Any authenticated user may upload documents the AI will quote | Phase 14 completion |
| ☐ | What closes a conversation | D-14 / D-24b | `CLOSED` is honoured everywhere but **no code path takes it** | Production |
| ☐ | Out-of-24-hour-window behaviour | D-18 | Recorded; nothing sent | Production |
| ☐ | Data retention windows | D-44 | Nothing purges `webhook_events`, `ai_runs`, `conversation_messages` | Production |
| ☐ | The three dashboard metric definitions | D-34 | Defined in `analytics.py`; unconfirmed with the client | Staging sign-off |
| ☐ | May a human change a lead's status | D-13 | `PATCH /leads/{id}` allows it, validated and audited | Staging sign-off |

---

## 7. Order of work

Dependencies, not preferences:

```text
Meta template submission ─────────────┐   (days of Meta review — start first)
Google OAuth consent ─────────────────┤   (one human action, cannot be parallelised)
HubSpot properties ───────────────────┤
Client Excel file ────────────────────┤
Real knowledge documents ─────────────┘
                                      ↓
                            OpenRouter benchmark (D-20)
                                      ↓
                    D-19 qualification · D-09 similarity · D-21 prompt
                                      ↓
                          First real-provider staging deployment
                                      ↓
                              The E2E plan, one provider at a time
```

**Turn providers on one at a time.** Four real integrations enabled together means
debugging four at once, each unproven and each with its own failure mode. The order
above is also the order of risk: WhatsApp first (nothing works without it), then LLM,
then Calendar, then HubSpot — which, by §14's design, cannot break anything else.
