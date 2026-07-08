# Scheduled Background Jobs

This document details all background jobs registered in Lifestack's FastAPI lifespan and managed by the embedded `AsyncIOScheduler` (APScheduler).

Most background jobs are subject to **advisory lock coordination** on Postgres to prevent split-brain conflicts during rolling deployment windows, using one of two primitives:

- **Transaction-scoped** (`pg_try_advisory_xact_lock`, released automatically on commit): `fx_rate_ingestion`, `export_cleanup`, `session_cleanup`, `import_preview_cleanup`, `push_delivery`.
- **Session-scoped** (`pg_try_advisory_lock`, held across the per-workspace loop and released explicitly): the remaining per-workspace jobs, via the shared `run_workspace_job` helper — this primitive is used instead of the transaction-scoped one specifically so the lock survives `COMMIT` between workspaces in the loop (see the helper's docstring). `investment_closing_prices_job` was the last per-workspace job managing its own per-workspace sessions with no lock at all; it now uses `run_workspace_job` too (key 1013, see `app/core/constants.py`).

Additionally, non-idempotent scheduler jobs are blocked from registering unless `SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS=true` is explicitly configured.

---

## 1. Budget Guardrails Job
- **Job ID**: `budget_guardrails`
- **Interval**: Every 6 hours (configurable via `BUDGET_GUARDRAILS_INTERVAL_HOURS`, defaults to `6`)
- **Job Function**: `budget_guardrails_job`
- **Workflow Function**: `evaluate_workspace_budget_guardrails`
- **Purpose**: Checks active workspace budgets against transaction spends for the current month. If spending exceeds the configured warning threshold (default: 90%) or critical threshold (default: 100%), it creates or updates a system-owned Todo. If the spend falls back below the threshold, it automatically completes the Todo.
- **Idempotency**: Handled at the database level via the `uq_todo_workspace_system_key` unique constraint on the `Todo.system_key` field (e.g. `budget:guardrail:{category_id}`).
- **Audit Logging**: Emits `budget_guardrail_triggered` audit events with before/after todo state snapshots.

## 2. Recurring Transactions Job
- **Job ID**: `recurring_transactions`
- **Schedule**: Daily at the hour defined by `RECURRING_TXN_GENERATION_HOUR` (UTC)
- **Job Function**: `recurring_transactions_job`
- **Workflow Function**: `process_workspace_recurring_transactions`
- **Recurrence advance (spec-053)**: both this job and recurring-todo generation advance `next_due_date` via the shared `app/core/recurrence.advance_due_date` (moved out of `app/spending/service.py`, which previously held a private copy `app/application/workflows.py` reached across module boundaries to apply to todo rules too). Supports three monthly modes — `day_of_month` (default, now clamped against the rule's *anchor* day rather than the current date's day, fixing a permanent-drift bug for anchors on days 29-31), `last_day`, and `nth_weekday` (e.g. "first Friday", "last Sunday") — selected per-rule via `monthly_mode`/`by_weekday`/`by_ordinal` on both `recurring_todo_rules` and `recurring_transactions`.
- **Purpose**: Scans active recurring transaction rules due on or before today. It automatically generates and commits corresponding spending transaction records in the database, updating the `next_due_date` and setting `last_generated_at`. In the same per-workspace transaction it also calls `process_workspace_recurring_todos`, generating recurring Todo records due on or before today — there is no separate "recurring todo" job.

## 3. FX Rate Ingestion Job
- **Job ID**: `fx_rate_ingestion`
- **Schedule**: Daily at 02:00 UTC
- **Job Function**: `fx_rate_ingestion_job`
- **Workflow Function**: Ingests updated foreign exchange rates for active currency pairs from external feeds or mock providers to keep look-through and portfolio valuation conversions current.

## 4. NSE Bhavcopy Price Feed Job
- **Job ID**: `bhavcopy_price_feed`
- **Schedule**: Daily at 02:00 UTC
- **Job Function**: `bhavcopy_price_feed_job`
- **Purpose**: Downloads NSE's official end-of-day security-wise bhavcopy CSV for the most recent completed trading day and pre-fills `holding_prices` (`source="bhavcopy"`) for INR-denominated stock/ETF holdings whose symbol matches an `EQ`-series row. Runs 30 minutes before the Investment Closing Prices job, which already skips any holding already priced for the expected close date — so a bhavcopy hit means that holding never falls through to the Yahoo-backed fallback. If the feed is unavailable (holiday, outage, format change) the job logs and exits with no writes; nothing downstream regresses, it just misses the optimization for that day. Mutual funds are untouched (already priced via AMFI NAV) and non-INR holdings are untouched (not covered by NSE).
- **Idempotency**: `HoldingPriceRepository.bulk_upsert_prices` — same `(holding_id, price_date)` upsert semantics every other price-writing path uses.

## 5. Investment Closing Prices Job
- **Job ID**: `investment_closing_prices`
- **Schedule**: Daily at 02:30 UTC
- **Job Function**: `investment_closing_prices_job`
- **Workflow Function**: Fetches the latest closing market prices for active holdings and records them in `holding_prices`, keeping portfolio valuation, daily-change, and look-through analytics on current market values.

## 6. Export Cleanup Job
- **Job ID**: `export_cleanup`
- **Schedule**: Daily at 03:00 UTC
- **Job Function**: `export_cleanup_job`
- **Workflow Function**: Cleans up expired workspace data exports and download records from database and storage backends.

## 7. Session Cleanup Job
- **Job ID**: `session_cleanup`
- **Schedule**: Daily at 04:00 UTC
- **Job Function**: `session_cleanup_job`
- **Workflow Function**: Purges expired and revoked authentication session records (`auth_sessions`) from the database to prevent database bloat.

## 8. Import Preview Cleanup Job
- **Job ID**: `import_preview_cleanup`
- **Schedule**: Daily at 05:00 UTC
- **Job Function**: `import_preview_cleanup_job`
- **Workflow Function**: Deletes cached CSV import preview rows and files (`import_preview_rows`) older than 24 hours to reduce storage usage and keep personal data secure.

## 9. Weekly Summary Job
- **Job ID**: `weekly_summary`
- **Schedule**: Weekly on Mondays at 01:30 UTC
- **Job Function**: `weekly_summary_job`
- **Workflow Function**: Generates a weekly productivity and financial summary report for active workspace memberships, combining completed todo counts and categories spend metrics.

## 10. Dashboard Insights Job
- **Job ID**: `dashboard_insights`
- **Schedule**: Daily at 06:00 UTC
- **Job Function**: `dashboard_insights_job`
- **Workflow Function**: `generate_workspace_insights` (`app/application/insights.py`)
- **Purpose**: Runs three detectors per workspace and writes `Notification` rows (`category="insight"`): spending anomaly vs a trailing 4-week average, budget pace forecast for the current month, and new recurring-charge detection (a same-category, similar-amount charge recurring across 2+ months with no matching active `RecurringTransaction` rule). Surfaced today via the existing `GET /v1/notifications?category=insight` endpoint; delivered over push automatically once web push (spec-052) ships, via the existing per-category `NotificationPreference.channel_push` toggle — no code in this job references push.
- **Idempotency**: No unique DB constraint (unlike `Todo.system_key`) — each detector does a targeted existence check against `Notification` (`entity_type` + `entity_public_id`, scoped to the relevant period) before writing, so re-running the job the same week/month does not duplicate a notification.

## 11. Push Delivery Job
- **Job ID**: `push_delivery`
- **Interval**: Every `PUSH_DELIVERY_INTERVAL_MINUTES` minutes (default: 1)
- **Job Function**: `push_delivery_job`
- **Workflow Function**: `deliver_pending_push_notifications` (`app/application/workflows.py`)
- **Purpose**: Drains pending `NotificationDelivery` rows with `channel="push"`, sending each notification's title/body to every active `PushSubscription` of the target user via `pywebpush`. Global, not per-workspace — a delivery queue has no natural workspace-iteration shape. One delivery row fans out to all of a user's active subscriptions (phone + tablet + desktop); the row's status folds all per-subscription outcomes together (`sent` if any endpoint accepted, `failed` with detail if all failed). A 404/410 from a push service means that subscription no longer exists — it is deactivated (`is_active=False`) and the run continues. No-ops cleanly (returns immediately) when `VAPID_PUBLIC_KEY`/`VAPID_PRIVATE_KEY` are unset.
- **Idempotency**: Only `pending` delivery rows are ever picked up; a re-run after a successful drain finds nothing to do.

## 12. Todo Reminder Job
- **Job ID**: `todo_reminder`
- **Interval**: Every `TODO_REMINDER_INTERVAL_MINUTES` minutes (default: 5)
- **Job Function**: `todo_reminder_job`
- **Workflow Function**: `process_workspace_todo_reminders` (`app/application/workflows.py`)
- **Purpose**: The first real notification source for push (spec-052) — finds incomplete todos with `due_date` inside the look-ahead window (now → now + interval) that haven't been reminded yet, and creates a `Notification` (`category="todo_reminder"`) for each via the existing `NotificationService.notify`. Push delivery then happens for free through that method's existing enqueue step.
- **Idempotency**: `Todo.reminded_at` — set when the reminder notification is created; a re-run only picks up todos where it's still `NULL`. Reset to `NULL` whenever a todo's `due_date` changes, so moving a reminder later re-arms it.

## 13. Net Worth Snapshot Job
- **Job ID**: `net_worth_snapshot`
- **Schedule**: Daily at 07:00 UTC (after `investment_closing_prices` and `dashboard_insights`)
- **Job Function**: `net_worth_snapshot_job`
- **Service Method**: `NetWorthService.create_net_worth_snapshot` (`app/finance/service.py`)
- **Purpose**: Materializes one `net_worth_snapshots` row per workspace per day so net worth has a real history to graph (spec-065). Computes holdings value, investing cash, and spending cash live via `InvestingSummaryService.get_summary` — the same path `GET /finance/net-worth` uses — rather than reading the cached, investing-only `portfolio_snapshots` table, so it doesn't depend on a dashboard visit having happened that day and doesn't skip spending-only workspaces. Skips a workspace silently if it has no reporting currency configured, or if any balance can't be FX-converted to it.
- **Idempotency**: Upserted on the unique `(workspace_id, snapshot_date)` constraint. `GET /finance/net-worth` also opportunistically upserts today's row on every read, so the cron run for "today" is frequently a no-op update of a value that already exists.
