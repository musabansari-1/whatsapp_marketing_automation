# Google Calendar: how the platform authenticates ([D19], [D62])

## Production path — the service account (preferred)

The worker authenticates as **itself**, with the identity Cloud Run already gives it,
and each product's calendar is shared with that service account. There is no refresh
token, so nothing expires, nothing is written back, and no human re-consent can ever
take booking down.

Why this is the default recommendation: the OAuth path below is a human's grant. An
OAuth consent screen in *Testing* status — the default when the client was created —
issues refresh tokens that **expire after 7 days** (2026-09-15 → 2026-09-22 on staging
was exactly this). Leaving Testing means Google's sensitive-scope review for
`.../auth/calendar`, and even a verified app still dies when the human changes a
password, revokes the grant, or their account is closed. None costs anything — a
service account is an IAM identity in this project, not a user — and calendar sharing
is a free Calendar action, well within the API's free quota.

One-time setup, per product calendar:

1. Open that product's calendar settings in Google Calendar → **Share with specific
   people** → add the worker's service account email (the `WORKER_SA` in
   `deploy/config.sh`, e.g. `wa-worker@verdant-victory-508614-v8.iam.gserviceaccount.com`)
   → permission that lets it **see all event details and make changes to events**.
2. Set `GOOGLE_CALENDAR_AUTH_MODE=service_account` on the services:
   `GOOGLE_CALENDAR_AUTH_MODE=service_account PROJECT_ID=my-project ./deploy/deploy.sh`
3. Verify: `PROJECT_ID=my-project ./deploy/smoke-test.sh`, then ask a conversation for
   slots and confirm the worker logs show no `ai_slot_lookup_failed`.

Reverting is one deploy variable flip — `GOOGLE_CALENDAR_AUTH_MODE=oauth_refresh_token` —
so nothing about this is a one-way door. Keeping the OAuth client and its secret
configured while on this path is harmless and recommended as the rollback.

## Fallback — the OAuth refresh token (local development)

**This is the one step on the critical path that a person has to perform.** It cannot be
scripted, delegated to a service account, or done by the platform — Google requires a
human to click "Allow" once, and what comes back is the refresh token everything else
depends on ([D19], D-27).

Ten minutes, once per environment. Until it is done, every booking in this mode fails
with *"No Google refresh token in secret … Complete the OAuth consent flow and store the
refresh token."* Even once done, a consent screen in **Testing** status silently expires
the token **7 days** later — check `GOOGLE_CALENDAR_AUTH_MODE=service_account` above
first if you are deploying production, especially with a personal Gmail account, which
has no Internal mode to avoid it.

## Before you start

Decide **which calendar** (D-28). This matters more than it looks: the platform treats
every busy block on that calendar as unavailable and offers everything else.

- An **existing team calendar** gives real availability, but unrelated meetings will
  suppress demo slots.
- A **new dedicated calendar** gives clean availability, but will offer times the
  salesperson is actually in another meeting.
- A **dedicated calendar with each salesperson's own calendar shared into it** as busy
  time is the best of both, if the client's Google Workspace permits it.

Nobody has chosen yet. Decide before you authorise, because the choice determines which
account should grant consent.

## 1. Create the OAuth client

In the Google Cloud console, under **APIs & Services → Credentials**:

1. Enable the **Google Calendar API** for the project.
2. Create an **OAuth client ID**, application type **Desktop app**.
3. Note the **Client ID** and **Client secret**.

Desktop app rather than Web application: it permits the loopback redirect used below,
which avoids standing up a web server just to catch one redirect.

If the consent screen asks for a publishing status, **Internal** is correct for a
Workspace organisation tool. External triggers Google's verification review for the
calendar scope, which takes weeks. Neither applies to a personal-Gmail account, which
has no Internal mode — use the service-account path above instead of this flow.

## 2. Sign in as the right account

The consent must be granted by an account that can **read and write the calendar chosen
above**. Whoever clicks "Allow" is whose access the platform inherits — if that person
leaves the company and their account is deleted, booking stops working.

**Prefer a shared operations account over an individual's**, for exactly that reason.

## 3. Authorise

Replace `YOUR_CLIENT_ID` and open this in a browser:

```
https://accounts.google.com/o/oauth2/v2/auth?client_id=YOUR_CLIENT_ID&redirect_uri=http://localhost&response_type=code&scope=https://www.googleapis.com/auth/calendar&access_type=offline&prompt=consent
```

Two parameters are doing real work:

- `access_type=offline` is what makes Google issue a **refresh** token rather than only
  a short-lived access token.
- `prompt=consent` forces the refresh token to be re-issued even if this account has
  authorised before. Without it, a second attempt returns no refresh token and the step
  appears to fail for no reason.

Approve. The browser will fail to load `http://localhost` — that is expected and fine.
The address bar now contains what you need:

```
http://localhost/?code=4/0AY0e-g7...&scope=...
```

Copy the `code` value. **It expires in a few minutes**, so do the next step now.

## 4. Exchange the code for a refresh token

```bash
curl -s https://oauth2.googleapis.com/token \
  -d client_id=YOUR_CLIENT_ID \
  -d client_secret=YOUR_CLIENT_SECRET \
  -d code=THE_CODE_FROM_THE_URL \
  -d grant_type=authorization_code \
  -d redirect_uri=http://localhost
```

The response contains `refresh_token`. **That value is what you need** — the
`access_token` alongside it expires in an hour and the platform mints its own.

If `refresh_token` is absent, this account has authorised before and `prompt=consent`
was missing from step 3. Repeat step 3 with it.

## 5. Store it

```bash
PROJECT_ID=my-project ./deploy/secrets.sh google-calendar-refresh-token
```

Also store the client secret, and set the client ID as ordinary configuration:

```bash
PROJECT_ID=my-project ./deploy/secrets.sh google-oauth-client-secret
```

Then set `GOOGLE_OAUTH_CLIENT_ID` on the services, record each product's calendar in
`products.google_calendar_id` (Phase 24 — there is no service-wide calendar variable, and a
product without a calendar refuses bookings), share every product calendar with the
operations account that gave consent, and switch `CALENDAR_PROVIDER=google` with
`GOOGLE_CALENDAR_AUTH_MODE=oauth_refresh_token`.

## 6. Verify

```bash
PROJECT_ID=my-project ./deploy/smoke-test.sh
```

Then check the calendar reads. The clearest test is to ask the worker for availability
and confirm it reflects the real diary — a calendar the credentials cannot see is
**refused** rather than treated as empty, deliberately, because reading it as empty
would offer every slot and double-book the team.

## Afterwards

**The platform rewrites this secret.** When Google rotates the refresh token, the worker
stores the new one as a fresh version — which is why the worker's service account needs
`secretmanager.secretVersionAdder` and not only read access. If that permission is
missing, everything works until the rotation and then fails everywhere at once.

Nothing needs re-doing unless the token is revoked, the authorising account is deleted,
or the OAuth client is removed.
