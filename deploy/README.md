# Deployment runbook (§23)

Everything needed to run this platform, as scripts and procedure. **Nothing here has
been executed.** Running it requires a Google Cloud project, a Neon database and a
Vercel account, and §23's instruction is explicit: *do not send real messages or deploy
production until explicitly approved.*

The scripts are idempotent. A run interrupted by a missing permission can be repeated,
which is the normal experience the first time.

## Order

```text
1. setup-infrastructure.sh   once per environment — APIs, registry, identities, queue, secrets
2. secrets.sh                supply credentials    (interactive, values never touch disk)
3. oauth-consent.md          the one manual step   (Google refresh token)
4. deploy.sh                 build → migrate → worker → API
5. schedule-sweeps.sh        needs the worker URL, so it follows deploy
6. alerts.sh                 log-based alerting
7. smoke-test.sh             verify — sends nothing
```

Two companion documents, neither of them a script:

```text
STAGING_READINESS.md   what must exist before a real-provider deployment
STAGING_E2E_PLAN.md    the 26 scenarios to run once it does
```

Vercel is separate and simpler: connect the repository, set `INTERNAL_API_BASE_URL` to
the Cloud Run API URL, deploy. The frontend is **not** deployed to Cloud Run ([D12]).

## Two different first deployments

Worth separating, because they have very different prerequisites:

- **Fake-provider staging** needs only an account and a database. Every external provider
  defaults to `fake`, so the whole loop runs end to end before a single credential
  exists. This is what the scripts above are for, and it can happen today.
- **Real-provider staging** needs credentials, client input and business decisions.
  `STAGING_READINESS.md` is the gate; `STAGING_E2E_PLAN.md` is what to run once it is
  passed. Neither has been started.

The distinction matters because the second is the first deployment where a mistake
reaches a real person.

---

## Required cloud resources

| Resource | Why | Created by |
|---|---|---|
| Cloud Run service `wa-api` | The API and the Meta webhook target (§23) | `deploy.sh` |
| Cloud Run service `wa-worker` | Background execution; loads the embedding model ([D1]) | `deploy.sh` |
| Cloud Run job `wa-migrate` | Explicit migrations (§23) | `deploy.sh` |
| Cloud Tasks queue `wa-tasks` | Asynchronous dispatch (§18) | `setup-infrastructure.sh` |
| Cloud Scheduler ×3 | The recovery sweep and two housekeeping sweeps (§20, D-15, D-43) | `schedule-sweeps.sh` |
| Secret Manager ×9 | Credentials (§19, [D19]) | `setup-infrastructure.sh` (empty), `secrets.sh` (values) |
| Artifact Registry | Container images | `setup-infrastructure.sh` |
| Service accounts ×3 | API, worker, invoker | `setup-infrastructure.sh` |
| Neon PostgreSQL + pgvector | Source of truth (§15) | **Manual** — outside GCP |
| Vercel project | Frontend and BFF proxy ([D12], [D13]) | **Manual** |

**Three service accounts, not one.** The API is publicly reachable — §19 is explicit
that the webhook path keeps it that way — so it should not carry the worker's ability to
read secrets, write to the calendar, or reach HubSpot.

### Neon

Create the database, then enable the extension the schema needs:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

The connection string goes into the `database-url` secret. Use the **pooled** endpoint
for the API and the **direct** endpoint for migrations if Neon offers both — a migration
through a connection pooler can fail in ways that are tedious to diagnose.

---

## Environment variables

The full annotated list is `.env.example` at the repository root; it is the reference,
and every variable there carries a comment explaining what it does and why its default
is what it is. What follows is what a *deployment* must set, and what happens if it does
not.

### Startup will refuse these (production only)

`APP_ENV=production` activates four guards. Each exists because the alternative is a
silent, dangerous default:

| Variable | Guard |
|---|---|
| `APP_SECRET` | Refuses the development placeholder |
| `SESSION_COOKIE_SECURE` | Cannot be `false` |
| `RATE_LIMIT_ENABLED` | Cannot be `false` (§19) |
| `AUDIENCE_COLUMN_SCHEMA` | Refuses the development placeholder ([D32]) |

**The last one is currently a hard blocker on production.** It needs the client's real
Excel column names (D-46b). Staging boots without it.

### Set by `deploy.sh`

`APP_ENV`, the six provider selections, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`,
`GOOGLE_CLOUD_TASKS_QUEUE`, `GOOGLE_SECRET_MANAGER_PROJECT`,
`GOOGLE_CALENDAR_TOKEN_SECRET_NAME`, `WORKER_SERVICE_ACCOUNT_EMAIL`, `WORKER_BASE_URL`.

### Secret references, resolved by Cloud Run at start

`APP_SECRET`, `DATABASE_URL`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`,
`WHATSAPP_VERIFY_TOKEN`, `OPENROUTER_API_KEY`, `GOOGLE_OAUTH_CLIENT_SECRET`,
`HUBSPOT_ACCESS_TOKEN`.

Referenced, not injected as literals, so no credential appears in the service definition
or in `gcloud run services describe` (D-46).

### Must be set by hand before going live

These have no safe default and the platform cannot infer them:

| Variable | Waiting on |
|---|---|
| `AUDIENCE_COLUMN_SCHEMA` | The client's Excel headers (D-46b) |
| `OPENROUTER_MODEL` | The benchmark ([D17], D-20) — **no default exists and startup fails without it when the provider is `openrouter`** |
| `QUALIFICATION_REQUIRED_FIELDS` | The criteria ([D37], D-19) — **empty means no lead can ever qualify** |
| `AI_SYSTEM_PROMPT` | The client's tone and limits ([D21], D-21) |
| `GOOGLE_OAUTH_CLIENT_ID` | The shared OAuth client (D-28). Each product's calendar is `products.google_calendar_id`, not an environment variable (Phase 24) |
| `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_BUSINESS_ACCOUNT_ID` | Meta ([D22]) |
| `MSG_*` | Customer-facing copy ([D21]) |

---

## Provider configuration

Six providers, each selected by configuration and each with a fake (§1). **`deploy.sh`
defaults every external one to `fake`**, so a staging deployment runs end to end before
a single credential exists — and turning a real one on is a visible, deliberate act.

| Provider | Variable | Real value | Needs |
|---|---|---|---|
| WhatsApp | `WHATSAPP_PROVIDER` | `meta` | Access token, phone number ID, WABA ID, verify token, App Secret ([D22]) |
| LLM | `LLM_PROVIDER` | `openrouter` | API key **and** `OPENROUTER_MODEL` ([D17]) |
| Embedding | `EMBEDDING_PROVIDER` | `local_sentence_transformer` | Nothing — runs in-process, weights baked into the image ([D1]) |
| Calendar | `CALENDAR_PROVIDER` | `google` | Calendar ID, OAuth client, refresh token ([D19]) |
| CRM | `CRM_PROVIDER` | `hubspot` | Access token **and eight custom properties** (§14) |
| Queue | `QUEUE_PROVIDER` | `cloud_tasks` | The queue — created by `setup-infrastructure.sh` |

Embedding defaults to the real provider because it needs no credential and no third
party ([D1]).

**Turn them on one at a time.** Four integrations enabled simultaneously means debugging
four at once; each has its own failure mode and each is unproven against a real account.

⚠️ **HubSpot silently ignores properties it does not recognise.** No error — the write
succeeds and the data is discarded. Create all eight `wa_*` properties before enabling
it, and verify by opening one contact after the first sync. This is the most likely way
an integration appears to work while losing half of what it sends (D-39).

---

## Migration procedure

§23 requires explicit migrations. They run as a **Cloud Run job**, not on service
startup — a startup migration runs once per instance, races itself under autoscaling,
and turns a schema error into a crash-loop rather than a failed step with a readable
log.

`deploy.sh` runs it automatically, in order: build → migrate → deploy. The script exits
if the migration fails, because deploying code against an unmigrated schema is how a
deployment becomes an outage.

To run one by hand:

```bash
gcloud run jobs execute wa-migrate --region "$REGION" --project "$PROJECT_ID" --wait
```

### Rolling back

Alembic downgrades are tested in CI on every run — upgrade → downgrade → upgrade, then
`alembic check` for drift. To roll back one revision:

```bash
gcloud run jobs update wa-migrate --region "$REGION" --project "$PROJECT_ID" \
  --args "run,alembic,downgrade,-1"
gcloud run jobs execute wa-migrate --region "$REGION" --project "$PROJECT_ID" --wait
```

**Then set the args back to `upgrade,head`**, or the next deployment will roll back
again instead of migrating forward.

A downgrade that drops a column loses its data. CI proves a migration *can* be reversed,
not that reversing it is free.

---

## Smoke-test procedure

```bash
PROJECT_ID=my-project ./deploy/smoke-test.sh
```

**Sends nothing.** Every check is a read or a rejection. It verifies:

- the API answers `/health` and reaches the database via `/readiness`
- the worker is **not** publicly reachable, and answers when given an OIDC token — which
  also proves the path Cloud Tasks depends on
- all ten task handlers are registered
- the webhook verification handshake works, and a wrong token is refused
- an unsigned webhook delivery is rejected (§8)
- an unauthenticated dashboard call is refused
- the §19 rate limiter is active
- which providers are real, with a warning for each one that is

It prints the provider configuration at the end. **If any provider reads other than
`fake`, a broadcast from that deployment reaches real people.**

### Manual checks the script cannot make

- **HubSpot properties** — open a synced contact in HubSpot and confirm the `wa_*`
  fields carry values. This is the only reliable way to catch D-39.
- **Calendar availability** — ask for slots and confirm they reflect the real diary
  (D-28).
- **A real WhatsApp message** — deliberately not automated. When the time comes, send
  one to a number you control, from a campaign of one recipient.

---

## What is not covered here

Honest gaps, each with its checklist ID:

- **Data retention** (D-44) — nothing purges `webhook_events`, `ai_runs` or
  `conversation_messages`. They hold customer message content indefinitely. The
  retention window is a client decision; the job would share the scheduler wiring above.
- **The dispatch rate** (D-05) — `QUEUE_MAX_DISPATCHES_PER_SECOND` is a placeholder.
  Set it from the WhatsApp Business account's real tier before any broadcast ([D28]).
  This is the only place a true global rate can be enforced: the application's
  client-side pacing is per-instance and its effective rate is the sum across them.
- **Alert thresholds** — starting points, not measurements. An alert that fires
  constantly is one nobody reads.
- **Load testing** — none has been done. The embedding model runs in-process in the
  worker; concurrency of 1 per instance is a considered guess, not a measurement.
- **Disaster recovery** — Neon's own backups are the whole plan. No restore has been
  rehearsed.
