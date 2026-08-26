#!/usr/bin/env bash
# Build, migrate, deploy (§23).
#
# Order is deliberate and not interchangeable:
#
#   1. build and push both images
#   2. run migrations as a Cloud Run job, and stop if they fail
#   3. deploy the worker
#   4. deploy the API — which needs the worker's URL, so it cannot go first
#
# §23 requires explicit migrations. They run as their own job rather than on service
# startup: a migration on startup runs once per instance, races itself under
# autoscaling, and turns a schema error into a crash-loop instead of a failed step with
# a readable log.
#
#     PROJECT_ID=my-project ./deploy/deploy.sh
#     PROJECT_ID=my-project APP_ENV=staging ./deploy/deploy.sh

source "$(dirname "$0")/config.sh"

# staging by default. Production is a deliberate choice, not something typed by
# accident — and APP_ENV=production activates startup guards that will refuse to boot
# with placeholder configuration (see the runbook).
APP_ENV="${APP_ENV:-staging}"
TAG="${TAG:-$(git rev-parse --short HEAD)}"

# Provider selection (§1). Defaults to fakes so a staging deployment runs end to end
# before any credential exists — and so turning on a real provider is a visible,
# deliberate change rather than a default.
WHATSAPP_PROVIDER="${WHATSAPP_PROVIDER:-fake}"
LLM_PROVIDER="${LLM_PROVIDER:-fake}"
EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-local_sentence_transformer}"
CALENDAR_PROVIDER="${CALENDAR_PROVIDER:-fake}"
CRM_PROVIDER="${CRM_PROVIDER:-fake}"
QUEUE_PROVIDER="${QUEUE_PROVIDER:-cloud_tasks}"

log "Deploying $TAG to $APP_ENV in $PROJECT_ID/$REGION"
echo "  providers: whatsapp=$WHATSAPP_PROVIDER llm=$LLM_PROVIDER calendar=$CALENDAR_PROVIDER crm=$CRM_PROVIDER"

# ---------------------------------------------------------------- images

log "Building images"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

docker build -f backend/Dockerfile.api -t "${API_IMAGE}:${TAG}" backend
docker push "${API_IMAGE}:${TAG}"

# Carries PyTorch and the baked-in embedding model ([D1]), so this is the slow one.
docker build -f backend/Dockerfile.worker -t "${WORKER_IMAGE}:${TAG}" backend
docker push "${WORKER_IMAGE}:${TAG}"

# ---------------------------------------------------------------- migrations

log "Migrations"
if ! exists gcloud run jobs describe "$MIGRATE_JOB" --region "$REGION" --project "$PROJECT_ID"; then
  gcloud run jobs create "$MIGRATE_JOB" \
    --image "${API_IMAGE}:${TAG}" \
    --region "$REGION" --project "$PROJECT_ID" \
    --service-account "$API_SA" \
    --set-secrets "DATABASE_URL=database-url:latest" \
    --command uv --args "run,alembic,upgrade,head" \
    --max-retries 0
else
  gcloud run jobs update "$MIGRATE_JOB" \
    --image "${API_IMAGE}:${TAG}" \
    --region "$REGION" --project "$PROJECT_ID"
fi

# --wait, and the script exits on failure: deploying code against a schema that did not
# migrate is how a deployment becomes an outage.
gcloud run jobs execute "$MIGRATE_JOB" --region "$REGION" --project "$PROJECT_ID" --wait

# ---------------------------------------------------------------- shared configuration

common_env=(
  "APP_ENV=${APP_ENV}"
  "WHATSAPP_PROVIDER=${WHATSAPP_PROVIDER}"
  "LLM_PROVIDER=${LLM_PROVIDER}"
  "EMBEDDING_PROVIDER=${EMBEDDING_PROVIDER}"
  "CALENDAR_PROVIDER=${CALENDAR_PROVIDER}"
  "CRM_PROVIDER=${CRM_PROVIDER}"
  "QUEUE_PROVIDER=${QUEUE_PROVIDER}"
  "GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
  "GOOGLE_CLOUD_LOCATION=${REGION}"
  "GOOGLE_CLOUD_TASKS_QUEUE=${TASKS_QUEUE}"
  "GOOGLE_SECRET_MANAGER_PROJECT=${PROJECT_ID}"
  "GOOGLE_CALENDAR_TOKEN_SECRET_NAME=${SECRET_CALENDAR_TOKEN}"
  "WORKER_SERVICE_ACCOUNT_EMAIL=${INVOKER_SA}"
)

# These are applied with --update-env-vars, not --set-env-vars, and the difference is
# load-bearing. --set replaces the service's entire environment, which would delete every
# variable an operator set by hand — OPENROUTER_MODEL, AUDIENCE_COLUMN_SCHEMA,
# QUALIFICATION_REQUIRED_FIELDS, GOOGLE_CALENDAR_ID, the MSG_* copy. The runbook
# instructs setting exactly those by hand, so the next deploy would silently undo the
# configuration and, under APP_ENV=production, the service would refuse to start.
#
# The cost of --update is that removing a variable from the list above no longer removes
# it from the service; that needs an explicit --remove-env-vars. A deploy that forgets a
# setting is a worse failure than one that leaves a stale variable behind.

# Secret *references*, not values: Cloud Run resolves them at start, so nothing appears
# in the service definition or in `gcloud run services describe` (D-46).
#
# --set-secrets here, deliberately unlike the environment above: this list is complete,
# nothing outside it is set by hand, and being declarative means a secret removed from
# the list is genuinely detached rather than lingering.
common_secrets=(
  "APP_SECRET=app-secret:latest"
  "DATABASE_URL=database-url:latest"
  "WHATSAPP_ACCESS_TOKEN=whatsapp-access-token:latest"
  "WHATSAPP_APP_SECRET=whatsapp-app-secret:latest"
  "WHATSAPP_VERIFY_TOKEN=whatsapp-verify-token:latest"
  "OPENROUTER_API_KEY=openrouter-api-key:latest"
  "GOOGLE_OAUTH_CLIENT_SECRET=google-oauth-client-secret:latest"
  "HUBSPOT_ACCESS_TOKEN=hubspot-access-token:latest"
)

join() { local IFS=,; echo "$*"; }

# ---------------------------------------------------------------- worker
#
# First, because the API needs its URL.
#
# --no-allow-unauthenticated: the worker is reachable only by Cloud Tasks and Cloud
# Scheduler, through the invoker service account's OIDC token. It is not a public
# service and must not become one.
#
# Memory is generous because the worker loads the embedding model in-process ([D1]);
# concurrency is 1 because embedding is CPU-bound and several concurrent turns on one
# instance would queue behind each other anyway.

log "Worker"
gcloud run deploy "$WORKER_SERVICE" \
  --image "${WORKER_IMAGE}:${TAG}" \
  --region "$REGION" --project "$PROJECT_ID" \
  --service-account "$WORKER_SA" \
  --no-allow-unauthenticated \
  --memory 4Gi --cpu 2 --concurrency 1 --timeout 900 \
  --min-instances 0 --max-instances "${WORKER_MAX_INSTANCES:-10}" \
  --update-env-vars "$(join "${common_env[@]}")" \
  --set-secrets "$(join "${common_secrets[@]}")"

WORKER_URL="$(gcloud run services describe "$WORKER_SERVICE" \
  --region "$REGION" --project "$PROJECT_ID" --format 'value(status.url)')"
echo "  worker: $WORKER_URL"

# ---------------------------------------------------------------- API
#
# --allow-unauthenticated because §19 is explicit that the API stays publicly reachable
# for Meta's webhook. Its own authentication (sessions) and the webhook signature check
# are the boundary; being on the public internet is not itself the vulnerability.

log "API"
gcloud run deploy "$API_SERVICE" \
  --image "${API_IMAGE}:${TAG}" \
  --region "$REGION" --project "$PROJECT_ID" \
  --service-account "$API_SA" \
  --allow-unauthenticated \
  --memory 1Gi --cpu 1 --concurrency 80 --timeout 60 \
  --min-instances "${API_MIN_INSTANCES:-1}" --max-instances "${API_MAX_INSTANCES:-10}" \
  --update-env-vars "$(join "${common_env[@]}"),WORKER_BASE_URL=${WORKER_URL}" \
  --set-secrets "$(join "${common_secrets[@]}")"

API_URL="$(gcloud run services describe "$API_SERVICE" \
  --region "$REGION" --project "$PROJECT_ID" --format 'value(status.url)')"

log "Deployed"
cat <<EOF
  API:    $API_URL
  Worker: $WORKER_URL   (not publicly reachable, by design)

Next:
  ./deploy/schedule-sweeps.sh    the sweeps need the worker URL, so they come after this
  ./deploy/smoke-test.sh         verify

Vercel needs INTERNAL_API_BASE_URL=$API_URL so the BFF proxy can reach the API ([D13]).
EOF
