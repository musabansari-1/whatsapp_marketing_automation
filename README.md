# WhatsApp AI Marketing Platform

Internal WhatsApp marketing platform. See `SPECIFICATION.md` for the product and
architecture source of truth, `CLAUDE.md` for development rules, and
`PROJECT_STATUS.md` for the current implementation state.

**Current state: Phase 9 (Conversations) complete.** The marketing pipeline runs end
to end — upload an audience, prepare a campaign, click Send Broadcast, and the worker
delivers each message with retries, throttling and cancellation. A customer's reply now
flows all the way through: webhook → task → worker → generated reply → delivery, with
the transcript, states and 24-hour window enforced. Documents can be uploaded to a
knowledge base and are embedded locally into pgvector. **The replies come from
`FakeLLMProvider`** — the real model, retrieval and tools arrive in Phase 10 — and
sending still needs real Meta credentials and approved templates.

## Layout

```text
backend/
  app/
    api/            FastAPI service (Cloud Run) — HTTP layer only
    worker/         Worker service (Cloud Run) — background task execution
    core/           Configuration, logging, correlation IDs, error taxonomy
    domain/         Business rules — state enums, auth policy
    persistence/    Database engine and session management
    integrations/   The six provider abstractions, each with a fake
  alembic/          Migrations
  tests/
    integration/    Tests requiring real PostgreSQL (skip when unavailable)
frontend/
  app/              Next.js App Router
    api/[...path]/  Server-side BFF proxy — the browser's only route to the API
    login/          Login page
  lib/              Proxy, session and route-protection helpers
  middleware.ts     Redirects unauthenticated navigation to /login
  tests/
compose.yaml        Local Postgres (pgvector) + API + worker
.env.example        Every configuration variable
```

Two Cloud Run services, deliberately separate: the API must never do long-running work
(§4), and the worker will carry the embedding model while the API stays slim.

## Prerequisites

Python 3.11+, Node 20+, Docker (for local Postgres), and [uv](https://docs.astral.sh/uv/).

## Local setup

```bash
cp .env.example backend/.env

# Database (pgvector-enabled)
docker compose up -d postgres

# Backend
cd backend
uv sync
uv run alembic upgrade head

# Create the first ADMIN (there is no public registration).
# Idempotent: re-running it when an admin exists changes nothing.
export BOOTSTRAP_ADMIN_EMAIL=you@example.com
export BOOTSTRAP_ADMIN_PASSWORD='a-long-password'
uv run python -m app.cli bootstrap-admin

uv run uvicorn app.api.main:app --reload --port 8000
uv run uvicorn app.worker.main:app --reload --port 8001   # separate terminal

# Frontend
cd frontend
npm install
echo "INTERNAL_API_BASE_URL=http://localhost:8000" > .env.local
npm run dev
```

Frontend on <http://localhost:3000>, API on <http://localhost:8000>.

Defaults select **fake providers**, so the stack runs with no credentials and cannot
reach any external service.

**Port 5432 already in use?** A locally installed PostgreSQL service will shadow the
container's published port and produce confusing `password authentication failed`
errors. Pick a free port instead:

```bash
echo "POSTGRES_PORT=5442" > .env       # repository root
docker compose up -d postgres
# then point DATABASE_URL at localhost:5442 in backend/.env
```

## Database

```bash
cd backend
uv run alembic upgrade head              # apply migrations
uv run alembic downgrade base            # roll everything back
uv run alembic check                     # fail if models have drifted from migrations
uv run alembic revision --autogenerate -m "description"
```

20 tables: the 18 in specification §15, plus `whatsapp_suppressions` ([D16]) and
`sessions` ([D2], a documented deviation — see §15 of the specification). The
`knowledge_chunks.embedding` column is `vector(384)`, matching `all-MiniLM-L6-v2`
([D1]) and driven by `EMBEDDING_DIMENSION`. Changing the embedding model means a new
migration plus a full re-embed — vectors from different models must never share the
column.

On Neon, run migrations against the **direct** endpoint and the application against
the **pooled** one.

### Integration tests

Tests under `backend/tests/integration/` need a real PostgreSQL with pgvector and skip
when none is reachable. Create the test database once:

```bash
docker compose exec postgres psql -U postgres -c "CREATE DATABASE whatsapp_marketing_test;"
TEST_DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5442/whatsapp_marketing_test" \
  uv run pytest tests/integration
```

## Checks

```bash
# Backend
cd backend
uv run pytest                    # tests
uv run ruff check .              # lint
uv run ruff format --check .     # formatting
uv run mypy app tests            # types (strict)

# Frontend
cd frontend
npm test                         # tests
npm run lint                     # lint
npm run typecheck                # types
npm run format:check             # formatting
npm run build                    # production build
```

## Authentication

Secure HttpOnly session cookies ([D2]); no JWT. Passwords are hashed with **Argon2id**.
Sessions are server-side, so logout and account deactivation revoke access immediately
rather than waiting for expiry — only the SHA-256 digest of each token is stored.

Two roles: `ADMIN` and `SALESPERSON`. There is **no public registration** — the first
admin comes from the bootstrap command above, and every other account is created by an
admin through `POST /api/v1/users`.

```text
POST   /api/v1/auth/login     email + password  → session cookie
POST   /api/v1/auth/logout    revokes the session
GET    /api/v1/auth/me        the current user
POST   /api/v1/users          ADMIN only
GET    /api/v1/users          ADMIN only
GET    /api/v1/users/{id}     ADMIN only
PATCH  /api/v1/users/{id}     ADMIN only
```

The browser reaches all of these through the same-origin BFF proxy ([D13]), never
directly. The API sets the cookie without a `Domain` attribute so the proxy can pass it
through and the browser scopes it to the frontend origin.

## Audiences

Upload an .xlsx, review the preview, then confirm. Uploading validates and stores
only — it never sends a message and never invokes the AI (§6).

```text
GET    /api/v1/audiences/schema        the expected columns
POST   /api/v1/audiences/upload        validate + store, returns the preview
GET    /api/v1/audiences               list
GET    /api/v1/audiences/{id}          detail
GET    /api/v1/audiences/{id}/contacts paged rows, filterable by status
POST   /api/v1/audiences/{id}/confirm  confirm, upserting valid rows into leads
```

### Canonical schema

```text
name          required
phone         required
email         optional
company       optional
designation   optional
city          optional
```

These fields are fixed ([D25]); only the mapping from the client's spreadsheet headers
varies, and that is configuration. All six are carried onto the lead at confirmation —
promotion discards nothing.

Rows are marked `VALID`, `INVALID` or `DUPLICATE` and **all three are kept**, so the
preview can show what was rejected and why. Phone numbers are normalized to E.164
using `AUDIENCE_PHONE_DEFAULT_REGION` (`IN` by default), and duplicates are detected
after normalization, so two spellings of one number are caught.

### The column schema is configuration

`AUDIENCE_COLUMN_SCHEMA` maps the client's spreadsheet headers onto the canonical
fields:

```json
[{ "source": "Full Name",     "field": "name",        "required": true },
 { "source": "Mobile Number", "field": "phone",       "required": true },
 { "source": "Email ID",      "field": "email",       "required": false },
 { "source": "Organization",  "field": "company",     "required": false },
 { "source": "Job Title",     "field": "designation", "required": false },
 { "source": "Location",      "field": "city",        "required": false }]
```

The value shipped in `.env.example` is a **development placeholder**, not a client
format. Production refuses to start while it is still in place, so the pipeline could
be built before the client confirmed their columns without inventing names ([D4],
[D20]). Swapping in the real headers needs no code change.

`name` and `phone` must both be mapped and marked required. Unmapped columns are still
preserved in each row's stored `raw_row`.

## Campaigns

```text
GET    /api/v1/whatsapp-templates            approved marketing templates (read-only)
POST   /api/v1/campaigns                     create + freeze the recipient snapshot
GET    /api/v1/campaigns                     list
GET    /api/v1/campaigns/{id}                detail, with template preview
GET    /api/v1/campaigns/{id}/recipients     the frozen snapshot, paged
POST   /api/v1/campaigns/{id}/send           the Send Broadcast confirmation
```

Creating a campaign **prepares** it: the recipient list is frozen at creation so later
audience changes cannot alter a campaign already made (§7), and numbers on the global
suppression list are recorded `SKIPPED` rather than `PENDING` ([D16]). Sending is a
separate, explicit confirmation that re-checks suppression, enqueues one task per
pending recipient and returns — the worker does the actual delivery (§4).

**Nothing is sent yet.** No handler is registered for the send task until Phase 7, and
the WhatsApp integration itself is Phase 6, so the queue records the work and stops
there.

**Templates come from Meta, not from here.** There is no endpoint that creates or
approves a template ([D6]); `whatsapp_templates` is populated by the Phase 6
synchronization. Until that exists the list is empty and no campaign can be created.

## WhatsApp

```text
GET    /api/v1/webhooks/whatsapp    Meta's subscription handshake
POST   /api/v1/webhooks/whatsapp    inbound messages and delivery statuses
```

Both bypass the BFF — Meta calls them directly — so they authenticate themselves. The
GET checks `WHATSAPP_VERIFY_TOKEN`; the POST verifies an HMAC-SHA256 signature over
the raw body using the Meta **App Secret**. An unsigned or wrongly-signed delivery is
rejected and never stored.

The POST does the minimum: verify, persist, enqueue, return. Everything else happens
in the worker, because Meta retries aggressively and a webhook that works inline turns
a slow database into a flood of duplicate deliveries. A redelivery is reported as a
duplicate rather than an error.

**Inbound handling** creates the lead and conversation if needed (a customer can
message without ever being in an audience), stores the message, and opens the 24-hour
messaging window.

**Opt-out** ([D5], [D16]): a message matching `OPT_OUT_KEYWORDS` writes a
phone-level suppression row, marks the lead, cancels any pending or queued campaign
sends to that number, and acknowledges the customer. Matching is on the whole message,
so "STOP" opts out but "please don't stop sending these" does not.

**Delivery statuses** update both the message and its campaign recipient. Out-of-order
webhooks cannot walk a status backwards — a late `delivered` will not overwrite `read`.

**Templates** are synchronized from Meta into `whatsapp_templates` with their status
carried through unchanged. Campaigns filter on that value; nothing here decides
approval ([D6]).

### The 24-hour messaging window

Meta only permits free-form replies within 24 hours of the customer's last inbound
message. `conversations.last_inbound_at` records when the window opened and
`CUSTOMER_SERVICE_WINDOW_HOURS` configures its length.

**Still outstanding**: the approved *utility* templates to use when the window has
closed have not been identified, so a reply sent outside the window cannot currently
be delivered. This was flagged in the Phase 0 report and is not addressed in the
original specification.

## Asynchronous dispatch

Send Broadcast enqueues one task per recipient; the worker delivers them.

```text
ADMIN clicks Send Broadcast
 ↓
one Cloud Task per pending recipient (deterministic name = idempotent)
 ↓
Cloud Run worker, authenticated by Google-issued OIDC
 ↓
re-check campaign state and suppression, then send
```

**Retries** ([D27]): 5 attempts, exponential backoff with jitter, all configurable.
The three failure classes behave differently — transient retries, permanent fails
immediately, rate-limited backs off. The worker signals a retry with `503` plus
`Retry-After`; any `2xx` tells Cloud Tasks the task is finished.

**Idempotency** is layered, because each layer catches a different failure: Cloud
Tasks refuses a duplicate task name, the recipient row is locked and its status
checked, and the outbound message carries a unique key so a worker that crashed after
sending recovers rather than re-sends.

**Throttling** ([D28]): `DISPATCH_RATE_PER_SECOND` spreads tasks over time so a large
campaign trickles rather than floods. It has **no default** — assuming a Meta
messaging tier is exactly what the decision forbids.

**Cancellation** ([D29]): `POST /api/v1/campaigns/{id}/cancel` (ADMIN-only) stops
unsent recipients, and the worker re-checks campaign state before every send, so tasks
already dispatched send nothing. Idempotent. Messages already delivered cannot be
recalled.

A campaign with some failed recipients ends `COMPLETED`; `sent_count` and
`failed_count` carry the breakdown. There is no `PARTIAL` state.

## Knowledge base (RAG)

Documents live in named knowledge bases and are retrieved by meaning, not keyword.
Endpoints, all under `/api/v1/knowledge-bases` and all requiring a session:

```text
POST   /                                     create a knowledge base
GET    /                                     list them
GET    /{id}                                 one knowledge base
POST   /{id}/documents                       upload (multipart)
GET    /{id}/documents                       list documents
GET    /{id}/documents/{doc}                 one document, with ingestion status
POST   /{id}/documents/{doc}/archive         withdraw from retrieval
```

Supported formats are `.txt`, `.md` and `.pdf`, capped by `KNOWLEDGE_MAX_FILE_BYTES`
(700 KB — V1 sends the bytes inside the Cloud Tasks payload, which is limited to 1 MB;
startup refuses a larger limit rather than letting an accepted upload fail at enqueue
time). A PDF with no text layer is rejected as needing OCR rather than stored as an
empty document, and re-uploading identical bytes to the same knowledge base is a 409 —
duplicate chunks would skew every later search.

Upload returns immediately with the document `PENDING`; ingestion runs in the worker
(§4 keeps slow work out of requests):

```text
parse → clean → chunk → embed → store vectors → READY
```

Only a `READY` document is retrievable. Failures are recorded on the document with a
reason, so the dashboard can show why a file did not ingest. Re-running an ingestion
task replaces the document's chunks rather than appending, which is what makes a
redelivered task idempotent.

**Embeddings run locally** ([D1]) via `all-MiniLM-L6-v2` — no per-request cost, no rate
limit, and no third party seeing company documents. The model accepts 256 word-pieces
and *silently truncates* beyond that, which is why `RAG_CHUNK_CHARS` defaults to 800
rather than being a free parameter. Weights are baked into `Dockerfile.worker` at build
time, so a cold-starting instance does not download them.

Every chunk records the model that produced it, and search filters on it: cosine
distance between two different models' vectors is arithmetically valid and
semantically meaningless.

**Retrieved text is untrusted data.** Before it reaches a prompt it is fenced in
explicit delimiters and attributed to its source; delimiter look-alikes in both text
and titles are replaced so no document can close the block early, and lines shaped like
role markers (`System:`) are defanged in place — neutralised but still legible, so an
operator reviewing a bad answer can see what the document actually said.

There is deliberately **no retrieval endpoint**. Retrieval is a backend tool the AI
calls in Phase 10, which keeps it inside the authorization model.

## Conversations

A customer's reply is one unit of work. The webhook stores it and queues one
`PROCESS_CONVERSATION_TURN` task per message; the worker does the rest:

```text
already replied? → conversation closed? → suppressed? → inside the 24-hour window?
 ↓
record the customer's turn
 ↓
ask the LLM with the recent transcript
 ↓
send the reply, record it, move the state
```

Each check prevents a specific failure and the order matters. Idempotency comes first
because Cloud Tasks redelivers and a second reply is a duplicate message to a real
person; suppression is re-checked before the model call because consent may have been
withdrawn after the task was queued ([D16]). The reply's idempotency key is derived
from the triggering message, so a retry computes the same key and the unique constraint
refuses the second write.

Outside the 24-hour window nothing is sent — §8 allows only an approved template there
— but the customer's turn is still recorded, so the dashboard shows what was missed
rather than losing it.

**States are the backend's decision, never the model's.** `app/domain/conversation.py`
holds the §10 transition tables, and every status change goes through them, whether it
comes from the worker or from a human using `PATCH /leads/{id}`. The most important
edge is the missing one: nothing reaches `QUALIFIED` except from `QUALIFYING`, so a
confident model cannot declare a lead qualified. What justifies that step is a Phase 10
decision ([D37]) and is deliberately not encoded yet.

Endpoints:

```text
GET   /api/v1/leads                        filter by status, search name/phone/email/company
GET   /api/v1/leads/{id}
PATCH /api/v1/leads/{id}                   correct details; status validated against §10
GET   /api/v1/leads/{id}/conversation      most recent thread, open or closed
GET   /api/v1/conversations                by most recent activity
GET   /api/v1/conversations/{id}           with transcript and window status
```

`PATCH` cannot write `qualification_data` (the AI's extraction target, [D18]) or
`opted_out` (consent, which changes only through the opt-out flow). There is
deliberately **no endpoint that sends a message** into a conversation: the worker is
where the window and suppression checks live.

In Phase 9 the replies come from `FakeLLMProvider`. A tool call from the model is
recorded and ignored — the authorized six arrive in Phase 10 — so the audit trail stays
honest about what was asked for versus what ran.

## Provider abstractions

Six external concerns sit behind interfaces, each with a fake used by the test suite.
Selection is by configuration, so no layer above `app/integrations/` names a concrete
provider.

| Concern   | Interface           | Fake                      | Real implementation      |
| --------- | ------------------- | ------------------------- | ------------------------ |
| WhatsApp  | `WhatsAppProvider`  | `FakeWhatsAppProvider`    | Meta — Phase 6           |
| Queue     | `QueueProvider`     | `InMemoryQueueProvider`   | Cloud Tasks — Phase 7    |
| Embedding | `EmbeddingProvider` | `FakeEmbeddingProvider`   | `LocalSentenceTransformerProvider` |
| LLM       | `LLMProvider`       | `FakeLLMProvider`         | OpenRouter — Phase 10    |
| Calendar  | `CalendarProvider`  | `FakeCalendarProvider`    | Google Calendar — Phase 11 |
| CRM       | `CRMProvider`       | `FakeCRMProvider`         | HubSpot — Phase 12       |

Selecting a real provider before its phase raises `ProviderNotImplementedError` naming
the phase, so a misconfiguration fails at startup rather than silently doing nothing.

## Deployment target

```text
Browser → Vercel (Next.js + BFF proxy) → Cloud Run API → PostgreSQL/Neon
                                         Cloud Run API → Cloud Tasks → Cloud Run Worker
Meta webhook ─────────────────────────→ Cloud Run API   (bypasses the BFF)
```

The frontend is never deployed to Cloud Run. Only the API and worker are containerized.
