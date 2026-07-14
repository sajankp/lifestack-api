# Spec-081: PostHog Observability and Resend Email Channel

**Created:** 2026-07-14
**Status:** Implemented (2026-07-14) — inert without `POSTHOG_API_KEY` / `VITE_POSTHOG_KEY` / `RESEND_API_KEY`+`EMAIL_ENABLED` env keys; owner still owns vendor-dashboard setup
**Depends on:** spec-052 (notification preferences: `channel_email` already exists, default False), spec-067 (briefing push default)

## Problem

Lifestack has no error tracking, no product analytics, and no email delivery:

- Frontend errors in production die silently in the browser console. The API has
  structured logging (structlog), but those logs stay inside the Docker stack and
  unhandled exceptions are not aggregated or alerted anywhere.
- Notifications are web-push only (`app/notifications/push.py`). The morning
  briefing and medication reminder jobs cannot reach the user when no push
  subscription is active (browser closed, permission revoked, new device).
  `NotificationPreference.channel_email` has existed since spec-052 but no code
  path delivers on it.

Both gaps are solvable at **zero recurring cost** on current free tiers
(verified 2026-07-14): PostHog free tier gives 1M analytics events + 100K
error-tracking exceptions per month with hard drop (no overage billing) on the
free plan; Resend free tier gives 100 emails/day, 3,000/month with one verified
domain. A single-user deployment sits orders of magnitude below all of these
limits. Decision (owner, 2026-07-14): **PostHog only** for analytics + error
tracking — no Sentry, to avoid a second SDK and dashboard.

## Solution

Two independent workstreams sharing one spec because both are "wire an external
free-tier service behind env-var config, off by default".

### A. PostHog

Initialize `posthog-js` in the web app, gated on config so dev/e2e/CI never
send events:

- Init in `src/main.tsx` only when `VITE_POSTHOG_KEY` is set (absent in dev,
  test, and e2e environments — zero behavior change there).
- **Error tracking:** enable PostHog exception autocapture so unhandled errors
  and promise rejections are reported.
- **Analytics:** pageview capture plus a small set of explicit events
  (candidate initial set: `login`, `import_completed`, `transfer_created`,
  `capture_session_started`). Exact event list is an implementation detail;
  the gate is that every event is explicit — see privacy below.
- **Privacy (binding):** `autocapture: false`, session recording off,
  `person_profiles: 'identified_only'`. No financial values, captured text, or
  entity names may appear in event properties — event names + counts only.
  Identify by user public id, never email.

**Backend (exception capture only).** Frontend capture cannot see server-side
failures with no browser in the loop: scheduled jobs (briefing, price refresh,
medication reminders, import workers) and API 500s currently reach structlog
and stop there. Add the `posthog` Python SDK (new dependency, proposed here
per the dependency rule) for **error tracking only**:

- Init in `app/main.py` only when `POSTHOG_API_KEY` is set (dev/test/e2e/CI
  never set it — zero behavior change there, and tests need no mocking).
- Capture unhandled API exceptions (exception-handler/middleware hook) and
  scheduled-job failures (the jobs' existing catch-and-log points also call
  `posthog.capture_exception`).
- Capture must never raise into the request/job path, and payloads follow the
  same privacy rule: exception type/trace + route/job name — no request
  bodies, no financial values, no PII.
- **No server-side behavior analytics** — user actions are captured once, in
  the frontend. structlog remains the API's logging system of record.

This does NOT satisfy the owner plan's P5 observability-infrastructure item —
traces and logs are scoped separately in spec-082
(OpenTelemetry instrumentation exporting to PostHog's OTLP endpoint) and
implemented independently after this spec.

### B. Resend email channel (api only)

Deliver notifications by email when `channel_email` is enabled, mirroring the
existing push pattern:

- New module `app/notifications/email.py` exposing a module-level
  `send_email(to, subject, html) -> EmailResult` (same patchable-function shape
  as `push.py::send_web_push`). Implementation is a plain
  `POST https://api.resend.com/emails` via the existing `httpx` dependency —
  **no new backend package**.
- `NotificationService.notify` gains an email leg parallel to the push leg:
  when `pref.channel_email` is true, create a pending email delivery row;
  the existing delivery-drain job in `app/application/jobs.py` sends it.
  Failures are logged and marked failed — never raised into the calling job
  (same isolation guarantee as push).
- Recipient is the user's registered account email. Content mirrors the
  notification title/body with a minimal HTML template (no per-category
  templates in v1).
- Config (api `Settings`): `RESEND_API_KEY` (unset → email channel inert,
  deliveries marked skipped), `EMAIL_FROM_ADDRESS`, `EMAIL_ENABLED`
  (explicit master switch, default False, so a copied `.env` can't
  accidentally send). Update `docs/PRODUCTION_DEPLOYMENT.md` and the
  config catalog in the same pass.
- Rate safety: free tier is 100/day. Notification volume for a single user is
  a handful/day; no queue throttling in v1, but the drain job caps email sends
  per run (defensive constant, e.g. 50) so a pathological loop cannot burn the
  quota or spam the inbox.

### Maintainer-side setup (not code)

Owner handles all vendor-dashboard work: PostHog project creation (US/EU cloud
choice), Resend account, domain verification DNS records, and API keys placed
in production env files. Code lands fully inert until those keys exist.

## Backend impact / API / schema impact

- No new tables. Email deliveries reuse the existing notification-delivery
  structure; if the current pending-delivery table is push-specific, add a
  `channel` discriminator column via Alembic (with working `downgrade()`)
  rather than a new table — resolve during implementation, escalate if it
  turns into more than one column.
- No API contract changes. Preferences UI already exposes `channel_email`
  through existing endpoints (verify; if the toggle is hidden in the web UI,
  un-hide it as part of workstream B).
- New deps: `posthog-js` (web) and `posthog` (api, exception capture only).
  Resend adds none (plain `httpx` call).
- New api settings: `POSTHOG_API_KEY` + `POSTHOG_HOST` (unset ⇒ fully inert),
  alongside the Resend settings above.

## Testing

- Red first, per change control. Backend: unit tests for `send_email` payload
  shape (httpx mocked), service tests for the email leg (preference on/off,
  `EMAIL_ENABLED` off ⇒ skipped, failure isolation), job test for the drain
  cap; exception-capture tests proving the hook fires on an unhandled error,
  stays inert without `POSTHOG_API_KEY`, and never raises into the request/job
  path (SDK mocked). Web: test that PostHog does not initialize without `VITE_POSTHOG_KEY`
  (the dev/e2e safety property) and that tracked events fire from the chosen
  actions with property-free payloads.
- E2E: no new specs — both integrations are inert in the e2e stack by
  construction (no keys set); existing suite proves no regression.

## Out of scope

- Sentry, or any second error-tracking service (owner decision: PostHog only).
- Backend PostHog *behavior* analytics (backend scope is exception capture
  only).
- OpenTelemetry traces and log shipping to PostHog — deliberately split into
  spec-082 (separate spec, separate implementation; owner decision
  2026-07-14). structlog stays the API's logging story until spec-082 lands.
- Session replay, feature flags, surveys, or any other PostHog product.
- Per-category HTML email templates, digests, or email verification flows.
- Retroactive delivery of past notifications; email starts with notifications
  created after the feature ships.
- Any paid-plan feature of either service; if a limit is hit, events/emails
  drop — that is accepted behavior, not a bug.
