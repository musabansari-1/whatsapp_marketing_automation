# WhatsApp AI Marketing Platform

Internal WhatsApp marketing platform. See `SPECIFICATION.md` for the product and
architecture source of truth, `CLAUDE.md` for development rules, and
`PROJECT_STATUS.md` for the current implementation state.

**Current state: Phase 1 (Foundation and abstractions) complete.** No business features
are implemented yet.

## Layout

```text
backend/
  app/
    api/            FastAPI service (Cloud Run) — HTTP layer only
    worker/         Worker service (Cloud Run) — background task execution
    core/           Configuration, logging, correlation IDs, error taxonomy
    domain/         Business rules (populated from Phase 2 onward)
    persistence/    Database engine and session management
    integrations/   The six provider abstractions, each with a fake
  alembic/          Migrations (first migration lands in Phase 2)
  tests/
frontend/
  app/              Next.js App Router
    api/[...path]/  Server-side BFF proxy — the browser's only route to the API
  lib/              Proxy helpers
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

## Provider abstractions

Six external concerns sit behind interfaces, each with a fake used by the test suite.
Selection is by configuration, so no layer above `app/integrations/` names a concrete
provider.

| Concern   | Interface           | Fake                      | Real implementation      |
| --------- | ------------------- | ------------------------- | ------------------------ |
| WhatsApp  | `WhatsAppProvider`  | `FakeWhatsAppProvider`    | Meta — Phase 6           |
| Queue     | `QueueProvider`     | `InMemoryQueueProvider`   | Cloud Tasks — Phase 7    |
| Embedding | `EmbeddingProvider` | `FakeEmbeddingProvider`   | Sentence Transformers — Phase 8 |
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
