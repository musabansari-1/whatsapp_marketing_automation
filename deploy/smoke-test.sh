#!/usr/bin/env bash
# Post-deployment smoke test (§22, Phase 15).
#
# Answers one question: did this deployment come up correctly? It is not a test suite —
# CI runs 1,250 of those. It checks the things only a real deployment can be wrong
# about: whether the services start, reach the database, hold their secrets, and see
# each other.
#
# **Sends nothing.** Every check is a read or a handshake. CLAUDE.md forbids sending a
# real WhatsApp message outside a deliberate act, and a smoke test that broadcast to a
# customer would be exactly that mistake. The script refuses to run destructive checks
# and says so when it detects real providers.
#
#     PROJECT_ID=my-project ./deploy/smoke-test.sh

source "$(dirname "$0")/config.sh"

PASS=0
FAIL=0

check() {
  local description="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf '  \033[32m✓\033[0m %s\n' "$description"
    PASS=$((PASS + 1))
  else
    printf '  \033[31m✗\033[0m %s\n' "$description"
    FAIL=$((FAIL + 1))
  fi
}

API_URL="$(gcloud run services describe "$API_SERVICE" \
  --region "$REGION" --project "$PROJECT_ID" --format 'value(status.url)' 2>/dev/null || true)"
WORKER_URL="$(gcloud run services describe "$WORKER_SERVICE" \
  --region "$REGION" --project "$PROJECT_ID" --format 'value(status.url)' 2>/dev/null || true)"

if [ -z "$API_URL" ] || [ -z "$WORKER_URL" ]; then
  echo "API or worker is not deployed. Run deploy/deploy.sh first." >&2
  exit 1
fi

log "Smoke test — $PROJECT_ID/$REGION"
echo "  API:    $API_URL"
echo "  Worker: $WORKER_URL"

# ---------------------------------------------------------------- API

log "API"

# /health is dependency-free: it proves the process is up even when the database is not.
check "responds to /health" \
  curl -fsS --max-time 10 "${API_URL}/health"

# /readiness touches the database, so this is really "can the API reach Neon with the
# secret it was given".
check "reaches the database (/readiness)" \
  curl -fsS --max-time 15 "${API_URL}/readiness"

# Read APP_ENV from the deployed service rather than the operator's shell. What is
# actually running is the authoritative answer, and a smoke test that skipped the
# production checks because nobody exported APP_ENV would be worse than useless.
DEPLOYED_ENV="$(gcloud run services describe "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
  --format 'value(spec.template.spec.containers[0].env.filter("name:APP_ENV").extract("value"))' 2>/dev/null || true)"
# Peel the YAML-list brackets --format returns (e.g. ['staging']).
DEPLOYED_ENV="$(printf '%s' "$DEPLOYED_ENV" | tr -d "[]\"'")"
echo "  APP_ENV: ${DEPLOYED_ENV:-unknown}"

# §19: docs are disabled in production. A 200 here means APP_ENV is not what you think.
if [ "$DEPLOYED_ENV" = "production" ]; then
  check "API docs are disabled (§19)" \
    bash -c "! curl -fsS --max-time 10 '${API_URL}/docs'"
fi

# ---------------------------------------------------------------- worker
#
# Not publicly reachable by design, so these carry an identity token — which also
# proves the OIDC path Cloud Tasks depends on actually works.

log "Worker"

# Minted as the operator's identity, without --audiences: that flag only works when
# authenticated as a service account. /health only has to pass IAM — it is not behind
# verify_task_request — so an untargeted token is accepted here. The point of this check
# is the IAM policy, which is what a real Cloud Tasks call also proves; the audience a
# real dispatch is judged on is checked in the task-dispatch section below.
TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"

check "is NOT publicly reachable" \
  bash -c "! curl -fsS --max-time 10 '${WORKER_URL}/health'"

check "responds to an authenticated /health" \
  curl -fsS --max-time 10 -H "Authorization: Bearer ${TOKEN}" "${WORKER_URL}/health"

check "reaches the database (/readiness)" \
  curl -fsS --max-time 20 -H "Authorization: Bearer ${TOKEN}" "${WORKER_URL}/readiness"

# The worker reports which task types it has handlers for. A missing one means a
# registration was lost — the kind of thing that otherwise shows up as work silently
# never running.
EXPECTED_TASKS=(
  send_campaign_message
  process_inbound_message
  process_conversation_turn
  ingest_knowledge_document
  notify_booking
  sync_crm_contact
  sync_whatsapp_templates
  sweep_unanswered_messages
  sweep_rate_limit_counters
  sweep_expired_sessions
)

REGISTERED="$(curl -fsS --max-time 10 -H "Authorization: Bearer ${TOKEN}" "${WORKER_URL}/health" 2>/dev/null || echo '{}')"
for task in "${EXPECTED_TASKS[@]}"; do
  check "handler registered: $task" bash -c "echo '$REGISTERED' | grep -q '$task'"
done

# ---------------------------------------------------------------- task dispatch
#
# Everything above can pass while no customer message reaches the worker. The API
# dispatches to its own WORKER_BASE_URL, the worker accepts a task only when the token's
# audience equals *its* WORKER_BASE_URL — two strings, in two services, that nothing
# else here compares — and the sweeps carry a third copy, baked in when their jobs were
# created. Any of the three can name a host Cloud Run does not serve, or disagree with
# the others, and the only symptom is silence. That was 2026-09-22: eighteen hours of
# unanswered messages, every service healthy (see resolve_service_url in config.sh).

log "Task dispatch (§18, §20)"

API_WORKER_URL="$(deployed_env_value "$API_SERVICE" WORKER_BASE_URL)"
WORKER_BASE_URL="$(deployed_env_value "$WORKER_SERVICE" WORKER_BASE_URL)"
echo "  API dispatches to: ${API_WORKER_URL:-unset}"
echo "  Worker expects:    ${WORKER_BASE_URL:-unset}"

check "the API has a worker URL to dispatch to" \
  bash -c "[ -n '$API_WORKER_URL' ]"

# Audiences are compared character for character, so the numbered and alias forms of the
# same service do not match — these must be one identical string, not two live hostnames.
check "the worker and the API agree on it (OIDC audience)" \
  bash -c "[ -n '$API_WORKER_URL' ] && [ '$WORKER_BASE_URL' = '$API_WORKER_URL' ]"

# The check that would have caught 09-22. Anonymous is the right shape: IAM answers 403
# for a private service that exists and 404 for a hostname Cloud Run does not serve, so
# no token is needed to tell "exists" from "fabricated".
DISPATCH_CODE="$(http_status "${API_WORKER_URL}/health")"
check "the API's worker URL reaches a host that exists (HTTP $DISPATCH_CODE)" \
  bash -c "[ '$DISPATCH_CODE' = '401' ] || [ '$DISPATCH_CODE' = '403' ] || [ '$DISPATCH_CODE' = '200' ]"

for job in wa-sweep-unanswered wa-sweep-rate-limits wa-sweep-sessions; do
  if gcloud scheduler jobs describe "$job" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
    AUDIENCE="$(gcloud scheduler jobs describe "$job" --location "$REGION" --project "$PROJECT_ID" \
      --format 'value(httpTarget.oidcToken.audience)' 2>/dev/null || true)"
    check "sweep audience matches: $job" bash -c "[ '$AUDIENCE' = '$API_WORKER_URL' ]"
  else
    echo "  – $job is not scheduled yet (run deploy/schedule-sweeps.sh)"
  fi
done

# ---------------------------------------------------------------- webhook
#
# The verification handshake only — never a delivery. Meta calls this to confirm the
# endpoint; answering it correctly proves the verify token secret arrived.

log "Webhook (§8)"

VERIFY_TOKEN="$(gcloud secrets versions access latest \
  --secret whatsapp-verify-token --project "$PROJECT_ID" 2>/dev/null || true)"

if [ -n "$VERIFY_TOKEN" ]; then
  CHALLENGE="smoke-$RANDOM"
  check "answers the verification handshake" \
    bash -c "curl -fsS --max-time 10 '${API_URL}/api/v1/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=${VERIFY_TOKEN}&hub.challenge=${CHALLENGE}' | grep -q '${CHALLENGE}'"

  check "rejects a wrong verify token" \
    bash -c "! curl -fsS --max-time 10 '${API_URL}/api/v1/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=x'"
else
  echo "  – skipped: whatsapp-verify-token has no value yet"
fi

# An unsigned POST must be refused (§8, §19). This is a *rejection* test — nothing is
# processed and nothing is sent.
check "rejects an unsigned webhook delivery" \
  bash -c "! curl -fsS --max-time 10 -X POST '${API_URL}/api/v1/webhooks/whatsapp' -H 'Content-Type: application/json' -d '{}'"

# ---------------------------------------------------------------- auth and limits

log "Authentication (§19)"

# /users/me, not /leads: list routes are product-scoped (Phase 26), so a bare /leads
# does not exist and answers 404. An ownerless route is what proves session refusal.
check "an unauthenticated dashboard call is refused" \
  bash -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 '${API_URL}/api/v1/users/me')\" = '401' ]"

# Rate limiting: deliberately over-attempt a login with a nonsense account. It creates
# no session and reveals nothing; it proves §19's limiter is active in this deployment.
log "Rate limiting (§19, D-42)"
SAW_429=0
# A domain EmailStr accepts: reserved TLDs like .local are rejected at validation
# with 422 before the request ever reaches the limiter.
for _ in $(seq 1 25); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    -X POST "${API_URL}/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d '{"email":"smoke-test@smoke.invalid-example.com","password":"not-a-real-password"}')"
  if [ "$code" = "429" ]; then SAW_429=1; break; fi
done
check "refuses repeated failed logins" bash -c "[ '$SAW_429' = '1' ]"

# ---------------------------------------------------------------- configuration

log "Configuration"

for var in WHATSAPP_PROVIDER LLM_PROVIDER CALENDAR_PROVIDER CRM_PROVIDER; do
  # --format returns a YAML list (e.g. ['fake']); peel the brackets for comparison.
  value="$(gcloud run services describe "$API_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
    --format "value(spec.template.spec.containers[0].env.filter(\"name:${var}\").extract(\"value\"))" 2>/dev/null || true)"
  value="$(printf '%s' "$value" | tr -d "[]\"'")"
  echo "  $var = ${value:-unset}"
  if [ "$value" != "fake" ] && [ -n "$value" ]; then
    printf '    \033[33m! real provider — a broadcast from this deployment reaches real people\033[0m\n'
  fi
done

# ---------------------------------------------------------------- result

log "Result"
echo "  passed: $PASS"
echo "  failed: $FAIL"

if [ "$FAIL" -gt 0 ]; then
  cat <<'FAILED'

Something is wrong. Where to look:

  /readiness failing      the database secret, or Neon's IP allow-list
  worker unreachable      the invoker service account lacks roles/run.invoker
  handler missing         an incomplete image; check the tag that was deployed
  dispatch failing        WORKER_BASE_URL differs between the two services, or names a
                          host Cloud Run does not serve — compare the values printed
                          above, then re-run deploy.sh to give both the same one
  sweep audience          a scheduler job was created against an older URL; re-run
                          deploy/schedule-sweeps.sh
  webhook handshake       whatsapp-verify-token has no value, or the wrong one
  no rate limiting        RATE_LIMIT_ENABLED is false, or the counters table is missing

  gcloud run services logs read wa-api --region REGION --project PROJECT --limit 50
FAILED
  exit 1
fi

echo
echo "  All checks passed. Nothing was sent."
