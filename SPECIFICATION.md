# WhatsApp AI Marketing Platform — Complete Specification
Version 4 — Consolidated

Version 4 incorporates the product owner's resolution of the 12 open decisions raised in the Phase 0 readiness report, plus the second-pass architectural resolutions (both decided 2026-08-17). Resolved decisions are marked **[D1]**–**[D21]** at the point they apply and are summarized in §27.

## 1. Product Goal

Build an internal WhatsApp marketing platform that lets an authorized salesperson/admin:

1. Upload an Excel audience in a predefined format.
2. Validate and manage the audience.
3. Create and manually trigger a WhatsApp broadcast campaign.
4. Send messages asynchronously through Meta WhatsApp Cloud API.
5. Receive customer replies through WhatsApp webhooks.
6. Process conversations with an AI agent.
7. Use RAG to answer approved company/product/service questions.
8. Qualify interested leads.
9. Offer and schedule demos through Google Calendar.
10. Notify the salesperson/admin through the dashboard.
11. Create the Google Calendar event.
12. Confirm the booking to the customer through WhatsApp.
13. Synchronize relevant information with HubSpot.
14. Provide an internal dashboard for campaigns, leads, conversations, bookings, knowledge base and notifications.

The architecture is event-driven using Google Cloud Run and Google Cloud Tasks.

## 2. Scope

### Included
- Authentication and authorization
- Excel audience upload/validation
- Audience management
- Campaign management
- WhatsApp Cloud API
- WhatsApp templates
- Async broadcast sending
- Delivery/read/failure tracking
- WhatsApp webhooks
- Conversation management
- OpenRouter AI
- RAG knowledge base
- Document ingestion and embeddings
- pgvector retrieval
- Lead qualification
- Google Calendar availability and booking
- Dashboard booking notifications
- Customer WhatsApp confirmation
- HubSpot synchronization
- Dashboard and analytics
- Logging, retries and idempotency
- Automated tests
- Production deployment architecture

### V1 explicitly excludes
- Salesperson email notifications
- Salesperson WhatsApp notifications
- Automated post-demo follow-ups
- Autonomous lead discovery
- HubSpot as the source of the marketing audience
- Unapproved AI-generated marketing templates
- Complex recurring campaign automation
- Unrestricted autonomous AI
- Multi-tenant SaaS architecture unless separately requested

## 3. Users

### Internal user
Uses the dashboard to upload audiences, create campaigns, trigger broadcasts, monitor campaigns, view leads/conversations/bookings, manage the knowledge base and receive dashboard notifications.

Roles **[D2]**:

```text
ADMIN
SALESPERSON
```

There is no public registration. An ADMIN creates and manages internal users. Role-based authorization is required.

Users have an active/inactive state. Inactive users cannot authenticate and do not receive notifications.

### Customer
Uses WhatsApp only to receive marketing messages, reply, ask questions, qualify, request a demo and select a demo slot.

## 4. Architecture

```text
Dashboard (Next.js)
        |
        v
API Service (FastAPI / Cloud Run)
        |
   +----+----------------+
   |                     |
   v                     v
PostgreSQL/Neon      Google Cloud Tasks
+ pgvector                |
                           v
                    Worker (Cloud Run)
                           |
          +----------------+----------------+
          |                |                |
          v                v                v
      WhatsApp         OpenRouter       Calendar
          |                |                |
          |             RAG/pgvector       |
          +----------------+----------------+
                           |
                           v
                        HubSpot
```

Use two Cloud Run services:
- API service
- stateless worker service

Long-running work must not happen inside HTTP requests/webhooks.

## 5. End-to-End Marketing Flow

```text
Excel
 ↓
Audience validation
 ↓
Admin confirms audience
 ↓
Campaign
 ↓
Approved WhatsApp template
 ↓
Admin confirms Send Broadcast
 ↓
Cloud Tasks
 ↓
Worker
 ↓
WhatsApp Cloud API
 ↓
Customer reply
 ↓
WhatsApp webhook
 ↓
Cloud Tasks
 ↓
AI worker
 ↓
RAG when knowledge is required
 ↓
Lead qualification
 ↓
Demo request
 ↓
Google Calendar availability
 ↓
Customer selects slot
 ↓
Availability rechecked
 ↓
Calendar event created
 ↓
Booking saved
 ├── Dashboard notification → salesperson/admin
 ├── Google Calendar event → salesperson/admin
 └── WhatsApp confirmation → customer
 ↓
HubSpot synchronization
```

## 6. Excel Audience

Pipeline:

```text
Upload
 ↓
File validation
 ↓
Required-column validation
 ↓
Row parsing
 ↓
Phone normalization
 ↓
Email validation
 ↓
Duplicate detection
 ↓
Invalid rows
 ↓
Preview
 ↓
Admin confirmation
 ↓
Audience stored
```

Store original row number, validation status, validation error and normalized values.

Uploading an Excel file must not send messages or trigger AI. Upload must never automatically trigger a broadcast.

Exact client-defined columns must be confirmed before Phase 4 and must not be invented **[D4]** **[D20]**. No production column names are to be invented in the meantime. The column schema must be defined in configuration so the pipeline can be pointed at the confirmed schema without code changes.

Regardless of the final column set, the pipeline must support:
- required-column validation
- optional-column validation
- phone normalization
- duplicate detection
- invalid-row reporting
- preview before import

Phone normalization defaults to Indian numbers (region `IN`) unless the client specifies a different audience geography. The default region is configuration.

## 7. Campaigns

States:

```text
DRAFT
READY
SCHEDULED
PROCESSING
PAUSED
COMPLETED
FAILED
CANCELLED
```

Campaign fields include name, audience, approved WhatsApp template, creator, status, timestamps and metrics.

Recipient records include campaign, lead, status, attempts, WhatsApp message ID, errors and timestamps.

Use a recipient snapshot so later audience changes do not unexpectedly change an existing campaign.

Marketing broadcast is human initiated:
Upload audience → create campaign → preview → confirm Send Broadcast.

Actual sending is automated asynchronously.

## 8. WhatsApp

Use Meta WhatsApp Cloud API through a provider abstraction:

```text
WhatsAppProvider
 ├── MetaWhatsAppProvider
 └── FakeWhatsAppProvider
```

Track:
- internal message ID
- provider message ID
- lead
- campaign
- status
- timestamps
- attempts
- errors

Statuses:

```text
QUEUED
SENT
DELIVERED
READ
FAILED
```

Webhook handling:
- verification
- inbound messages
- delivery/read status
- failures
- duplicate-event protection

Webhooks must persist/queue work and return quickly.

Respect eligibility, opt-outs, invalid numbers and applicable provider messaging rules.

Automated tests must never send real WhatsApp messages.

### Templates **[D6]**

Meta-approved templates are the source of truth. The application synchronizes approved template metadata from Meta into `whatsapp_templates`.

The application must never invent or locally assign a Meta approval status. Campaigns may only use templates known to be approved, and marketing broadcasts must use the appropriate approved template type for marketing messages.

Meta template synchronization is implemented in the WhatsApp integration phase (Phase 6), not earlier.

Respect WhatsApp's messaging-window and template requirements.

### Opt-out **[D5]**

Opt-out is global at the normalized phone/lead level. There are no campaign-specific opt-outs.

When a customer opts out:
- record the suppression in `whatsapp_suppressions`, keyed by normalized E.164 phone **[D16]**
- mark the lead as opted out
- prevent future marketing broadcasts to that number
- prevent campaign workers from sending to that number
- retain the opt-out state across campaigns

`whatsapp_suppressions` is authoritative and survives independently of any lead row. Campaign sending must check it before sending; `lead.opted_out` alone is not sufficient (§15 **[D16]**).

Recognized opt-out keywords are configuration.

### Customer-facing content **[D21]**

Exact customer-facing copy is configurable content, not hard-coded strings. The implementation must support configurable:
- RAG insufficient-evidence fallback message
- booking confirmation message
- opt-out response message
- other approved messages and templates

Do not invent Meta-approved WhatsApp template names or content. Where a message must be sent as an approved template, the template identity comes from the Meta synchronization in **[D6]**.

## 9. AI Agent

Provider **[D10]**:

```text
LLMProvider
├── OpenRouterLLMProvider
└── FakeLLMProvider
```

OpenRouter is the LLM gateway. `FakeLLMProvider` is used in automated tests.

The model is configuration (`OPENROUTER_MODEL`) and must not be hard-coded anywhere in the application. V1 uses a low-cost capable model.

No specific model is chosen yet **[D17]**. Inexpensive capable models will be benchmarked during Phase 10 against representative conversations. The application must have no default model baked in and should fail fast if `OPENROUTER_MODEL` is unset.

The AI handles:
- customer understanding
- approved knowledge questions
- structured information extraction
- demo discussion
- authorized tool requests

The AI must not hallucinate business facts. When RAG retrieval does not provide sufficient verified information, the AI must use an approved fallback response rather than inventing an answer.

The AI is responsible for conversation understanding, extraction and orchestration. The backend remains the source of truth for state and business rules.

The AI must not directly access arbitrary databases or external APIs.

Authorized tools:

```text
get_lead
update_lead
get_conversation
get_available_slots
book_demo
send_whatsapp_message
```

The backend remains the source of truth for state.

## 10. Conversation and Lead State

Conversation:

```text
ACTIVE
WAITING_FOR_USER
BOOKING
CLOSED
```

Lead:

```text
NEW
CONTACTED
RESPONDED
INTERESTED
QUALIFYING
QUALIFIED
DEMO_OFFERED
DEMO_BOOKED
NOT_INTERESTED
OPTED_OUT
INVALID
FAILED
```

The AI cannot arbitrarily invent state transitions.

### Qualification **[D9]**

The LLM must not arbitrarily determine whether a lead is qualified.

Division of responsibility:
- The AI extracts structured information from the conversation into `leads.qualification_data` (JSONB) **[D18]**.
- The backend applies explicit qualification rules to that structured information and is solely responsible for setting `lead.status`.

Initial rule concept — a lead becomes `QUALIFIED` when all of the following hold:
- there is genuine product/service interest
- required business/qualification information has been collected
- the customer has demonstrated demo intent or accepted a demo

The exact required fields are configuration.

V1 does not include a lead-scoring system.

## 11. RAG

RAG is required.

Use RAG for:
- products
- services
- features
- use cases
- approved pricing
- policies
- FAQs
- company information
- approved documents

Do not use RAG for live operational data:

```text
Lead state → PostgreSQL/tool
Calendar availability → Google Calendar/tool
Message sending → WhatsAppProvider
CRM state → CRMProvider
```

### Embeddings **[D1]**

```text
EmbeddingProvider
├── LocalSentenceTransformerProvider
├── FakeEmbeddingProvider          (tests)
├── GeminiEmbeddingProvider        (optional/future)
└── VoyageEmbeddingProvider        (optional/future)
```

V1 uses a local Sentence Transformers model. Initial model: `sentence-transformers/all-MiniLM-L6-v2` (384 dimensions). Vectors are stored in PostgreSQL via pgvector.

V1 does not use Gemini, Voyage, or OpenRouter for embeddings.

Rules:
- The application must not depend on a specific embedding provider outside the provider implementation.
- Model name and vector dimension are configuration-driven.
- `all-MiniLM-L6-v2` is not assumed permanent. Retrieval quality will be evaluated against representative project questions before production.
- A change of embedding model is a vector-schema/migration concern. Vectors generated by incompatible models or dimensions must never be mixed.

### Knowledge entities

```text
knowledge_bases
knowledge_documents
knowledge_chunks
knowledge_ingestion_jobs
```

### Ingestion

```text
Document upload
 ↓
Metadata stored
 ↓
Ingestion job
 ↓
Cloud Task
 ↓
Worker
 ↓
Parse
 ↓
Extract/clean text
 ↓
Chunk
 ↓
Generate embeddings (local Sentence Transformer)
 ↓
Store chunks + vectors (pgvector)
 ↓
Document READY
```

Embedding generation happens in the worker, for both ingestion and query-time retrieval. The API service does not embed.

### Retrieval

```text
Customer question
 ↓
Query embedding
 ↓
pgvector similarity search
 ↓
READY-document filtering
 ↓
Relevant chunks
 ↓
Bounded context
 ↓
LLM
```

If evidence is insufficient, do not hallucinate. Use an approved fallback.

Retrieved documents are untrusted data. Document instructions must never override system rules.

Test relevant, irrelevant, unanswered, multi-chunk and prompt-injection cases.

## 12. Google Calendar and Demo Booking

Provider **[D7]**:

```text
CalendarProvider
├── FakeCalendarProvider
└── GoogleCalendarProvider
```

V1 uses Google OAuth against ONE shared sales calendar. Per-salesperson calendars are not implemented in V1.

`FakeCalendarProvider` must be used in automated tests.

Never claim a demo is booked until Google Calendar confirms successful event creation.

### Credential storage **[D19]**

The Google OAuth refresh token is stored in **Google Secret Manager**. It must never be held in plaintext application configuration or in the database.

- The worker reads the credential from Secret Manager at runtime and may cache it in memory. This keeps Cloud Run workers stateless.
- If Google returns a rotated refresh token, the worker writes a new secret version, so the worker's service account needs both read and write access to that secret.
- Local development and automated tests use `FakeCalendarProvider` and must not require Secret Manager.

### Slot policy **[D8]**

Configurable defaults — these must be configuration, not values scattered through the code:

```text
Demo duration     30 minutes
Buffer            15 minutes
Working days      Monday-Friday
Working hours     10:00-18:00
Booking horizon   14 days
Timezone          sales team's timezone
```

Required sequence:

```text
1. retrieve actual available slots
2. show those slots to the customer
3. receive the customer's selection
4. RECHECK availability immediately before booking
5. create the Calendar event
6. persist the booking only after successful Calendar confirmation
7. create dashboard notifications
8. send the customer WhatsApp confirmation
```

Handle race conditions where another booking takes the selected slot between presentation and confirmation.

Final agreed flow:

```text
Customer asks for demo
 ↓
AI offers demo
 ↓
get_available_slots
 ↓
Google Calendar
 ↓
Return real available slots
 ↓
AI presents slots
 ↓
Customer selects
 ↓
Recheck availability
 ↓
Create Calendar event
 ↓
Save booking in PostgreSQL
 ↓
Create dashboard notification
 ↓
Send WhatsApp confirmation
```

Rules:
- Never invent availability.
- Respect timezone.
- Recheck immediately before booking.
- Handle race conditions.
- Persist calendar event ID.
- Never claim booking success before provider confirmation.

Calendar event may include customer name, email, date/time, timezone, duration and meeting link when configured.

### Booking failure

If Calendar creation fails:
- no confirmed booking
- no successful-booking notification
- no successful-booking WhatsApp confirmation

If Calendar succeeds but a downstream notification fails:
- booking remains confirmed
- failed notification retries
- no duplicate calendar event

## 13. Dashboard Notifications

When a demo is successfully booked:

Customer receives:
- WhatsApp confirmation

Salesperson/admin receives:
- dashboard notification
- Google Calendar event

Recipients **[D3]**: a dashboard notification is created for **every active internal user**.

V1 does not implement lead assignment.

V1 does NOT send salesperson email or salesperson WhatsApp notifications.

`notifications.user_id` remains the recipient relationship, so a booking produces one notification row per active internal user. Notification creation must be idempotent per recipient so task retries cannot duplicate rows.

Example:

```text
New Demo Booked

Rahul has booked a demo for
tomorrow at 2:00 PM.

[View Booking]
```

Notification fields:

```text
id
user_id
type
title
message
related_entity_type
related_entity_id
is_read
created_at
read_at
```

## 14. HubSpot

HubSpot is an integration, not the primary application database.

PostgreSQL is the operational source of truth.

Use:

```text
CRMProvider
 ├── HubSpotProvider
 └── FakeCRMProvider
```

Lead deduplication identity **[D11]**:
- primary: normalized WhatsApp phone number
- secondary: email

Synchronize relevant core information:
- contact information
- lead state
- qualification information
- source/campaign information where applicable
- demo booking information
- opt-out state where appropriate

Synchronization must be asynchronous and retryable.

A HubSpot outage or failure must NOT break:
- WhatsApp
- AI
- RAG
- Calendar
- dashboard
- core PostgreSQL state

Excel remains the marketing audience source.

## 15. Database

Use:

```text
PostgreSQL
Neon
pgvector
```

Tables:

```text
users
sessions
audiences
audience_contacts
leads
campaigns
campaign_recipients
whatsapp_templates
whatsapp_suppressions
messages
conversations
conversation_messages
bookings
notifications
webhook_events
ai_runs
knowledge_bases
knowledge_documents
knowledge_chunks
knowledge_ingestion_jobs
```

Important booking fields:

```text
id
lead_id
conversation_id
calendar_event_id
start_time
end_time
timezone
status
created_at
updated_at
```

Booking states:

```text
PENDING
CONFIRMED
CANCELLED
FAILED
```

### WhatsApp identity and suppression **[D16]**

The normalized E.164 phone number is the primary WhatsApp identity.

`whatsapp_suppressions` is a phone-level table with a unique constraint on `phone_e164`. Global opt-out lives here and survives independently of any lead row, so it persists across audiences, campaigns and re-uploads.

Campaign sending must check `whatsapp_suppressions` before sending. A `lead.opted_out` field alone is not sufficient and must not be the only check.

### Lead qualification data **[D18]**

`leads` carries a structured `qualification_data` JSONB field holding AI-extracted facts.

The backend remains solely responsible for deciding `lead.status`. The LLM writes extracted facts into `qualification_data`; it can never set `QUALIFIED` itself.

Add indexes for lead/campaign/booking lookups, notifications and vector similarity. Use unique constraints for provider IDs and idempotency.

Vector storage **[D1]**: the `knowledge_chunks` vector column dimension is driven by the configured embedding model — 384 for the initial `all-MiniLM-L6-v2`. The dimension is fixed at migration time; changing the embedding model requires a migration and full re-embedding, and vectors from incompatible models must never coexist in the same column.

### Addition: `sessions` (implementation, not a new requirement)

Phase 3 adds a `sessions` table, bringing the total to 20. It is the storage mechanism behind **[D2]**, recorded here rather than introduced silently.

A self-contained signed cookie was rejected: it is JWT in all but encoding, which **[D2]** excludes, and it cannot be revoked. `POST /auth/logout` would then only clear the browser's copy while a captured cookie stayed valid until expiry, and deactivating a user — a real operation, since **[D3]** fans notifications out to *active* users — would not end their access.

Server-side sessions make revocation real. Only the SHA-256 digest of each token is stored, so a database leak yields no usable sessions.

## 16. API

Base:

```text
/api/v1
```

Auth:

```text
POST /auth/login
POST /auth/logout
GET /auth/me
```

Authentication uses secure HTTP-only session cookies **[D2]**, reached through the same-origin BFF proxy (§19 **[D13]**). JWT is not used for user authentication unless a concrete architectural requirement appears later. (Service-to-service task authentication in §18 uses Google-issued OIDC tokens; that is unrelated to this decision.)

Passwords are hashed with **Argon2id** (product owner decision, 2026-08-18). Login failures are indistinguishable — unknown address and wrong password return the same status and body, and cost comparable time — so the endpoint cannot be used to enumerate accounts.

User management — ADMIN only, approved **[D14]**:

```text
POST /users
GET /users
GET /users/{id}
PATCH /users/{id}
```

Only ADMIN users may manage internal users. There is no public registration endpoint.

### First ADMIN bootstrap **[D15]**

The first ADMIN is created by a one-time bootstrap mechanism (CLI command), not through the API.

- Credentials come from secure environment configuration/secrets.
- The command must be idempotent and must refuse to run if an ADMIN already exists.
- No public registration path is ever created.

Audiences:

```text
POST /audiences/upload
GET /audiences
GET /audiences/{id}
GET /audiences/{id}/contacts
```

Campaigns:

```text
POST /campaigns
GET /campaigns
GET /campaigns/{id}
POST /campaigns/{id}/send
```

Leads:

```text
GET /leads
GET /leads/{id}
PATCH /leads/{id}
```

Conversations:

```text
GET /conversations/{id}
GET /leads/{id}/conversation
```

Bookings:

```text
GET /bookings
GET /bookings/{id}
```

Notifications:

```text
GET /notifications
POST /notifications/{id}/read
POST /notifications/read-all
```

Knowledge base:

```text
POST /knowledge-bases
GET /knowledge-bases
GET /knowledge-bases/{id}
POST /knowledge-bases/{id}/documents
GET /knowledge-bases/{id}/documents
GET /knowledge-bases/{id}/documents/{document_id}
POST /knowledge-bases/{id}/documents/{document_id}/archive
```

Analytics:

```text
GET /analytics/overview
GET /analytics/campaigns/{id}
```

WhatsApp:

```text
GET /webhooks/whatsapp
POST /webhooks/whatsapp
```

## 17. Dashboard

Sections:

```text
Overview
Audiences
Campaigns
Leads
Conversations
Bookings
Knowledge Base
Notifications
Analytics
```

Overview metrics:
- campaigns
- sent
- delivered
- read
- failed
- replies
- interested
- qualified
- demos booked

Bookings:
- customer
- date/time
- timezone
- status
- meeting details
- calendar event reference

Leads:
- customer information
- source
- state
- conversation
- booking
- timestamps

Knowledge Base:
- create/select knowledge base
- upload documents
- status
- archive
- ingestion errors

## 18. Cloud Tasks and Worker

Use Cloud Tasks for:
- WhatsApp broadcast jobs
- inbound conversation processing
- RAG ingestion
- embeddings
- Calendar jobs when asynchronous
- HubSpot sync

Queue abstraction:

```text
QueueProvider
├── Fake/Inline implementation   (early development and tests)
└── CloudTasksProvider           (production)
```

The abstraction is created early (Phase 1) so earlier phases — notably the Phase 6 webhook "persist and queue" requirement — can enqueue work before the real integration exists. The real Cloud Tasks integration is implemented in Phase 7.

Every task must be:
- authenticated
- retryable
- idempotent
- observable

Worker is stateless. The worker loads a read-only local embedding model at startup; this is not request state and does not make the worker stateful.

## 19. Security

- Never hard-code secrets.
- Use environment variables locally.
- Use secure secret storage in production.
- Protect internal APIs.
- Authenticate/validate webhooks.
- Validate all external input.
- Limit uploaded files.
- Avoid unnecessary PII logging.
- Never log provider access tokens.
- Use parameterized database access.
- Use HTTPS in production.
- Configure CORS appropriately.
- Protect against prompt injection.
- Apply appropriate rate limits.

### Same-origin BFF proxy **[D13]**

The browser communicates with the application through the frontend origin only. A Next.js server-side API proxy (backend-for-frontend) forwards dashboard requests to the Cloud Run FastAPI service.

```text
Browser
  ↓
Vercel / Next.js
  ↓
Next.js server-side API proxy
  ↓
Cloud Run FastAPI
```

Rules:
- Session cookies remain secure and HTTP-only, per **[D2]**.
- `SameSite=None` must not be the default architecture. Because the browser only ever sees the frontend origin, the session cookie is same-site.
- The API must set the session cookie without an explicit `Domain` attribute so the proxy can pass `Set-Cookie` through and the browser attributes it to the frontend origin.
- The proxy must forward the correlation ID (§21) so request tracing survives the extra hop.

The BFF covers browser traffic only. These paths do NOT go through the proxy:
- `GET|POST /webhooks/whatsapp` — Meta calls the Cloud Run API directly.
- Cloud Tasks → worker — Google-issued OIDC, service to service.

The Cloud Run API therefore remains publicly reachable for webhooks; the proxy is a same-origin convenience for the browser, not a network boundary that makes the API private.

## 20. Error Handling and Idempotency

Handle provider:
- timeout
- network failure
- invalid response
- rate limit
- auth failure
- outage

Retry only retryable failures.

Idempotency is required for:
- WhatsApp webhooks
- sending tasks
- campaign recipients
- RAG ingestion
- embeddings
- Calendar booking
- notifications
- HubSpot sync

## 21. Observability

Use structured logs and correlation IDs.

Track:
- request ID
- task ID
- lead ID
- campaign ID
- conversation ID
- booking ID

Monitor worker failures, task retries, WhatsApp failures, AI failures, RAG failures, Calendar failures and HubSpot failures.

## 22. Testing

### Unit
Validation, state transitions, chunking, retrieval, tools, notification creation.

### Integration

```text
API → database
API → Cloud Tasks
Worker → fake WhatsApp
Worker → fake LLM
Worker → fake Calendar
Worker → fake CRM
RAG → pgvector
```

### End-to-end

```text
Excel
→ audience
→ campaign
→ WhatsApp
→ reply
→ webhook
→ worker
→ AI
→ RAG
→ qualification
→ Calendar
→ booking
→ dashboard notification
→ customer confirmation
→ HubSpot
```

### Fake providers

```text
FakeWhatsAppProvider
FakeLLMProvider
FakeEmbeddingProvider
FakeCalendarProvider
FakeCRMProvider
Fake/Inline QueueProvider
```

## 23. Deployment **[D12]**

```text
Browser
  ↓
Vercel / Next.js
  ↓
Next.js server-side API proxy (BFF)
  ↓
Cloud Run API
  ↓
PostgreSQL/Neon

Meta WhatsApp webhook
  ↓
Cloud Run API          (direct, bypasses the BFF)

Cloud Run API
  ↓
Cloud Tasks
  ↓
Cloud Run Worker

Worker
 ├── WhatsApp
 ├── OpenRouter
 ├── Local Sentence Transformer RAG
 ├── Google Calendar
 └── HubSpot
```

Hosting:
- Next.js frontend → Vercel
- FastAPI API → Google Cloud Run
- Worker → Google Cloud Run
- Background jobs → Google Cloud Tasks
- Database → PostgreSQL/Neon

The Next.js frontend is NOT deployed to Cloud Run in V1. Docker images are therefore built for the API and worker services only.

Browser traffic reaches the API through the same-origin Next.js BFF proxy **[D13]**, so the browser never makes a cross-origin request and no permissive CORS configuration is needed for the dashboard. See §19.

Use secure production secrets and explicit migrations.

## 24. Environment Configuration

Expected categories:

```text
DATABASE_URL
APP_ENV
APP_SECRET

WHATSAPP_ACCESS_TOKEN
WHATSAPP_PHONE_NUMBER_ID
WHATSAPP_VERIFY_TOKEN

OPENROUTER_API_KEY
OPENROUTER_MODEL

EMBEDDING_PROVIDER
EMBEDDING_MODEL
EMBEDDING_DIMENSION

GOOGLE_CLOUD_PROJECT
GOOGLE_CLOUD_TASKS_QUEUE
GOOGLE_CALENDAR_CREDENTIALS_OR_CONFIG

HUBSPOT_ACCESS_TOKEN

FRONTEND_URL
API_URL
```

Additional configuration required by the resolved decisions:

```text
AUDIENCE_PHONE_DEFAULT_REGION      IN                    [D4]
AUDIENCE_COLUMN_SCHEMA             (pending client)      [D4]

OPT_OUT_KEYWORDS                                         [D5]

GOOGLE_CALENDAR_ID                 shared sales calendar [D7]

DEMO_DURATION_MINUTES              30                    [D8]
DEMO_BUFFER_MINUTES                15                    [D8]
DEMO_WORKING_DAYS                  MON-FRI               [D8]
DEMO_WORKING_HOURS                 10:00-18:00           [D8]
DEMO_BOOKING_HORIZON_DAYS          14                    [D8]
DEMO_TIMEZONE                      sales team timezone   [D8]

QUALIFICATION_REQUIRED_FIELDS                            [D9]

SESSION_COOKIE_NAME                                      [D2]
SESSION_COOKIE_SECURE              true                  [D2]
SESSION_TTL                                              [D2]
INTERNAL_API_BASE_URL              BFF → Cloud Run       [D13]

BOOTSTRAP_ADMIN_EMAIL              secret                [D15]
BOOTSTRAP_ADMIN_PASSWORD           secret                [D15]

GOOGLE_SECRET_MANAGER_PROJECT                            [D19]
GOOGLE_CALENDAR_TOKEN_SECRET_NAME                        [D19]

MSG_RAG_FALLBACK                                         [D21]
MSG_BOOKING_CONFIRMATION                                 [D21]
MSG_OPT_OUT_RESPONSE                                     [D21]
```

`OPENROUTER_MODEL` has no default value and the application must fail fast if it is unset **[D17]**.

Customer-facing copy keys **[D21]** are configurable content. Whether they live in environment variables or a configuration file is an implementation choice for the relevant phase; the requirement is that they are not hard-coded strings.

Exact variable names may be adjusted during implementation.

## 25. Implementation Phases

### Phase 0
Analysis and repository inspection. No coding.

### Phase 1
Foundation and abstractions: Next.js, FastAPI, Docker (API and worker only), testing, linting, configuration, and the six provider abstractions with their fakes.

### Phase 2
Database, migrations, pgvector, indexes and constraints.

### Phase 3
Authentication and authorization, including the ADMIN user-management endpoints and the first-ADMIN bootstrap command.

### Phase 4
Excel audience upload, validation, normalization, duplicates and preview.

### Phase 5
Campaign creation, templates, recipient snapshots and campaign states.

### Phase 6
WhatsApp provider, Meta API, webhooks, statuses and fake provider.

### Phase 7
Cloud Tasks, worker, retries, idempotency and task authentication.

### Phase 8
RAG knowledge base, ingestion, chunking, local Sentence Transformer embeddings, pgvector and retrieval.

### Phase 9
Conversation persistence and processing.

### Phase 10
OpenRouter AI, RAG integration, tools and qualification.

### Phase 11
Google Calendar, booking, dashboard notification and customer confirmation.

### Phase 12
HubSpot integration.

### Phase 13
Complete dashboard and analytics.

### Phase 14
Full testing, security and hardening.

### Phase 15
Production deployment and smoke tests.

## 26. Acceptance Test

The finished system must demonstrate:

```text
1. Admin uploads Excel.
2. System validates audience.
3. Admin confirms audience.
4. Admin creates campaign.
5. Admin selects approved WhatsApp template.
6. Admin confirms Send Broadcast.
7. Cloud Tasks queues recipients.
8. Worker sends WhatsApp messages.
9. Delivery/read events update database.
10. Customer replies.
11. WhatsApp webhook receives reply.
12. Event is persisted and queued.
13. AI worker processes conversation.
14. RAG retrieves relevant company knowledge when needed.
15. AI answers customer.
16. AI qualifies customer.
17. Customer requests demo.
18. AI retrieves real Calendar availability.
19. Customer selects slot.
20. System rechecks availability.
21. Calendar event is created.
22. Booking is persisted.
23. Dashboard notification is created.
24. Customer receives WhatsApp confirmation.
25. Salesperson sees the dashboard notification.
26. Salesperson sees the event in Google Calendar.
27. Relevant data synchronizes to HubSpot.
```

## 27. Locked Product Decisions

Unless explicitly changed:
- Audience comes from Excel.
- Broadcast is manually initiated.
- Sending is asynchronous.
- WhatsApp is the customer communication channel.
- RAG answers approved factual business knowledge.
- OpenRouter is the LLM gateway.
- PostgreSQL is the operational database.
- pgvector is used for RAG.
- Cloud Run hosts API/worker.
- Cloud Tasks handles background jobs.
- Google Calendar handles demo availability/events.
- HubSpot is an integration, not the primary database.
- Demo booking gives salesperson/admin a dashboard notification and Google Calendar event.
- No salesperson email/WhatsApp notification in V1.
- Follow-up automation after demos is out of scope.

### Decisions resolved 2026-08-17

| ID | Decision |
|---|---|
| D1 | Embeddings: local Sentence Transformers, `all-MiniLM-L6-v2` (384 dim), provider-abstracted, model/dimension configuration-driven. No Gemini/Voyage/OpenRouter embeddings in V1. |
| D2 | Auth: secure HTTP-only session cookies. No public registration. Roles ADMIN and SALESPERSON. Admin manages users. No JWT for user auth. |
| D3 | Booking notification fans out to every active internal user. No lead assignment in V1. `notifications.user_id` retained. |
| D4 | Excel schema not invented; confirmed before Phase 4. Validation/normalization/duplicate/preview capability required regardless. Phone default region `IN`. |
| D5 | Opt-out is global at normalized phone/lead level, persists across campaigns, keywords configurable. No campaign-specific opt-out. |
| D6 | Meta-approved templates are the source of truth, synchronized into `whatsapp_templates` in Phase 6. Never invent local approval status. |
| D7 | Calendar: Google OAuth, one shared sales calendar. No per-salesperson calendars in V1. Fake provider in tests. |
| D8 | Slot policy configurable: 30 min duration, 15 min buffer, Mon–Fri, 10:00–18:00, 14-day horizon, sales team timezone. |
| D9 | AI extracts structured information; backend applies explicit qualification rules. Required fields configurable. No lead scoring in V1. |
| D10 | OpenRouter gateway, model configurable via `OPENROUTER_MODEL`, low-cost capable model for V1, fake LLM in tests, approved fallback on insufficient retrieval. |
| D11 | CRM dedupe identity: normalized WhatsApp phone primary, email secondary. Async, retryable. CRM failure never breaks core flows. |
| D12 | Next.js → Vercel. API and worker → Cloud Run. Frontend not on Cloud Run in V1. |

### Architectural items resolved 2026-08-17 (second pass)

| ID | Decision |
|---|---|
| D13 | Browser talks only to the frontend origin via a same-origin Next.js server-side API proxy (BFF) that forwards to Cloud Run FastAPI. Secure HTTP-only session cookies retained. `SameSite=None` is not the default architecture. Webhooks and Cloud Tasks bypass the BFF. |
| D14 | ADMIN-only user management endpoints approved: `POST /users`, `GET /users`, `GET /users/{id}`, `PATCH /users/{id}`. No public registration. |
| D15 | First ADMIN created by a one-time bootstrap CLI command with credentials from secure environment configuration/secrets. No public registration. |
| D16 | Normalized E.164 phone is the primary WhatsApp identity. Separate `whatsapp_suppressions` table with a unique constraint on `phone_e164`. Global opt-out survives independently of any lead row. Campaign sending must check it; `lead.opted_out` alone is insufficient. |
| D17 | No permanent LLM model chosen. `OPENROUTER_MODEL` stays configurable with no baked-in default; inexpensive capable models benchmarked in Phase 10 against representative conversations. |
| D18 | `leads.qualification_data` JSONB holds AI-extracted facts. The backend alone decides `lead.status`; the LLM can never set `QUALIFIED`. |
| D19 | Google OAuth refresh token stored in Google Secret Manager, never in plaintext config or the database. Workers stay stateless. |
| D20 | Excel schema remains deliberately unresolved until the client provides the real format. No production column names invented. Ingestion stays configuration-driven. Reaffirms **[D4]**. |
| D21 | Customer-facing copy is configurable content: RAG fallback, booking confirmation, opt-out response and other approved messages. No Meta-approved template names or content invented. |

Provider abstractions are mandatory for all six external concerns: WhatsApp, LLM, Embedding, Calendar, CRM, Queue. Each has a fake used in automated tests. **[D1]**–**[D12]** remain in force.
