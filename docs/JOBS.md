# Scheduled Background Jobs

This document details all background jobs registered in Lifestack's FastAPI lifespan and managed by the embedded `AsyncIOScheduler` (APScheduler).

All background jobs are subject to **advisory lock coordination** (`pg_try_advisory_xact_lock` on Postgres) to prevent split-brain conflicts during rolling deployment windows. Additionally, non-idempotent scheduler jobs are blocked from registering unless `SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS=true` is explicitly configured.

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
- **Purpose**: Scans active recurring transaction rules due on or before today. It automatically generates and commits corresponding spending transaction records in the database, updating the `next_due_date` and setting `last_generated_at`.

## 3. FX Rate Ingestion Job
- **Job ID**: `fx_rate_ingestion`
- **Schedule**: Daily at 02:00 UTC
- **Job Function**: `fx_rate_ingestion_job`
- **Workflow Function**: Ingests updated foreign exchange rates for active currency pairs from external feeds or mock providers to keep look-through and portfolio valuation conversions current.

## 4. Export Cleanup Job
- **Job ID**: `export_cleanup`
- **Schedule**: Daily at 03:00 UTC
- **Job Function**: `export_cleanup_job`
- **Workflow Function**: Cleans up expired workspace data exports and download records from database and storage backends.

## 5. Session Cleanup Job
- **Job ID**: `session_cleanup`
- **Schedule**: Daily at 04:00 UTC
- **Job Function**: `session_cleanup_job`
- **Workflow Function**: Purges expired and revoked authentication session records (`auth_sessions`) from the database to prevent database bloat.

## 6. Import Preview Cleanup Job
- **Job ID**: `import_preview_cleanup`
- **Schedule**: Daily at 05:00 UTC
- **Job Function**: `import_preview_cleanup_job`
- **Workflow Function**: Deletes cached CSV import preview rows and files (`import_preview_rows`) older than 24 hours to reduce storage usage and keep personal data secure.

## 7. Weekly Summary Job
- **Job ID**: `weekly_summary`
- **Schedule**: Weekly on Mondays at 01:30 UTC
- **Job Function**: `weekly_summary_job`
- **Workflow Function**: Generates a weekly productivity and financial summary report for active workspace memberships, combining completed todo counts and categories spend metrics.
