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
# Embedding is an internal provider, not one of the six external ones above. The default
# matches [D1]: the worker embeds in-process with the model baked into its image.
EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-local_sentence_transformer}"
# Per-service split. The API image is deliberately slim — it carries no PyTorch and no
# code path embeds in the API (retrieval and ingestion are worker work) — so it is pinned
# to the fake below regardless of the value above. The worker honours the value above
# unless this override says otherwise, e.g. WORKER_EMBEDDING_PROVIDER=fake for a
# torch-free worker image.
WORKER_EMBEDDING_PROVIDER="${WORKER_EMBEDDING_PROVIDER:-${EMBEDDING_PROVIDER}}"
CALENDAR_PROVIDER="${CALENDAR_PROVIDER:-fake}"
# [D62]: how the platform proves its identity to Google. `service_account` (production)
# uses the worker's own Cloud Run identity — each product's calendar must be shared
# with it — and `oauth_refresh_token` (fallback / local) uses the refresh token in
# Secret Manager via a human consent flow. Flipping this is the entire migration;
# no redeploy of another variable is required.
GOOGLE_CALENDAR_AUTH_MODE="${GOOGLE_CALENDAR_AUTH_MODE:-oauth_refresh_token}"
CRM_PROVIDER="${CRM_PROVIDER:-fake}"
QUEUE_PROVIDER="${QUEUE_PROVIDER:-cloud_tasks}"

log "Deploying $TAG to $APP_ENV in $PROJECT_ID/$REGION"
echo "  providers: whatsapp=$WHATSAPP_PROVIDER llm=$LLM_PROVIDER calendar=$CALENDAR_PROVIDER($GOOGLE_CALENDAR_AUTH_MODE) crm=$CRM_PROVIDER"

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
  # Not in the shared list: the API and the worker need different values on their slim
  # and torch-carrying images, so each service's deploy line sets its own below.
  "CALENDAR_PROVIDER=${CALENDAR_PROVIDER}"
  "GOOGLE_CALENDAR_AUTH_MODE=${GOOGLE_CALENDAR_AUTH_MODE}"
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
# variable an operator set by hand — OPENROUTER_MODEL, DISPATCH_RATE_PER_SECOND, the MSG_*
# copy. The runbook instructs setting exactly those by hand, so the next deploy would
# silently undo the configuration, and a service whose provider needs OPENROUTER_MODEL would
# refuse to start. (What belongs to one product — its number, calendar, spreadsheet format
# and prompt — is not an environment variable at all since Phase 27. It lives on the
# product's rows; see backend/config/README.md.)
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

# ---------------------------------------------------------------- worker URL
#
# Resolved before the worker is deployed, because the worker needs it as much as the API
# does: it verifies each task's OIDC audience against WORKER_BASE_URL
# (app/worker/security.py), and `Settings` refuses to start with the Cloud Tasks queue
# selected and that variable unset. The two services must hold the *same* string or the
# worker rejects every task with a 401 — the API's copy is the address it dispatches to,
# the worker's copy is what it compares the token against. Resolving once, here, and
# giving both services the result is what stops those two facts from drifting apart.
#
# Read from Cloud Run rather than rewritten from `status.url`; see resolve_service_url in
# config.sh for why, and for what the rewrite cost on 2026-09-22.
WORKER_URL="$(resolve_service_url "$WORKER_SERVICE" 2>/dev/null \
  || canonical_service_url "$WORKER_SERVICE")"
echo "  worker: $WORKER_URL"

# ---------------------------------------------------------------- worker
#
# First, because the API needs its URL.
#
# --no-allow-unauthenticated: the worker is reachable only by Cloud Tasks and Cloud
# Scheduler, through the invoker service account's OIDC token. It is not a public
# service and must not become one.
#
# Memory is generous because the worker loads the embedding model in-process ([D1]).
# Concurrency comes from config.sh: turns are I/O-bound on the model, so a small
# overlap beats strict serialization (measured: ~10s queue waits at 1).
#
log "Worker"
gcloud run deploy "$WORKER_SERVICE" \
  --image "${WORKER_IMAGE}:${TAG}" \
  --region "$REGION" --project "$PROJECT_ID" \
  --service-account "$WORKER_SA" \
  --no-allow-unauthenticated \
  --memory 4Gi --cpu 2 --concurrency "${WORKER_CONCURRENCY:-3}" --timeout 900 \
  --min-instances "${WORKER_MIN_INSTANCES:-1}" --max-instances "${WORKER_MAX_INSTANCES:-10}" \
  --update-env-vars "$(join "${common_env[@]}"),EMBEDDING_PROVIDER=${WORKER_EMBEDDING_PROVIDER},WORKER_BASE_URL=${WORKER_URL}" \
  --set-secrets "$(join "${common_secrets[@]}")"

# The first deployment of a project has no service URL to read, so the value above came
# from the canonical form. Now that the service exists, ask Cloud Run what it actually
# serves and correct the worker before the API is told to dispatch to it. On any later
# deploy the two already agree and this is a no-op — no extra revision.
RESOLVED_WORKER_URL="$(resolve_service_url "$WORKER_SERVICE")"
if [ "$RESOLVED_WORKER_URL" != "$WORKER_URL" ]; then
  echo "  correcting: $WORKER_URL -> $RESOLVED_WORKER_URL"
  WORKER_URL="$RESOLVED_WORKER_URL"
  gcloud run services update "$WORKER_SERVICE" \
    --region "$REGION" --project "$PROJECT_ID" \
    --update-env-vars "WORKER_BASE_URL=${WORKER_URL}" --quiet
fi

# Prove the URL names a host before it is written into the API. 404 is Cloud Run telling
# us it serves no such hostname, so a fabricated URL fails the deploy here instead of
# silently swallowing every customer message from a service that reports itself healthy.
# Any other answer — 403 for the private service refusing an anonymous caller, a 5xx
# while a revision is still warming — proves the request reached Cloud Run under that
# name, which is the question being asked.
WORKER_PROBE="$(http_status "${WORKER_URL}/health")"
case "$WORKER_PROBE" in
  404)
    cat >&2 <<EOF

ERROR: ${WORKER_URL} is not a host Cloud Run serves for ${WORKER_SERVICE} (HTTP 404).
       Deploying now would point every task at nothing. Read what Cloud Run advertises
       and set WORKER_BASE_URL by hand on both services, then re-run:

         gcloud run services describe ${WORKER_SERVICE} --region ${REGION} \\
           --project ${PROJECT_ID} \\
           --format 'value(metadata.annotations."run.googleapis.com/urls")'
EOF
    exit 1
    ;;
  000)
    # No answer of any kind. That is a fact about this machine's route to Cloud Run, not
    # about the URL, so it is worth saying and not worth refusing to deploy over.
    echo "  warning: ${WORKER_URL} answered nothing (curl ${WORKER_PROBE}); cannot confirm" >&2
    echo "           the dispatch target from here. deploy/smoke-test.sh checks it again." >&2
    ;;
  *)
    echo "  dispatch target answered ($WORKER_PROBE; a 403 is the private worker, as designed)"
    ;;
esac

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
  --update-env-vars "$(join "${common_env[@]}"),WORKER_BASE_URL=${WORKER_URL},EMBEDDING_PROVIDER=fake" \
  --set-secrets "$(join "${common_secrets[@]}")"

API_URL="$(gcloud run services describe "$API_SERVICE" \
  --region "$REGION" --project "$PROJECT_ID" --format 'value(status.url)')"

log "Deployed"
cat <<EOF
  API:    $API_URL
  Worker: $WORKER_URL   (not publicly reachable, by design)

The worker URL above is WORKER_BASE_URL on *both* services and the OIDC audience the
sweeps are scheduled with. Change one without the others and tasks are rejected; that
is what deploy/smoke-test.sh's task-dispatch checks exist to catch.

Next:
  ./deploy/schedule-sweeps.sh    the sweeps need the worker URL, so they come after this
  ./deploy/smoke-test.sh         verify

The dashboard (Cloudflare Workers) needs INTERNAL_API_BASE_URL=$API_URL as a Worker
secret so the BFF proxy can reach the API ([D13]): from frontend/,
echo "$API_URL" | npx wrangler secret put INTERNAL_API_BASE_URL
EOF
