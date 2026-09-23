#!/usr/bin/env bash
# Scheduled recovery sweeps (§20, D-15, D-43).
#
# The sweep handlers were built in Phase 14 and nothing triggers them. This is what
# does. Run *after* deploy.sh, because the jobs need the worker's URL.
#
# Cron rather than event-driven, because the failures these repair are the *absence* of
# an event: a conversation turn whose enqueue failed leaves nothing behind to react to.
#
# Each job posts the same envelope Cloud Tasks would, to the same endpoint, with an OIDC
# token from the same invoker service account. The worker cannot tell the difference and
# does not need to.
#
#     PROJECT_ID=my-project ./deploy/schedule-sweeps.sh

source "$(dirname "$0")/config.sh"

# Read from Cloud Run through the same helper deploy.sh uses, so the sweeps address the
# host the worker actually answers on. This used to rewrite the a.run.app alias into
# SERVICE-HASH.REGION.run.app, which is not a hostname Cloud Run serves — the same
# mistake that broke task dispatch on 2026-09-22, in a second place.
WORKER_URL="${WORKER_URL:-$(resolve_service_url "$WORKER_SERVICE" || true)}"

if [ -z "$WORKER_URL" ]; then
  echo "Worker not deployed. Run deploy/deploy.sh first." >&2
  exit 1
fi

TASK_ENDPOINT="${WORKER_URL}/internal/tasks"

# `verify_task_request` compares the OIDC audience against the worker's own
# WORKER_BASE_URL character for character, and deploy.sh sets that to exactly this
# value. So the audience is this URL and not a re-derived one: the scheduler jobs are
# created once, long before anyone notices they 401 every fifteen minutes.
SWEEP_AUDIENCE="${SWEEP_AUDIENCE:-$WORKER_URL}"


schedule() {
  local name="$1" cron="$2" task_type="$3" description="$4"

  # A fixed task_id, unlike a real queued task. These sweeps are periodic and
  # idempotent — two overlapping runs find the same work and enqueue the same
  # deterministic turn tasks, which the queue collapses.
  local body
  body="$(printf '{"task_type":"%s","task_id":"scheduled-%s"}' "$task_type" "$task_type")"

  local action="create"
  if exists gcloud scheduler jobs describe "$name" --location "$REGION" --project "$PROJECT_ID"; then
    action="update"
  fi

  gcloud scheduler jobs "$action" http "$name" \
    --location "$REGION" --project "$PROJECT_ID" \
    --schedule "$cron" \
    --time-zone "$SCHEDULER_TIMEZONE" \
    --uri "$TASK_ENDPOINT" \
    --http-method POST \
    --headers "Content-Type=application/json" \
    --message-body "$body" \
    --oidc-service-account-email "$INVOKER_SA" \
    --oidc-token-audience "$SWEEP_AUDIENCE" \
    --description "$description" \
    --attempt-deadline 600s \
    --quiet

  echo "  $action $name — $cron"
}

log "Scheduling sweeps against $TASK_ENDPOINT"

# Every fifteen minutes. The sweep only acts on messages older than
# SWEEP_UNANSWERED_GRACE_SECONDS (ten minutes by default), so a stranded customer waits
# at most about twenty-five minutes for a reply that was lost — long enough not to race
# a task still in flight, short enough that the reply is still relevant.
schedule "wa-sweep-unanswered" "$SWEEP_UNANSWERED_SCHEDULE" \
  "sweep_unanswered_messages" \
  "Re-enqueue conversation turns that were never queued (D-15)"

# Hourly is ample: these rows are already unreachable, and this is housekeeping rather
# than correctness (§19).
schedule "wa-sweep-rate-limits" "$SWEEP_COUNTERS_SCHEDULE" \
  "sweep_rate_limit_counters" \
  "Delete expired rate limit counters (§19)"

# Daily. Expiry is enforced when a session is read ([D2]), so an expired row grants
# nothing whether it is deleted or not — this is growth control on the one table every
# authenticated request touches (D-43).
schedule "wa-sweep-sessions" "$SWEEP_SESSIONS_SCHEDULE" \
  "sweep_expired_sessions" \
  "Delete expired session rows (D-43)"

log "Scheduled"
cat <<EOF
Run one now to check it works, rather than waiting for the cron:

  gcloud scheduler jobs run wa-sweep-unanswered --location $REGION --project $PROJECT_ID

Then read the result. A healthy empty sweep logs sweep_unanswered_none:

  gcloud logging read \\
    'resource.type=cloud_run_revision AND jsonPayload.event=~"sweep_"' \\
    --limit 10 --project $PROJECT_ID

Note: sweep_unanswered_requeued is logged at WARNING even when it succeeds. Finding
anything means a turn was lost earlier, which is worth noticing even though the sweep
has just repaired it — deploy/alerts.sh raises on it.
EOF
