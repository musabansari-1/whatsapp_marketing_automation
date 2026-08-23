# WhatsApp AI Marketing Platform — Complete Specification
Version 5 — Consolidated

Version 5 incorporates the product owner's resolution of the 12 open decisions raised in the Phase 0 readiness report, the second-pass architectural resolutions (both 2026-08-17), and the pre-Phase-7 decisions of 2026-08-21. Resolved decisions are marked **[D1]**–**[D39]** at the point they apply and are summarized in §27.

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

The uploaded file is authoritative for contact display names **[D35]**. A WhatsApp profile name must never silently overwrite a name supplied by the audience file, and the source audience must remain traceable from the lead.

Uploading an Excel file must not send messages or trigger AI. Upload must never automatically trigger a broadcast.

### Canonical audience schema **[D25]**

V1 defines a canonical set of contact fields. This is the application's schema and is
independent of whatever headers the client's spreadsheet happens to use.

```text
name          REQUIRED
phone         REQUIRED
email         optional
company       optional
designation   optional
city          optional
```

The canonical schema stays stable even when the client's Excel headers change.
Supersedes **[D4]**/**[D20]**, which deferred the schema until the client supplied it.

### Configurable column mapping **[D25]**

An administrator configures the mapping from spreadsheet header to canonical field.
The application is never hard-coded to the client's header names.

```text
"Full Name"      → name
"Mobile Number"  → phone
"Email ID"       → email
"Organization"   → company
"Job Title"      → designation
"Location"       → city
```

**[D32]**: the canonical schema is the V1 production schema. A development placeholder
may remain as a test fixture, but production must never silently fall back to it — the
existing startup guard that refuses the placeholder in production stands.

Validation requirements:
- required-column validation
- optional-column handling
- phone normalization
- duplicate detection
- invalid-row reporting
- preview before confirmation

Phone normalization defaults to Indian numbers (region `IN`) unless the client
specifies a different audience geography. The default region is configuration.

### End-to-end audience flow **[D25]**, **[D31]**

```text
Excel upload
 ↓
Parse
 ↓
Validate
 ↓
Normalize
 ↓
Show preview and errors
 ↓
User confirms
 ↓
Create/confirm audience
 ↓
Promote eligible records to leads
 ↓
Campaign can use the audience
 ↓
Campaign becomes READY when sendable recipients exist
 ↓
ADMIN explicitly clicks Send Broadcast
```

Uploading must never trigger a broadcast, and must never create permanent leads:
records become leads only at audience confirmation **[D31]**.

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

The snapshot is frozen at campaign creation and stays frozen **[D30]**. Leads added afterwards never silently enter an existing campaign; targeting them means creating a new audience or campaign. This is a campaign-auditability requirement.

Marketing broadcast is human initiated:
Upload audience → create campaign → preview → confirm Send Broadcast.

Actual sending is automated asynchronously.

### Permissions **[D33]**

Mass broadcast sending is **ADMIN-only** in V1. A salesperson must not be able to
accidentally trigger a mass WhatsApp broadcast.

```text
ADMIN         create, edit and review campaigns; send broadcasts; pause and cancel
SALESPERSON   view campaigns; prepare and review campaign information; manage leads
```

This narrows the earlier position that campaign work followed the general internal-user
permission of §3.

### READY does not mean sending **[D34]**

A campaign becomes READY automatically once it has at least one sendable recipient.
READY is a state of preparedness, not an instruction to send.

```text
DRAFT
 ↓
READY
 ↓
ADMIN explicitly clicks Send Broadcast
 ↓
PROCESSING
 ↓
COMPLETED / FAILED / CANCELLED
```

Human initiation of broadcasts is a core V1 requirement.

### Cancellation **[D29]**

A running campaign must be cancellable, and cancelling must actually stop work already
queued.

```text
Campaign PROCESSING
 ↓
ADMIN cancels
 ↓
Campaign CANCELLED
 ↓
A queued worker receives its task
 ↓
The worker re-checks campaign state
 ↓
No WhatsApp message is sent
```

Requirements:
- the worker re-checks campaign state immediately before sending, because a task may
  have been queued before the cancellation
- cancellation is idempotent
- messages already sent successfully cannot be undone

### Scheduling is out of V1 **[D39]**

`SCHEDULED` and `scheduled_for` exist in the domain model but nothing enters that state
in V1. Specifically excluded: cron-based sending, automatic scheduled broadcasts and
any autonomous campaign execution. Every broadcast begins with an explicit human
action.

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

Real Meta-approved templates are required before production broadcasting **[D23]**. Tests may continue to use fixtures and mocks, but the production campaign system admits only templates actually synchronized from Meta.

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

Recognized opt-out keywords are configuration. V1 defaults, confirmed **[D26]**:

```text
STOP
UNSUBSCRIBE
OPT OUT
OPTOUT
```

Whole-message matching is sufficient for V1. Additional client-approved or regional
variants are added to configuration, not to code.

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

### V1 agent behaviour (Phase 10)

One customer turn runs:

```text
retrieve knowledge (§11)
 ↓
assemble: system prompt + fenced knowledge + recent transcript
 ↓
ask the model, offering the six authorized tools
 ↓
run any tools it requests, feed the results back
 ↓
reply → the worker delivers it through the window and suppression checks
```

Retrieval happens **before** the model runs and outside the tool loop: §11 makes it the
evidence the reply is grounded in, not something the model chooses to consult. A
retrieval failure is not fatal — the agent is told there is no evidence and uses the
approved fallback, so the customer's turn is not lost.

Knowledge is placed in a *system* message with a preamble stating that it is data and
that instructions inside it must be ignored. This is belt and braces on top of the
sanitisation in §11: the customer's own message stays exactly what they wrote.

The tool loop is bounded by `AI_MAX_TOOL_ROUNDS`. The final round is asked *without*
tools, which is what makes it terminate — a model with no tools available answers in
words.

### The authorized tools (Phase 10)

All six from §9 are implemented, and `app/ai/tools.py` is the only route from the model
to the backend. Three properties are structural rather than prompt-dependent:

- **No tool accepts a lead or conversation identifier.** The context is scoped to the
  conversation in hand, so the model cannot name another customer and read them.
- **`update_lead` has no `status` parameter.** [D18] gives the backend sole authority;
  the tool records extracted facts and the qualification rules read them.
- **An unrecognised tool name is refused, never approximated.** Models hallucinate tool
  names, and a fuzzy match would run something the conversation never called for.

`send_whatsapp_message` is authorized but declines: the turn handler already sends the
reply through the 24-hour window and suppression checks, and a second sending path
would be a way around them. The model is told to put its text in the reply instead.

### Booking (Phase 10 / §12)

`get_available_slots` and `book_demo` are implemented here because §9 places both in
the AI's tool set and they are useless without slot computation. **Phase 11 still owns
the Google Calendar provider, the booking record and the notification fan-out**; V1
runs against `FakeCalendarProvider` until then.

Slot computation lives in `app/domain/slots.py` and is driven entirely by the [D8]
policy. Two rules matter:

- The buffer applies on **both sides** of a busy interval. A demo starting the instant
  another ends leaves nobody time to move between them.
- Everything is computed in the sales team's timezone and returned as UTC instants.
  "10:00 to 18:00" is a local statement, and a customer elsewhere is still offered
  those hours.

The §12 sequence is enforced in `book_demo`: re-check the slot against live busy
intervals, create the event, and only then report success. **A demo is never reported
as booked unless Calendar returned an event** — a failed write, a lost race and an
unavailable slot are all reported honestly so the model offers another time.

The event ID is derived from the conversation and the slot, so a retried turn converges
on one event rather than double-booking the sales team.

### Qualification (Phase 10 / [D37] pending)

`app/domain/qualification.py` implements the **mechanism** [D9] settles; the criteria
[D37] leaves open stay in configuration:

```text
QUALIFICATION_REQUIRED_FIELDS   which extracted fields must be present
```

**With that list empty — the default — nothing ever reaches QUALIFIED.** That is the
correct behaviour for an undecided rule: the machinery is complete and inert, and no
lead is qualified by accident. Supplying the fields switches it on without a code
change.

The rules require all three of §10's conditions: interest, the configured fields, and
demo intent. An explicit "not interested" outranks everything else, because a customer
who has said no must not be walked further down the funnel by a rule about collected
fields.

Results are *proposed*, never applied: the caller passes them through the §10
transition table, so a lead that has not reached QUALIFYING advances one step rather
than jumping.

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

A CLOSED conversation is never reopened **[D36]**. A later inbound message from the same customer starts a new conversation, which keeps each conversation's history coherent.

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

The final `QUALIFYING → QUALIFIED` rules remain a Phase 10 decision **[D37]** and must not be invented earlier. The division of responsibility above is settled; the criteria are not.

### V1 transition tables (Phase 9)

§10 lists the states; the edges are derived in `app/domain/conversation.py`, where each
is justified individually so review can challenge one edge rather than the whole table.
Two rules shape them:

- **CLOSED and OPTED_OUT are terminal.** [D36] never reopens a conversation, and the
  platform does not reverse a withdrawal of consent on its own.
- **Progress is not linear.** `NOT_INTERESTED` can be revived by a later message, a
  booked demo can be re-offered, and a failed delivery can be retried by a later
  campaign. Real conversations do this.

The edge that matters most is the one that is *absent*: nothing reaches `QUALIFIED`
except from `QUALIFYING`. A model that decides a lead is qualified cannot make it so.

Every status change — by the worker or by a human through `PATCH /leads/{id}` — goes
through this table. [D18] gives the backend sole authority over `lead.status`; a single
validated path is what makes that enforceable rather than a convention.

### Conversation processing (Phase 9)

One customer message is one task:

```text
webhook stored → PROCESS_CONVERSATION_TURN queued per message
 ↓
worker: already replied? → closed? → suppressed? → inside 24h window?
 ↓
record the customer's turn
 ↓
ask the LLM with the recent transcript
 ↓
send, record the reply, move the state
```

Each check exists to prevent a specific failure, and the order is deliberate: the
idempotency check comes first because a redelivered task must not reply twice, and the
suppression check precedes the model call because consent may have been withdrawn
after the task was queued ([D16]).

The reply's idempotency key is derived from the triggering message, so a retry computes
the same key and the unique constraint refuses the second write.

Outside the 24-hour window the customer's turn is still recorded but nothing is sent:
§8 permits only an approved template there, and [D24] leaves that content with the
client.

Transcript order is decided by a database-assigned sequence, not `created_at`. The
latter defaults to `now()`, which is the *transaction* timestamp, so turns written
together would tie — and that order is what the AI reads the conversation in.

There is no endpoint for sending a message into a conversation. The worker is where
the window and suppression checks live, and an endpoint bypassing them would be a way
to message a customer who has opted out.

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

### V1 retrieval parameters (Phase 8)

Configuration, not constants in code. Defaults:

```text
RAG_CHUNK_CHARS            800     chunk size
RAG_CHUNK_OVERLAP_CHARS    120     overlap between adjacent chunks
RAG_TOP_K                  5       chunks returned per query
RAG_MIN_SIMILARITY         0.25    below this, a chunk counts as no evidence
RAG_MAX_CONTEXT_CHARS      4000    ceiling on assembled context
KNOWLEDGE_MAX_FILE_BYTES   700000  upload limit (§19), bounded by the queue
EMBEDDING_BATCH_SIZE       32      chunks per forward pass
```

Chunk size is bounded by the model, not chosen freely: `all-MiniLM-L6-v2` accepts 256
word-pieces and silently truncates the rest, so an over-long chunk would be embedded
from its opening words alone with no error raised. At roughly four characters per
token, 800 characters stays inside that window.

Supported upload formats in V1: `.txt`, `.md`, `.pdf`. A PDF with no text layer is
rejected as needing OCR rather than stored as an empty document.

The upload limit is not a free choice either. V1 has no blob store, so an uploaded
document travels base64-encoded inside the Cloud Tasks payload, and Cloud Tasks rejects
a task larger than 1 MB. Startup refuses a `KNOWLEDGE_MAX_FILE_BYTES` above that
ceiling when Cloud Tasks is the selected queue, so an oversized upload cannot be
accepted and then fail at enqueue time. **Ingesting documents larger than this requires
object storage, with the task carrying only a reference — a V2 change.**

### Retrieval filters

Two filters are mandatory on every search:

- **READY documents only.** A pending, failed or archived document must not contribute
  content to an answer.
- **Matching embedding model.** Cosine distance between vectors from different models
  is arithmetically valid and semantically meaningless, so **[D1]**'s prohibition is
  enforced in the query, not merely intended. Each chunk records the model that
  produced it.

Archiving a document stops retrieval but does not delete its chunks: answers already
given cite chunk IDs in `ai_runs`, and deleting would break that trail.

### Untrusted-document handling

Retrieved text is fenced inside explicit delimiters and attributed to its source
document. Before it enters the context:

- Delimiter look-alikes in both chunk text and document titles are replaced, so no
  document can close the block early and have the remainder read as system input.
- Lines shaped like role markers (`System:`, `Assistant:`) have the colon replaced with
  a look-alike character. The text is neutralised in place rather than deleted, so an
  operator reviewing a bad answer can still see what the document said.

Retrieval is not exposed as an HTTP endpoint. It is a backend tool the AI calls
(§13), which keeps it inside the authorization model rather than open to any
authenticated caller.

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
- Google Calendar credentials are introduced in the Calendar phase, and HubSpot credentials in the HubSpot phase **[D38]**. No production credentials are introduced earlier.

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

### V1 implementation (Phase 11)

The §12 sequence, and where each step lives:

```text
customer selects a slot          book_demo tool (§9)
 ↓
already booked?                  by deterministic event ID — before the recheck
 ↓
recheck against live busy time   app/domain/slots.is_slot_available
 ↓
Calendar event                   GoogleCalendarProvider.create_event
 ↓
booking row                      bookings, CONFIRMED, keyed on calendar_event_id
 ↓
dashboard notification           NOTIFY_BOOKING task, fan-out to every active user
 ↓
customer confirmation            free-form WhatsApp reply, inside the 24-hour window
```

**The already-booked check comes before the recheck, and that ordering is
load-bearing.** On a retried turn the booking's own calendar event is now busy time, so
rechecking first would find the slot occupied and tell the customer their demo was lost
— when in fact it is theirs.

**Notification and confirmation are a separate task.** §12 says a confirmed booking
stays confirmed while a failed notification retries, so neither may delay nor undo a
booking Google has already accepted. Within the task the team is told first: if the
customer's confirmation fails, the team can still act.

The customer's confirmation needs no approved template. It is a reply inside the window
the customer opened by asking for the demo, so it does not depend on [D24].

### Credentials (Phase 11 / [D19])

The refresh token lives in Google Secret Manager and is read at first use, then cached
in memory for the process lifetime — the worker stays stateless. When Google returns a
**rotated** refresh token it is written back as a new secret version; missing that is a
failure that hides for months, then breaks authentication everywhere at once.

Local development and tests use an in-memory store and never require GCP, as [D19]
requires.

Obtaining the first refresh token is a **one-time human OAuth consent flow**. It is not
something the platform can perform for itself, and until it is done the Google provider
fails with a message saying so.

### Idempotency (Phase 11)

Three independent mechanisms, because a demo booked twice is worse than one not booked:

- **Google accepts a client-supplied event ID**, derived from the conversation and the
  slot. A retry sends the same ID, Google answers 409, and the provider fetches and
  returns the existing event.
- **`bookings.calendar_event_id` is unique.** A retry converges on the existing row.
  This also covers the gap the sequence cannot close: the event is created before the
  row is written, so a crash between them leaves an event with no record, and the next
  attempt must find it rather than book again.
- **`notifications` is unique on user, type and entity.** A retried fan-out re-runs for
  everyone and duplicates for nobody, and the customer's confirmation is keyed on the
  booking so it cannot be sent twice.

`uq_bookings_confirmed_start_time` — partial, CONFIRMED only — is the database's share:
two concurrent confirmations for the same slot cannot both commit, and a cancelled
booking does not block rebooking the time.

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

### V1 implementation (Phase 12)

Synchronization is one-way and outward. Nothing reads a CRM value back to change
platform state, so a stale or hand-edited HubSpot record can never overwrite what the
platform knows.

**What is synchronized**, mapped in `app/domain/crm.py`:

```text
contact information   firstname, lastname, email, phone, company, jobtitle, city
lead state            wa_lead_status
qualification         wa_qualification  (the AI's extraction, [D18] — informational)
source                wa_lead_source    (audience name, or "WhatsApp inbound")
campaign              wa_campaign       (the most recent campaign that messaged them)
booking               wa_demo_at, wa_demo_status  (confirmed bookings only)
opt-out               wa_opted_out
```

The `wa_*` properties are **custom properties the client must create in HubSpot**.
HubSpot silently ignores unknown properties on write, so a missing one loses data
without erroring — which is why they are listed in one place in the code.

**What is not synchronized:** the conversation transcript. It is long, personal, and
pushing it into a third-party system is a decision nobody has made.

Two mapping rules are load-bearing. Unknown fields are **omitted, not blanked** —
HubSpot treats an empty string as a deliberate clear, so sending one would erase what a
salesperson typed by hand. Opt-out is **always sent, including false**, because consent
is the one field where a stale value does real harm.

### When synchronization runs (Phase 12)

On meaningful change, not on bulk upload:

- after a conversation turn (the lead engaged; facts or status may have moved)
- after a demo is confirmed
- on opt-out

**Confirming an audience does not synchronize its leads.** A confirmed audience can
hold tens of thousands of uploaded numbers with no engagement, and pushing them all
into HubSpot would flood the CRM with cold contacts nobody asked it to hold.

### Isolation (Phase 12 / §14)

§14's requirement — a HubSpot outage must not break WhatsApp, AI, RAG, Calendar, the
dashboard or core PostgreSQL state — is enforced structurally:

- Synchronization is its own task, so it retries without touching what triggered it.
- `queue_crm_sync` **swallows its own failures** and returns a boolean. Its callers are
  the conversation turn, the booking flow and the opt-out path, and none of them should
  abandon its work because a queue call failed.
- A permanent CRM failure is recorded on the lead (`crm_sync_error`) rather than raised,
  because §14 forbids surfacing it at the caller and a failure nobody records is
  invisible until someone opens HubSpot.

### Identity (Phase 12 / [D11])

[D11] makes the normalized phone number primary and email secondary. HubSpot's own
contact uniqueness is **email-based**, and a WhatsApp lead often has no email — so the
upsert searches by phone, falls back to email, and creates only when neither matches. A
409 from a concurrent create is resolved by re-reading and updating, so a retried sync
converges on one contact rather than producing a duplicate for the sales team to work
from.

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

Derived from §6's admin-confirmation step, which §16 does not give a route:

```text
POST /audiences/{id}/confirm
GET /audiences/schema
```

Confirmation upserts the audience's valid contacts into `leads`, deduplicated by
normalized phone **[D11]**. It sends nothing: a broadcast is a separate, explicitly
triggered action (§7).

`GET /audiences/schema` reports the configured column mapping so the dashboard can
state the expected format, and flags when the development placeholder is still in
force **[D20]**.

Campaigns:

```text
POST /campaigns
GET /campaigns
GET /campaigns/{id}
POST /campaigns/{id}/send
```

Derived, since §16 lists no route for reading the §7 recipient snapshot or for
selecting a template:

```text
GET /campaigns/{id}/recipients
GET /whatsapp-templates
```

Added by **[D29]**, since §16 lists no route for stopping a broadcast:

```text
POST /campaigns/{id}/cancel
```

Campaign permissions **[D33]**: `POST /campaigns`, `POST /campaigns/{id}/send` and
`POST /campaigns/{id}/cancel` are **ADMIN-only**. The read routes remain open to any
authenticated internal user.

`GET /whatsapp-templates` is read-only and returns only APPROVED MARKETING templates.
There is deliberately no endpoint that creates or approves a template: **[D6]** puts
approval with Meta, and rows appear only from the Phase 6 synchronization.

Creating a campaign freezes its recipient snapshot immediately, so the preview an
admin confirms against is exactly the set that will be messaged. `POST
/campaigns/{id}/send` is the human confirmation §7 requires; it re-checks suppression
**[D16]**, enqueues one task per pending recipient and returns. Sending itself happens
in the worker (§4, §18).

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

Both bypass the BFF **[D13]** — Meta calls them directly — so they authenticate
themselves: the GET verifies `WHATSAPP_VERIFY_TOKEN` during Meta's subscription
handshake, and the POST verifies an HMAC-SHA256 signature computed with the Meta App
Secret over the raw request body (§19).

The POST persists the delivery, enqueues processing and returns. It answers 200 for
anything correctly signed, including payloads this platform does not model: Meta
treats a non-2xx as failure and redelivers, so returning an error for an unmodelled
event would produce an endless retry loop. A redelivery is reported as a duplicate
rather than an error, enforced by the unique constraint on `webhook_events`.

### Messaging window

Meta permits free-form (non-template) messages only within 24 hours of the customer's
last inbound message. Outside it, only an approved template can be delivered.

This is not stated in the original specification and was flagged in the Phase 0
report. `conversations.last_inbound_at` records when the window opened and
`CUSTOMER_SERVICE_WINDOW_HOURS` configures its length.

### Template-based fallback **[D24]**

When the window has closed, an approved template must be used instead of a free-form
message. The architecture must support, at minimum:
- booking confirmation
- other required transactional/utility messages
- fallback messaging for a conversation outside the permitted window

Template *selection* is data/configuration-driven: a purpose (for example
"booking confirmation") maps to an approved template name in configuration, so the
platform chooses which template to send without any template identity being compiled
in.

**Content is never invented.** The customer-facing template text is written and
approved by the client through Meta, and reaches this platform only via the **[D6]**
synchronization. Until the client supplies and approves those templates, an AI reply
or booking confirmation outside the window has no template to fall back to and cannot
be delivered.

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

### V1 implementation (Phase 13)

All nine sections exist. Analytics is not a separate page: its metrics *are* the
Overview, and a second view of the same numbers would be a menu entry with nothing
behind it.

```text
Overview        /            metrics, grouped as reach → engagement → outcomes
Audiences       /audiences   (Phase 4)
Campaigns       /campaigns   (Phase 5)
Leads           /leads       list, filter and search; detail carries the transcript
Conversations   /conversations  by most recent activity
Bookings        /bookings    read-only, each in its own timezone
Knowledge Base  /knowledge   create, upload, status, archive, ingestion errors
Notifications   /notifications  per-user, with an unread count
```

### Metric definitions (Phase 13)

Three of the §17 metrics involve a judgement about which rows answer the question. They
are defined in `app/persistence/repositories/analytics.py` and computed from the tables
that own the data, never from stored counters — a counter can drift after a crashed
worker or a manual correction, and a dashboard that quietly disagrees with the records
is worse than a slow one.

- **Sent, delivered, read are cumulative.** A delivered message was also sent, so
  "sent" does not fall as receipts arrive.
- **Replies count people, not messages.** A customer who sends five messages is one
  reply; counting messages would make engagement look like reach.
- **Interested and qualified include the states past them.** A qualified lead is by
  definition interested, and a dashboard where "interested" dropped as leads progressed
  would read as leads being lost.
- **Only CONFIRMED bookings count as demos.** A cancelled or failed attempt is not a
  demo anyone will attend.

The lead funnel returns every state including the empty ones: gaps would hide where
leads are stalling, which is what the view is for.

### Dashboard constraints (Phase 13)

- **No message can be sent from the dashboard.** Replies come from the worker, which is
  where the 24-hour window and suppression checks live.
- **No booking can be created from the dashboard.** §12 puts booking behind the
  availability re-check and Calendar confirmation.
- **`qualification_data` is read-only.** [D18] makes it the AI's extraction target; a
  human editing it would be arguing with the extraction rather than correcting a record.
- Every list is scoped by the API's own authorization. Notifications are per-user and no
  route accepts a user ID.

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

### Retry policy **[D27]**

```text
Maximum attempts        5
Backoff                 exponential, with jitter
Permanent failures      never retried
Rate-limit responses    respected, then retried
```

All of it is configuration, not literals.

Three failure classes must stay distinguishable, because they demand different
behaviour:

```text
retryable / transient   retry with backoff, up to the maximum
permanent               fail the recipient immediately; retrying cannot help
rate limit              back off and retry; the request was never processed
```

**A retry must never produce a duplicate successful delivery.** Idempotency is
mandatory, not best-effort: Cloud Tasks redelivers, workers restart mid-task, and two
workers can receive the same task.

### Dispatch throttling **[D28]**

No Meta messaging tier is assumed and no production throughput number is hard-coded.
Dispatch rate is configuration, set from the WhatsApp Business account's actual limits
once the production account exists.

A large campaign must not overwhelm the provider — the platform paces itself rather
than enqueuing everything at once and letting Meta reject the overflow.

### Campaign state re-check **[D29]**

Before sending, a worker re-reads the campaign's state. A task queued before a
cancellation must not send. See §7.

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

### Asynchronous dispatch (Phase 7)

```text
successful delivery
transient failure → retry → success
transient failure → maximum attempts → FAILED
permanent failure → no retry
rate-limit response
duplicate task delivery
cancelled campaign with queued tasks
campaign cancellation race condition
worker restart / retry
multiple workers processing one campaign
idempotent recipient processing
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

No automated test may send a real WhatsApp message. Tests use the fake provider or a
mock HTTP transport.

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

Added by the 2026-08-21 decisions:

```text
WHATSAPP_APP_SECRET                      secret            [D22]
WHATSAPP_BUSINESS_ACCOUNT_ID                               [D22]
CUSTOMER_SERVICE_WINDOW_HOURS            24

AUDIENCE_COLUMN_MAPPING                  header → field    [D25]

TEMPLATE_BOOKING_CONFIRMATION            template name     [D24]
TEMPLATE_WINDOW_FALLBACK                 template name     [D24]

SEND_MAX_ATTEMPTS                        5                 [D27]
SEND_BACKOFF_BASE_SECONDS                                  [D27]
SEND_BACKOFF_MAX_SECONDS                                   [D27]
SEND_BACKOFF_JITTER                                        [D27]

DISPATCH_RATE_PER_SECOND                 no default        [D28]
DISPATCH_MAX_CONCURRENT                                    [D28]
```

Meta credentials are supplied through secure environment variables or secret storage
and are never committed **[D22]**. Template names are configuration **[D24]**; template
*content* comes from Meta and is never written here.

`DISPATCH_RATE_PER_SECOND` deliberately has no default: **[D28]** forbids assuming a
messaging tier, and a wrong guess either throttles a legitimate campaign or gets
messages rejected.

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

Also carries three revisions to already-completed phases, because the decisions of
2026-08-21 changed what those phases must do:
- campaign cancellation and the worker's state re-check **[D29]**
- ADMIN-only broadcast sending **[D33]**
- the canonical audience schema and configurable column mapping **[D25]**/**[D32]**,
  which needs new contact fields and therefore a migration

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

### Decisions resolved 2026-08-21 (pre-Phase 7)

| ID | Decision |
|---|---|
| D22 | Meta credentials come from secure environment variables/secrets and are never committed. Required: access token, phone number ID, WABA ID, verify token, App Secret. |
| D23 | Real Meta-approved templates are required before production broadcasting. Approval is never fabricated or claimed locally. Tests may use fixtures and mocks. |
| D24 | Approved templates are used when the customer-service window is closed. Selection is configuration-driven (purpose → template name); content is supplied and approved by the client through Meta. Must cover booking confirmation, transactional/utility messages and out-of-window fallback. |
| D25 | Canonical V1 audience schema: `name` and `phone` required; `email`, `company`, `designation`, `city` optional. Column mapping from the client's headers is configurable. Supersedes **[D4]**/**[D20]**. |
| D26 | Opt-out keywords confirmed as STOP, UNSUBSCRIBE, OPT OUT, OPTOUT; configurable; whole-message matching acceptable for V1; opt-out stays global at the normalized phone level. |
| D27 | Retry policy: maximum 5 attempts, exponential backoff with jitter, no retry on permanent failures, rate limits respected, all configuration-driven. A retry must never produce a duplicate successful delivery. |
| D28 | Dispatch throttling is configurable. No messaging tier or throughput number is assumed or hard-coded. |
| D29 | Campaign cancellation is implemented before or within Phase 7. A CANCELLED campaign prevents queued sends; the worker re-checks campaign state before sending; cancellation is idempotent; already-sent messages cannot be undone. |
| D30 | The recipient snapshot stays frozen at campaign creation. Leads added later never silently enter an existing campaign; targeting them means a new audience or campaign. Auditability. |
| D31 | Lead promotion stays at audience confirmation. Uploading an Excel file never creates permanent leads. |
| D32 | The canonical schema is the V1 production schema. Development fixtures migrate to it. Production must never silently fall back to a placeholder. |
| D33 | Mass broadcast sending is ADMIN-only. ADMIN creates, edits, reviews, sends, pauses and cancels; SALESPERSON views campaigns and prepares/reviews information. |
| D34 | READY is automatic once a campaign has a sendable recipient, and means prepared, not sending. An ADMIN must explicitly click Send Broadcast. |
| D35 | The uploaded audience file is authoritative for display names. A WhatsApp profile name never silently overwrites it, and source audience data stays traceable. |
| D36 | A CLOSED conversation is never reopened; a later inbound message starts a new conversation. Unchanged in Phase 7. |
| D37 | Final qualification criteria remain a Phase 10 decision. The AI extracts; the backend applies authoritative rules. |
| D38 | Google Calendar and HubSpot credentials remain with their own phases. No production credentials in Phase 7. |
| D39 | Scheduled campaigns are out of V1. No cron sending, no automatic scheduled broadcasts, no autonomous execution, despite `SCHEDULED`/`scheduled_for` existing in the model. |
