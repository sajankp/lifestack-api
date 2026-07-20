# Spec-088: Job Failure Visibility & Alerting (retry + failure ledger + owner digest)

**Created:** 2026-07-19
**Status:** Approved (2026-07-19) — not yet implemented
**Depends on:** none new. Reuses: the advisory-lock single-connection job design (`app/application/jobs.py`,
api#119) — **must be preserved exactly**; the Resend email path (`app/notifications/email.py::send_email`,
spec-081); `NotificationService` for in-app notifications (spec-052).
**Scope:** `lifestack-api` only — `app/core/retry.py` (new), `app/core/job_failures.py` (new model +
writer), one Alembic migration (`job_failures` table), `app/application/jobs.py` (retry wiring +
two new jobs), `app/application/workflows.py` (digest/heartbeat composition), `app/config.py`,
`.env.example`. No `lifestack-web` / `lifestack-e2e` changes.
**Source:** `PERFORMANCE_ANALYSIS_REPORT.md` §1.3 ("No dead-letter / retry policy — jobs fail
silently") + §12.1 (single-user / 1 GB VM: code-only, no broker). Reshaped 2026-07-19 after owner
feedback: a captured failure nobody looks at is half a feature — the **owner must be actively told**
when something breaks.

---

## Problem

Scheduled jobs fail silently and **the owner has no way to find out**. Two mechanics:

1. **Per-workspace jobs** (`run_workspace_job`, `jobs.py:111`): a failing workspace is rolled back,
   logged (`{job}_workspace_failed`), sent to `capture_exception` (PostHog), then skipped. The loop
   continues; **no durable, queryable record** that the unit didn't complete, and **no notification**.
2. **Global singleton jobs** (`fx_rate_ingestion_job:684`, `bhavcopy_price_feed_job:286`, …): single
   `try/except` that logs and either raises or swallows. Same — no retry, no record, no alert.

For **cleanup jobs** (exports/sessions/import-previews) silence is fine — the next daily run is a
perfectly good retry. The gap that bites is **data-integrity jobs with external dependencies**: FX
rates and bhavcopy prices call third-party APIs. A transient network blip means that day's
rates/prices are missing until tomorrow — a full day of stale valuations — **and the owner is blind
to it.** PostHog error tracking exists but nobody watches a dashboard for a personal app; the
Prometheus counters (`job_failed_total`) are in-memory scrape counters with **no detail** (can't
answer "*which* job, *which* workspace, *what* error, *today*").

The fix, in three cooperating layers, **in-process only (no broker)**:
- **Layer A — Failure ledger:** a durable, detailed record of every job/workspace unit that failed,
  which is what makes an informative alert (and a heartbeat) possible.
- **Layer B — In-run retry:** bounded exponential backoff for *transient* failures on opted-in
  idempotent jobs, so a blip never even becomes a ledger row.
- **Layer C — Owner alerting:** a **daily failure digest** (email + in-app notification, failures
  only) plus a **weekly heartbeat** email so "silence" is provably "healthy," not "the cron/email
  itself is broken."

## Non-goals / why no broker (1 GB single-user)

Celery/RQ + a broker would give cross-process retry but is the wrong spend on a 1 GB box shared by
API + Postgres + Redis + tunnel (report §12.3). **All retry here is intra-invocation.** Cross-run
recovery stays what it is: the next scheduled run. We add (a) surviving transient blips in-run, and
(b) telling the owner when we couldn't.

---

## Design

### Layer A — Failure ledger: `job_failures` table + `app/core/job_failures.py`

(Named `job_failures`, **not** "dead_letters" — its purpose is the alert/heartbeat data source and a
"what's currently broken" view, not a replay queue. Mirrors the `app/core/audit.py` model+writer shape.)

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `public_id` | UUID | external id, project convention |
| `job_name` | str | e.g. `fx_rate_ingestion_job` |
| `workspace_id` | int, **nullable** | set for per-workspace units; NULL for global jobs |
| `error_type` | str | exception class name |
| `error_message` | str | `str(exc)`, **PII-scrubbed** (invariant #8) — never raw payloads |
| `attempts` | int | tries before giving up (1 if retry not enabled for this job) |
| `first_failed_at` | datetime(tz) | |
| `created_at` | datetime(tz) | when the ledger row was written |
| `notified_at` | datetime(tz), nullable | set by the digest once it has reported this row — **guarantees each failure is emailed exactly once** |
| `resolved_at` | datetime(tz), nullable | NULL = still open; set on auto-resolve (see below) or manually |

- **Append-only by convention** (not a delete-blocking trigger like `audit_logs`): these are
  operational, and retention needs real deletes. Only `notified_at` / `resolved_at` are ever updated
  (one-way stamps).
- **Writer** `record_job_failure(session, *, job_name, workspace_id, exc, attempts, first_failed_at)`:
  written in its **own short transaction** so a per-workspace failure's rollback can't erase its own
  ledger row; **best-effort** — a failed ledger insert is logged, never raised into the job (same
  fail-open discipline as spec-087's cache).
- **Auto-resolve (self-heal):** when an opted-in job later *succeeds* for a (job_name, workspace_id),
  mark that unit's still-open rows `resolved_at = now()`. Keeps a "currently broken" view honest and
  lets the heartbeat report "N failed, M auto-recovered." One cheap UPDATE on the success path,
  guarded so it never fails the job.

### Layer B — In-run retry: `app/core/retry.py` (pure, no DB)

```python
async def retry_async(fn, *, attempts, base_delay_seconds, transient_exceptions, on_retry=None):
    """Retry fn only on transient_exceptions with exponential backoff
    (base * 2**(n-1)). Any exception NOT transient re-raises immediately
    (deterministic failure — retrying can't help). Re-raises the last
    transient exception after `attempts` are exhausted."""
```

- **Transient vs deterministic is explicit.** Only network/HTTP-class exceptions (`httpx.HTTPError`,
  `asyncio.TimeoutError`, connection errors) retry. A `ValueError`/constraint/data-shape error
  re-raises on the first attempt → straight to the ledger (no wasted retries holding the advisory lock).
- No jitter (single caller, not a herd). `attempts=3`, `base=2s` caps added lock-hold at ~6s worst
  case — negligible for daily jobs.

**Opt-in set** (external-API + idempotent — the only jobs where in-run retry helps): `fx_rate_ingestion_job`,
`bhavcopy_price_feed_job`, `investment_closing_prices_job`. **Idempotency is a hard precondition**
(architecture invariant #6 / `SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS`): retrying a partially-applied
non-idempotent job double-writes. All three are date-keyed upserts; any future opt-in must argue its
idempotency in-PR. Everything else keeps today's behavior.

**Wiring preserves the api#119 topology exactly.** Retry wraps only the *unit of work*
(`process_workspace(...)` for per-workspace jobs; the workflow call for globals). It opens **no new
sessions**, does **not** change the session-level advisory lock, and each attempt is its own
`async with session.begin()` so a failed attempt rolls back cleanly before the next. `run_workspace_job`
gains optional `retry: RetryPolicy | None = None` and `record_failures: bool = False` params
(defaults = today's behavior). The existing `_workspace_failed` log + `capture_exception` stay;
`record_job_failure(...)` is *added* on exhaustion. The `test_scheduler.py` logical-hold / single-
connection assertions are the regression guard and must pass unchanged.

### Layer C — Owner alerting

**Config recipient — a dedicated ops address, bypassing per-user notification prefs.** Product
notifications (dose reminders etc.) are opt-in per `NotificationPreference` (`channel_email` defaults
False). **Operational alerts must not be silenceable by a product toggle**, so the digest/heartbeat
email is sent *directly* via `send_email(...)` to a new `OWNER_ALERT_EMAIL` setting, gated only by
the existing `EMAIL_ENABLED` + `RESEND_API_KEY` master switches. If `OWNER_ALERT_EMAIL` is unset, the
email step is skipped-and-logged (the in-app notification still happens).

**C1 — Daily failure digest** (`job_failure_digest_job`, new; scheduled at a **fixed 04:00 UTC =
09:30 IST — NOT jitter-staggered**, deliberately: the critical jobs carry ±60min jitter (closing-
prices up to 03:30 UTC), so a jittered digest could fire *before* them and miss the failure. A fixed
04:00 UTC sits safely after their max finish). Rationale: the three data-integrity jobs this spec exists for run at
02:00–02:30 UTC ±60min jitter, finishing by ~03:30 UTC, so 04:00 UTC reliably captures them *and*
lands in the owner's IST morning. Tradeoff (accepted): the low-value cluster tail
(cleanup/insights/net-worth, 03:00–08:00 UTC) reports the *next* morning if it fails — fine, since
those self-retry/self-heal and a persistent failure still nags daily. Catching the tail same-day
would push the digest to ~08:30 UTC (14:00 IST), out of the IST morning window:
- Reads `job_failures` rows where `notified_at IS NULL`.
- **If none → do nothing** (silence = healthy; no "all clear" spam).
- If any → compose **one** email (not one-per-failure) listing job / workspace / error_type /
  error_message / attempts / first_failed_at, send to `OWNER_ALERT_EMAIL`; **and** create **one**
  in-app owner notification via `NotificationService` (a `Notification` row for in-app visibility;
  its push/email fan-out stays preference-gated, which is fine — the in-app row is the point).
- Stamp `notified_at = now()` on the reported rows so a persistently-broken job produces a fresh row
  each day and keeps nagging, but no single failure is emailed twice.
- Runs under its own advisory lock like every other job; idempotent (re-running finds nothing new).

**C2 — Weekly heartbeat** (`job_health_heartbeat_job`, new; weekly, **Monday 04:30 UTC = 10:00 IST**):
- Sends **one** email to `OWNER_ALERT_EMAIL` summarizing the last 7 days: total failures, how many
  auto-recovered (resolved), how many still open, broken out by job. **Sends even when zero
  failures** — that IS its purpose: a periodic "the monitoring itself is alive" proof, so an absence
  of failure emails can be trusted as healthy rather than a broken cron/email pipeline.
- Gated by `EMAIL_ENABLED` + `OWNER_ALERT_EMAIL` + `JOB_HEALTH_HEARTBEAT_ENABLED`.

### Config (new `Settings` fields)

| Field | Default | Notes |
|---|---|---|
| `OWNER_ALERT_EMAIL` | `None` | Ops-alert recipient. Unset ⇒ digest/heartbeat email skipped (in-app notification still fires). |
| `JOB_FAILURE_DIGEST_ENABLED` | `True` | Master switch for the daily digest. |
| `JOB_HEALTH_HEARTBEAT_ENABLED` | `True` | Master switch for the weekly heartbeat. |
| `JOB_RETRY_MAX_ATTEMPTS` | `3` | Opted-in jobs; 1 = no retry. |
| `JOB_RETRY_BASE_DELAY_SECONDS` | `2.0` | Backoff base; attempt n waits `base * 2**(n-1)`. |

No production-validator requirement — every field fails safe (a disabled/unset value reverts to
today's behavior; nothing becomes insecure).

### Retention

`job_failures` rows are written only on failure (tiny volume for one user). Fold a purge of
`resolved_at IS NOT NULL AND created_at < now() - 90d` into an existing hygiene job
(`export_cleanup_job`) — one line, included here. Open rows (`resolved_at IS NULL`) are never
auto-purged.

## Out of scope

- Cross-process / cross-run durable retry (the broker — rejected, §12.3).
- A web UI / API endpoint to browse or replay failures (owner sees them via the digest email + in-app
  notification; a `/admin` surface is a separate spec if ever wanted for a single-user app).
- Per-failure (non-digest) email spam; retry on cleanup or non-idempotent jobs.
- Circuit-breaking / backpressure on the external APIs (over-engineering for daily jobs).

## Risks

| Risk | Mitigation |
|---|---|
| Retry disturbs the advisory-lock single-connection rule (api#119 landmine) | Retry wraps only the unit of work; no new sessions, no lock change; each attempt its own `session.begin()`. Guarded by unchanged `test_scheduler.py` hold-count. |
| Retrying a non-idempotent job double-writes | Retry ONLY on proven date-keyed-upsert jobs; tied to invariant #6; future opt-ins argue idempotency in-PR. |
| Alert spam (nagging every day for a known-broken job) | Digest is one email/day max, and only for *new* rows (`notified_at IS NULL`); a persistent failure nags daily by design (that's correct for "still broken"), never multiple times/day. |
| Owner never gets alerts because email is misconfigured, and can't tell | Weekly heartbeat sends even at zero failures — its absence is itself the signal the pipeline is down. |
| Digest/heartbeat email failure cascades | Both are best-effort, log-not-raise; a Resend outage never fails the scheduler run or masks the underlying job failures (which remain in the ledger). |
| PII in `error_message` or the email body | `str(exc)` scrubbed before write; invariant #8 applies to the table and the email. |

## Testing plan (Red/Green, coverage gate 80%)

`retry.py` (pure, fast):
1. Transient then success → returns value, correct call count. 2. Transient always → raises after
exactly `attempts`. 3. Deterministic exception → raises on first call, no retry. 4. Backoff delays
(`base, 2·base, …`) with `asyncio.sleep` patched.

Ledger + wiring (testcontainer Postgres):
5. Global job exhausts retries → one `job_failures` row, `workspace_id IS NULL`, scrubbed message,
correct `attempts`. 6. Per-workspace unit exhausts → workspace-scoped row; **other workspaces still
processed** (isolation preserved). 7. Auto-resolve: a later success stamps prior open rows
`resolved_at`. 8. **Regression:** `test_scheduler.py` single-connection assertions pass unchanged
with retry enabled.

Alerting:
9. Digest with failures → exactly one `send_email` call (to `OWNER_ALERT_EMAIL`) + one owner
`Notification`; reported rows get `notified_at`. 10. Digest with no unnotified rows → **no** email,
**no** notification. 11. `OWNER_ALERT_EMAIL` unset → no email, but in-app notification still created.
12. Weekly heartbeat sends even at zero failures; respects `JOB_HEALTH_HEARTBEAT_ENABLED=False`.
13. `JOB_FAILURE_DIGEST_ENABLED=False` → digest job is a no-op. 14. Retention purge removes only
resolved rows past the window; open rows untouched.

## Rollout

- Merge with retry active on the three opted-in jobs; digest + heartbeat **enabled by default but
  inert until `OWNER_ALERT_EMAIL` + `EMAIL_ENABLED` are set** (so a fresh env sends nothing by
  accident). Owner sets `OWNER_ALERT_EMAIL` to turn alerting on — no code change.
- Two new scheduled jobs (`job_failure_digest_job` daily, `job_health_heartbeat_job` weekly)
  registered in `main.py` lifespan, staggered.
- Per CLAUDE.md "runbook in the same pass": document the five new settings, the `job_failures` table
  (how to read open failures, what auto-resolve means), and the digest/heartbeat cadence in the
  `lifestack-config-and-flags` + `lifestack-run-and-operate` domain memory in the same PR.

## Resolved decisions

- **Digest / heartbeat timing** (owner-decided 2026-07-19): daily digest **04:00 UTC (09:30 IST)**,
  weekly heartbeat **Monday 04:30 UTC (10:00 IST)** — earliest IST-morning slot that still lands
  after the critical data-integrity jobs. See C1 rationale above.
