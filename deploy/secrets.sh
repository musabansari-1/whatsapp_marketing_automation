#!/usr/bin/env bash
# Store credentials in Secret Manager (§19, [D19], D-46).
#
# Interactive on purpose. It reads each value from the terminal without echoing and adds
# it as a new secret version — so no credential passes through a command line (where it
# would land in shell history and in the process list), a file, or this repository.
#
# Re-runnable: adding a version supersedes the previous one. Rotating a credential means
# running this again for that one secret.
#
#     PROJECT_ID=my-project ./deploy/secrets.sh              # all of them
#     PROJECT_ID=my-project ./deploy/secrets.sh openrouter-api-key   # just one

source "$(dirname "$0")/config.sh"

set_secret() {
  local name="$1" prompt="$2"

  if ! exists gcloud secrets describe "$name" --project "$PROJECT_ID"; then
    echo "  $name does not exist — run setup-infrastructure.sh first" >&2
    return 1
  fi

  printf '\n%s\n' "$prompt"
  # -s: not echoed. -r: a backslash in a token is a backslash, not an escape.
  read -rs -p "  $name: " value
  echo

  if [ -z "$value" ]; then
    echo "  skipped (empty)"
    return 0
  fi

  # printf, not echo: no trailing newline. A newline inside a bearer token produces an
  # authentication failure that looks like a wrong credential and wastes an afternoon.
  printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- --project "$PROJECT_ID"
  echo "  stored"
}

declare -A PROMPTS=(
  ["app-secret"]="Application secret. Generate one: openssl rand -hex 32"
  ["database-url"]="Neon connection string, e.g. postgresql+asyncpg://user:pass@host/db"
  ["whatsapp-access-token"]="Meta access token ([D22])"
  ["whatsapp-app-secret"]="Meta App Secret — verifies webhook signatures (§8)"
  ["whatsapp-verify-token"]="Meta verify token — your own value, given to Meta"
  ["openrouter-api-key"]="OpenRouter API key ([D10])"
  ["google-oauth-client-secret"]="Google OAuth client secret ([D19])"
  ["hubspot-access-token"]="HubSpot private app token (§14)"
  ["google-calendar-refresh-token"]="Google Calendar refresh token — see oauth-consent.md ([D19])"
)

if [ $# -gt 0 ]; then
  for name in "$@"; do
    set_secret "$name" "${PROMPTS[$name]:-Value for $name}"
  done
else
  log "Storing all credentials"
  for name in "${SECRET_APP[@]}" "$SECRET_CALENDAR_TOKEN"; do
    set_secret "$name" "${PROMPTS[$name]:-Value for $name}"
  done
fi

cat <<'NOTE'

Stored. Two reminders:

  The Google Calendar refresh token is not something you can paste from a settings
  page — it comes from a one-time OAuth consent flow. See deploy/oauth-consent.md.

  A rotated credential needs this script re-run for that secret. The services read
  secrets at startup, so they need a new revision to pick it up: deploy/deploy.sh.
NOTE
