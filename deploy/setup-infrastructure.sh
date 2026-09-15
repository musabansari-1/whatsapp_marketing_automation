#!/usr/bin/env bash
# One-time cloud resource creation (§23, Phase 15).
#
# Creates everything the platform needs that is not a deployment: APIs, the registry,
# service accounts and their permissions, the Cloud Tasks queue, empty Secret Manager
# secrets, and the scheduled sweeps.
#
# **Idempotent.** Every step checks before creating, so a run interrupted by a
# permissions problem can simply be repeated. That matters more than elegance here: a
# half-finished setup is the normal state the first time anyone does this.
#
# **Creates no secret values.** The secrets are created empty; an operator fills them
# with `deploy/secrets.sh`. Nothing in version control ever holds a credential (§19).
#
# Run once per environment:
#
#     PROJECT_ID=my-project ./deploy/setup-infrastructure.sh

source "$(dirname "$0")/config.sh"

log "Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  cloudtasks.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  monitoring.googleapis.com \
  logging.googleapis.com \
  --project "$PROJECT_ID"

# ---------------------------------------------------------------- registry

log "Artifact Registry"
if ! exists gcloud artifacts repositories describe "$REPOSITORY" \
  --location "$REGION" --project "$PROJECT_ID"; then
  gcloud artifacts repositories create "$REPOSITORY" \
    --repository-format=docker \
    --location "$REGION" \
    --description "WhatsApp marketing platform images" \
    --project "$PROJECT_ID"
fi

# ---------------------------------------------------------------- service accounts
#
# Three, not one. The API is publicly reachable — §19 notes the webhook path keeps it
# that way — so it should not carry the worker's ability to read secrets, write to the
# calendar, or reach HubSpot.

create_sa() {
  local email="$1" name="$2" description="$3"
  if ! exists gcloud iam service-accounts describe "$email" --project "$PROJECT_ID"; then
    gcloud iam service-accounts create "$name" \
      --display-name "$description" --project "$PROJECT_ID"
  fi
}

log "Service accounts"
create_sa "$API_SA" "wa-api" "WhatsApp marketing API"
create_sa "$WORKER_SA" "wa-worker" "WhatsApp marketing worker"
create_sa "$INVOKER_SA" "wa-invoker" "Cloud Tasks and Scheduler invoker"

grant() {
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:$1" --role "$2" --condition=None --quiet >/dev/null
}

log "Permissions"
# The API enqueues work.
grant "$API_SA" "roles/cloudtasks.enqueuer"
# The worker enqueues too: one conversation turn per inbound message (§18), and its
# own recovery sweep re-enqueues stranded turns (D-15). Without this the webhook
# path dies here: the message is stored, and no reply is ever queued.
grant "$WORKER_SA" "roles/cloudtasks.enqueuer"
# Both read their configuration secrets.
grant "$API_SA" "roles/secretmanager.secretAccessor"
grant "$WORKER_SA" "roles/secretmanager.secretAccessor"
# [D19]: the worker *writes* a new secret version when Google rotates the refresh
# token. Read access alone works until the rotation, then fails everywhere at once.
grant "$WORKER_SA" "roles/secretmanager.secretVersionAdder"
# The invoker is what Cloud Tasks and Cloud Scheduler use to call the worker; the
# worker verifies the resulting OIDC token.
grant "$INVOKER_SA" "roles/run.invoker"
# The worker's requeue path (§18, D-15) creates tasks with the invoker's identity, so
# OIDC tokens can be minted for its sends. Acting as the invoker needs the same
# delegation the API already has, granted on the invoker account itself.
gcloud iam service-accounts add-iam-policy-binding "$INVOKER_SA" \
  --member "serviceAccount:$WORKER_SA" --role "roles/iam.serviceAccountUser" \
  --project "$PROJECT_ID" --quiet >/dev/null

# ---------------------------------------------------------------- queue
#
# [D27] / D-04: these must match the application's retry configuration. See config.sh.

log "Cloud Tasks queue"
if ! exists gcloud tasks queues describe "$TASKS_QUEUE" \
  --location "$REGION" --project "$PROJECT_ID"; then
  gcloud tasks queues create "$TASKS_QUEUE" \
    --location "$REGION" --project "$PROJECT_ID"
fi

# Applied on every run, not only at creation: the retry policy is the thing most likely
# to drift from the application's, and re-asserting it is cheap.
gcloud tasks queues update "$TASKS_QUEUE" \
  --location "$REGION" --project "$PROJECT_ID" \
  --max-attempts "$QUEUE_MAX_ATTEMPTS" \
  --min-backoff "$QUEUE_MIN_BACKOFF" \
  --max-backoff "$QUEUE_MAX_BACKOFF" \
  --max-dispatches-per-second "$QUEUE_MAX_DISPATCHES_PER_SECOND" \
  --max-concurrent-dispatches "$QUEUE_MAX_CONCURRENT"

# ---------------------------------------------------------------- secrets
#
# Created empty. Values come from `deploy/secrets.sh`, run by someone holding them.

log "Secret Manager entries (empty)"
for secret in "${SECRET_APP[@]}" "$SECRET_CALENDAR_TOKEN"; do
  if ! exists gcloud secrets describe "$secret" --project "$PROJECT_ID"; then
    gcloud secrets create "$secret" --replication-policy=automatic --project "$PROJECT_ID"
    echo "  created $secret (no value yet)"
  fi
done

log "Done"
cat <<'NEXT'
Created. Nothing is deployed and no secret has a value yet.

Next:
  1. deploy/secrets.sh          — supply the credentials
  2. deploy/deploy.sh           — build, migrate, deploy
  3. deploy/schedule-sweeps.sh  — needs the worker URL, so it runs after deploy
  4. deploy/alerts.sh           — log-based alerting
  5. deploy/smoke-test.sh       — verify

The Cloud Tasks dispatch rate is a placeholder. Set QUEUE_MAX_DISPATCHES_PER_SECOND
from the WhatsApp Business account's real tier before any broadcast ([D28], D-05).
NEXT
