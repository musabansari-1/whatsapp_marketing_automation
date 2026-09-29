#!/usr/bin/env bash
# Deployment variables (§23).
#
# Sourced by every other script here. Nothing in this file is a secret: it names
# resources, it does not hold credentials. Secrets live in Secret Manager (§19, [D19])
# and are referenced by name.
#
# Override any value from the environment before sourcing, e.g.
#
#     PROJECT_ID=my-project REGION=asia-south1 ./deploy/setup-infrastructure.sh
#
# There are deliberately no defaults for PROJECT_ID: a script that silently deploys to
# the wrong project is worse than one that refuses to run.

set -euo pipefail

# ---------------------------------------------------------------- project

: "${PROJECT_ID:?Set PROJECT_ID to the Google Cloud project}"

# asia-south1 (Mumbai) matches the default DEMO_TIMEZONE of Asia/Kolkata. Change both
# together, or neither — a European deployment serving Indian customers means every
# request crosses the planet twice.
REGION="${REGION:-asia-south1}"

# ---------------------------------------------------------------- services

API_SERVICE="${API_SERVICE:-wa-api}"
WORKER_SERVICE="${WORKER_SERVICE:-wa-worker}"
MIGRATE_JOB="${MIGRATE_JOB:-wa-migrate}"

# Scaling. Named here rather than left inline in deploy.sh so every knob this deployment
# has is in one file.
#
# API_MIN_INSTANCES defaults to 1: Meta retries a webhook that times out, and a cold
# start under an inbound burst turns one slow reply into a redelivery storm.
# WORKER_MAX_INSTANCES bounds concurrency against the LLM and WhatsApp providers, each
# of which has its own rate limit.
API_MIN_INSTANCES="${API_MIN_INSTANCES:-0}"
API_MAX_INSTANCES="${API_MAX_INSTANCES:-10}"
WORKER_MAX_INSTANCES="${WORKER_MAX_INSTANCES:-10}"

# WORKER_MIN_INSTANCES defaults to 1. A worker instance starts empty and loads the
# embedding model on first use, so a cold start is tens of seconds, while Cloud Tasks
# retries a failed dispatch five times on a 2s→300s backoff and then *drops* the task.
# A burst of customer messages that arrives while the worker is scaled to zero can
# therefore 503 every dispatch of a turn before any instance is warm, stranding the
# customer exactly once — the sweep cannot re-enqueue it either, because Cloud Tasks
# keeps the dropped task's name reserved for an hour. One always-warm instance removes
# the 503 path; the burst ceiling is WORKER_MAX_INSTANCES, not this floor.
WORKER_MIN_INSTANCES="${WORKER_MIN_INSTANCES:-1}"


# ---------------------------------------------------------------- environments
#
# Staging and production are separate Google Cloud **projects**, not two sets of names in
# one. Every resource below — services, queue, secrets, scheduler jobs — is named without
# an environment suffix, so pointing PROJECT_ID at a project that already holds the other
# environment would deploy straight over it.
#
#     PROJECT_ID=acme-wa-staging    ./deploy/deploy.sh                     # APP_ENV=staging by default
#     PROJECT_ID=acme-wa-production APP_ENV=production ./deploy/deploy.sh
#
# Separate projects also keep the two sets of secrets apart, which is the part that
# matters: a staging deployment holding production's WhatsApp token can message real
# customers. If one project must host both, override API_SERVICE, WORKER_SERVICE,
# MIGRATE_JOB, TASKS_QUEUE, REPOSITORY and every secret name — and expect to regret it.

# Artifact Registry, not Container Registry: gcr.io is deprecated.
REPOSITORY="${REPOSITORY:-whatsapp-marketing}"
IMAGE_HOST="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}"
API_IMAGE="${IMAGE_HOST}/api"
WORKER_IMAGE="${IMAGE_HOST}/worker"

# ---------------------------------------------------------------- identity
#
# Three service accounts rather than one, so a compromise of the public-facing API does
# not carry the worker's ability to read secrets and write to the calendar.

API_SA="${API_SA:-wa-api@${PROJECT_ID}.iam.gserviceaccount.com}"
WORKER_SA="${WORKER_SA:-wa-worker@${PROJECT_ID}.iam.gserviceaccount.com}"
# Used by Cloud Tasks and Cloud Scheduler to mint the OIDC token the worker verifies.
INVOKER_SA="${INVOKER_SA:-wa-invoker@${PROJECT_ID}.iam.gserviceaccount.com}"

# ---------------------------------------------------------------- queue
#
# [D27]: five attempts, exponential backoff with jitter. These values must match the
# application's SEND_MAX_ATTEMPTS / SEND_BACKOFF_* configuration — D-04 in the
# pre-production checklist. If they disagree, the application's retry budget is fiction:
# a lower queue limit gives up before the application does, a higher one redelivers work
# the application considers permanently failed.

TASKS_QUEUE="${TASKS_QUEUE:-wa-tasks}"
# Deliberately NOT SEND_MAX_ATTEMPTS. Cloud Tasks retries cover transport failures
# (503 while a cold-started worker finishes loading), not message-send retries — a
# send that fails burns the app's own retry schedule instead, so the queue budget
# should outlast a cold start, not mirror the send budget (the original value of 5
# dropped turns during the ~40s worker cold start; D-27's alignment goal holds for
# SEND retry semantics, not for availability).
QUEUE_MAX_ATTEMPTS="${QUEUE_MAX_ATTEMPTS:-10}"
QUEUE_MIN_BACKOFF="${QUEUE_MIN_BACKOFF:-10s}"       # first retry waits out most of a cold start
QUEUE_MAX_BACKOFF="${QUEUE_MAX_BACKOFF:-300s}"      # = SEND_BACKOFF_MAX_SECONDS

# [D28]: the real global dispatch ceiling. The application's client-side pacing is
# per-instance, so under autoscaling its effective rate is the sum across instances —
# this is the only place an absolute rate can be set. **Set from the WhatsApp Business
# account's actual tier once it exists** (D-05); the value below is a conservative
# placeholder, not a recommendation.
QUEUE_MAX_DISPATCHES_PER_SECOND="${QUEUE_MAX_DISPATCHES_PER_SECOND:-10}"
QUEUE_MAX_CONCURRENT="${QUEUE_MAX_CONCURRENT:-50}"

# ---------------------------------------------------------------- secrets ([D19], D-46)
#
# Names only. Values are created by an operator with `deploy/secrets.sh`, never by a
# script in version control.

SECRET_APP=(
  "app-secret"
  "database-url"
  "whatsapp-access-token"
  "whatsapp-app-secret"
  "whatsapp-verify-token"
  "openrouter-api-key"
  "google-oauth-client-secret"
  "hubspot-access-token"
)

# [D19] names this one explicitly: the Google Calendar refresh token, which the worker
# both reads and *writes* when Google rotates it.
SECRET_CALENDAR_TOKEN="${SECRET_CALENDAR_TOKEN:-google-calendar-refresh-token}"

# ---------------------------------------------------------------- scheduling
#
# The sweeps (§20, D-15) and cleanup (D-43). Cron rather than event-driven, because the
# failures they repair are the *absence* of an event.

SWEEP_UNANSWERED_SCHEDULE="${SWEEP_UNANSWERED_SCHEDULE:-*/15 * * * *}"
SWEEP_COUNTERS_SCHEDULE="${SWEEP_COUNTERS_SCHEDULE:-0 * * * *}"
# Daily. Sessions expire on a scale of days, so an hourly run would find nothing most
# of the time (D-43).
SWEEP_SESSIONS_SCHEDULE="${SWEEP_SESSIONS_SCHEDULE:-30 3 * * *}"
# Hourly, staggered off the :00 rate-limit sweep. Detection latency, not correctness:
# a marathon sustains across runs while a burst confined to one hour still trips once.
SWEEP_BOT_ACTIVITY_SCHEDULE="${SWEEP_BOT_ACTIVITY_SCHEDULE:-30 * * * *}"
# Etc/UTC deliberately: these are housekeeping intervals, not customer-facing times, and
# a UTC schedule does not shift twice a year. DEMO_TIMEZONE is the separate setting that
# governs when demos may be offered.
SCHEDULER_TIMEZONE="${SCHEDULER_TIMEZONE:-Etc/UTC}"

# ---------------------------------------------------------------- helpers

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# gcloud has no universal "create or update", and `create` on an existing resource is a
# hard error. This keeps the scripts re-runnable, which matters because a half-finished
# setup is the normal state after a permissions problem.
exists() { "$@" >/dev/null 2>&1; }

# ---------------------------------------------------------------- Cloud Run URLs
#
# A Cloud Run service answers on two hostnames: the numbered
# SERVICE-PROJECT_NUMBER.REGION.run.app, and a legacy SERVICE-HASH-REGIONCODE.a.run.app
# alias. They are unrelated strings — the project number appears nowhere in the alias —
# so no substitution turns one into the other, and `status.url` may return either.
#
# deploy.sh used to try exactly that substitution (`${url/.a.run.app/.${REGION}.run.app}`)
# and fabricated a hostname Cloud Run does not serve. On 2026-09-22 it left the API
# dispatching every customer message to a 404 while the sweeps, configured by hand
# against the real URL, kept working: eighteen hours of inbound messages unanswered,
# both services healthy, and no alert able to see it.
#
# So the URL is *read*, never derived. Every consumer — the API's WORKER_BASE_URL, the
# worker's own copy of it, the sweeps' OIDC audience — takes the value from here, so
# they cannot disagree about a host that does not exist.
resolve_service_url() {
  local service="$1" url urls canonical

  url="$(gcloud run services describe "$service" \
    --region "$REGION" --project "$PROJECT_ID" --format 'value(status.url)' 2>/dev/null || true)"
  if [ -z "$url" ]; then
    echo "Service $service is not deployed in $PROJECT_ID/$REGION" >&2
    return 1
  fi

  # Cloud Run lists every hostname it serves in this annotation. Prefer the numbered
  # form from that list — not because the alias is broken, but because it is the stable
  # one: it does not change shape on a redeploy, and the sweeps bake their audience in
  # at schedule time, so a value that flips would 401 them silently.
  urls="$(gcloud run services describe "$service" \
    --region "$REGION" --project "$PROJECT_ID" \
    --format 'value(metadata.annotations."run.googleapis.com/urls")' 2>/dev/null || true)"
  canonical="$(printf '%s' "$urls" | tr -d '[]"' | tr ',' '\n' \
    | grep -E "^https://${service}-[0-9]+\.${REGION}\.run\.app$" | head -n 1 || true)"

  # A service that advertises only the alias is fine: it is a real hostname, and
  # status.url returned it rather than us inventing it.
  echo "${canonical:-$url}"
}

# The canonical hostname for a service that has never been deployed, which has no
# status.url and no annotation to read. Needed exactly once: the worker verifies task
# OIDC audiences against WORKER_BASE_URL, and Settings refuses to start with the Cloud
# Tasks queue selected and that variable unset — so the first worker revision of a new
# project needs a value before Cloud Run can tell us one. deploy.sh verifies it against
# the live service immediately afterwards, so a wrong guess fails the deployment rather
# than reaching the API.
canonical_service_url() {
  local service="$1" number
  number="$(gcloud projects describe "$PROJECT_ID" --format 'value(projectNumber)')"
  echo "https://${service}-${number}.${REGION}.run.app"
}

# The HTTP status a URL answers with, or 000 when it answers not at all (no curl, no
# route, no DNS). This is the probe that catches a fabricated hostname: Cloud Run serves
# 404 for a host it does not have, where a real but private service answers 403.
http_status() {
  local url="$1" code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$url" 2>/dev/null || true)"
  echo "${code:-000}"
}

# An environment variable as the deployed service actually holds it, not as the
# operator's shell or this file claims. Checks that read configuration are only worth
# anything if they read what is running.
deployed_env_value() {
  local service="$1" name="$2" value
  value="$(gcloud run services describe "$service" \
    --region "$REGION" --project "$PROJECT_ID" \
    --format "value(spec.template.spec.containers[0].env.filter(\"name:${name}\").extract(\"value\"))" \
    2>/dev/null || true)"
  # --format returns a YAML list (e.g. ['staging']); peel the brackets and quotes.
  printf '%s' "$(printf '%s' "$value" | tr -d "[]\"'")"
}
