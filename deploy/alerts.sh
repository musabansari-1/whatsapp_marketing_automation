#!/usr/bin/env bash
# Alerting on the failures that are silent by design (§21, D-47).
#
# Several failure modes are deliberately swallowed, each for a good reason:
#
#   §14 forbids a CRM failure surfacing at its caller, so a HubSpot outage is recorded
#   on the lead and nowhere else.
#   §12 keeps a confirmed booking confirmed while its notification retries.
#   An inbound message whose turn fails to enqueue is committed and logged, because
#   raising would reprocess the whole webhook.
#   A document that fails to ingest is marked FAILED and the uploader is not told.
#
# Every one of those is *recorded*. None of them is *noticed*. That is the gap this
# closes: a CRM failing for a week is invisible until somebody opens HubSpot.
#
# Built on the structured events the application already emits (§21) rather than on new
# instrumentation, so there is nothing to keep in sync.
#
#     PROJECT_ID=my-project NOTIFICATION_CHANNEL=projects/.../notificationChannels/123 \
#       ./deploy/alerts.sh
#
# Create a channel first if you have none — email is enough to start:
#     gcloud alpha monitoring channels create --display-name "Ops" \
#       --type email --channel-labels email_address=ops@example.com

source "$(dirname "$0")/config.sh"

: "${NOTIFICATION_CHANNEL:?Set NOTIFICATION_CHANNEL to a Cloud Monitoring channel resource name}"

# A log-based metric counts matching entries; the alert policy fires on the count.
metric() {
  local name="$1" description="$2" filter="$3"

  if exists gcloud logging metrics describe "$name" --project "$PROJECT_ID"; then
    gcloud logging metrics update "$name" \
      --description "$description" --log-filter "$filter" --project "$PROJECT_ID" --quiet
  else
    gcloud logging metrics create "$name" \
      --description "$description" --log-filter "$filter" --project "$PROJECT_ID" --quiet
  fi
  echo "  metric $name"
}

log "Log-based metrics"

# The application logs structlog JSON to stdout, so the event name is a field rather
# than something to pattern-match out of a message string.
BASE='resource.type="cloud_run_revision"'

metric "wa_crm_sync_failed" \
  "CRM synchronisation failures (§14 — deliberately not raised at the caller)" \
  "${BASE} AND jsonPayload.event=\"crm_sync_failed\""

metric "wa_knowledge_ingest_failed" \
  "Documents that failed to ingest — invisible to the AI until fixed (§11)" \
  "${BASE} AND jsonPayload.event=\"knowledge_ingest_failed\""

metric "wa_turn_enqueue_failed" \
  "An inbound message whose conversation turn was never queued (D-15)" \
  "${BASE} AND jsonPayload.event=\"conversation_turn_enqueue_failed\""

metric "wa_sweep_requeued" \
  "The sweep found stranded messages — repair happened, so something broke earlier" \
  "${BASE} AND jsonPayload.event=\"sweep_unanswered_requeued\""

metric "wa_llm_failed" \
  "LLM failures — the customer got the fallback or nothing" \
  "${BASE} AND jsonPayload.event=\"conversation_llm_failed\""

metric "wa_booking_confirmation_failed" \
  "A confirmed booking the customer was never told about (§12)" \
  "${BASE} AND jsonPayload.event=\"booking_confirmation_failed\""

metric "wa_rate_limit_unavailable" \
  "The rate limiter could not be reached, so requests are being refused (§19)" \
  "${BASE} AND jsonPayload.event=\"rate_limit_unavailable\""

# ---------------------------------------------------------------- policies

policy() {
  local display="$1" metric_name="$2" threshold="$3" window="$4"

  # Alert policies have no natural key, so an existing one with the same display name is
  # replaced rather than duplicated. Without this, re-running the script accumulates
  # policies until every incident pages several times.
  local existing
  existing="$(gcloud alpha monitoring policies list \
    --filter "displayName='${display}'" --format 'value(name)' --project "$PROJECT_ID" 2>/dev/null || true)"
  if [ -n "$existing" ]; then
    gcloud alpha monitoring policies delete "$existing" --project "$PROJECT_ID" --quiet
  fi

  cat > /tmp/wa-policy.json <<JSON
{
  "displayName": "${display}",
  "combiner": "OR",
  "conditions": [{
    "displayName": "${display}",
    "conditionThreshold": {
      "filter": "metric.type=\"logging.googleapis.com/user/${metric_name}\" AND resource.type=\"cloud_run_revision\"",
      "comparison": "COMPARISON_GT",
      "thresholdValue": ${threshold},
      "duration": "${window}",
      "aggregations": [{
        "alignmentPeriod": "${window}",
        "perSeriesAligner": "ALIGN_SUM"
      }]
    }
  }],
  "notificationChannels": ["${NOTIFICATION_CHANNEL}"],
  "documentation": {
    "content": "See deploy/README.md — this failure is recorded by the application and does not surface to any user.",
    "mimeType": "text/markdown"
  }
}
JSON

  gcloud alpha monitoring policies create --policy-from-file /tmp/wa-policy.json \
    --project "$PROJECT_ID" --quiet >/dev/null
  rm -f /tmp/wa-policy.json
  echo "  policy $display"
}

log "Alert policies"

# Thresholds are starting points, not measurements. Tune once real traffic shows what
# normal looks like — an alert that fires constantly is one nobody reads.

# Any of these is worth a look even once.
policy "WhatsApp: rate limiter unavailable" "wa_rate_limit_unavailable" 0 "300s"
policy "WhatsApp: conversation turn never queued" "wa_turn_enqueue_failed" 0 "300s"
policy "WhatsApp: booking confirmed but customer not told" "wa_booking_confirmation_failed" 0 "300s"

# These tolerate the occasional transient failure and alert on a pattern.
policy "WhatsApp: CRM sync failing" "wa_crm_sync_failed" 5 "900s"
policy "WhatsApp: document ingestion failing" "wa_knowledge_ingest_failed" 2 "900s"
policy "WhatsApp: LLM failing" "wa_llm_failed" 5 "900s"
policy "WhatsApp: sweep is repairing stranded messages" "wa_sweep_requeued" 0 "3600s"

log "Done"
cat <<'NOTE'
Thresholds are starting points. Revisit them once there is real traffic: an alert that
fires constantly is an alert nobody reads, and one that never fires may simply be
watching the wrong field.

Not covered here, because it needs no log parsing — configure these from the Cloud Run
service pages: request error rate, instance count hitting max, and worker 5xx.
NOTE
