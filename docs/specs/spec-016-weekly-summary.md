# Feature Spec: Weekly Summary Workflow
**Status:** Implemented
**Spec ID:** 016

Implementation note (2026-06-22): weekly investing metrics use compatible portfolio
snapshots in their stored reporting currency, keep cash separate, and return unavailable
states when a valid comparison cannot be made. The web client renders typed metrics rather
than raw JSON.

## 1. Overview
The README identifies cross-module workflows as a key differentiator: "weekly summaries can combine productivity and finance data." This spec defines the second major scheduler workflow — a periodic summary that aggregates activity across todo, spending, and investing into a single digest available via API and optionally delivered as a notification.

This builds on:
- Spec 005 (scheduler): APScheduler infrastructure
- Spec 007 (dashboard): aggregation patterns
- Spec 009 (budget guardrails): first workflow pattern
- Spec 015 (notifications): delivery channel

## 2. Goals
- Generate a weekly cross-module summary combining task completion, spending patterns, and portfolio changes.
- Persist summaries for historical review ("how was my week?").
- Surface the latest summary on the dashboard.
- Deliver summary as a notification (in-app, optionally email).
- Demonstrate the cross-module orchestration pattern for future workflows.

## 3. Non-Goals (for this slice)
- AI-generated narrative or natural language insights.
- Configurable summary frequency (weekly only in V1; monthly/daily are future).
- Comparative analytics against previous weeks (trend lines).
- User-customizable summary sections or thresholds.
- PDF or image export of summaries.

## 4. Data Model

### WeeklySummary
- `id`: internal PK
- `public_id`: external UUID
- `workspace_id`: tenant FK
- `week_start`: date (Monday of the summarized week)
- `week_end`: date (Sunday)
- `generated_at`: timestamp
- `todo_summary`: JSONB — task metrics for the week
- `spending_summary`: JSONB — spending metrics for the week
- `investing_summary`: JSONB — portfolio metrics for the week
- `highlights`: JSONB — notable events/flags
- `created_at`

Constraints:
- unique `(workspace_id, week_start)`

### 4.1 todo_summary Shape
```json
{
  "tasks_created": 5,
  "tasks_completed": 8,
  "tasks_overdue": 2,
  "completion_rate_pct": 80.0,
  "open_count_start": 12,
  "open_count_end": 9
}
```

### 4.2 spending_summary Shape
```json
{
  "total_income": "5000.00",
  "total_expense": "3200.00",
  "net": "1800.00",
  "top_categories": [
    { "name": "Food & Dining", "amount": "800.00", "pct_of_total": 25.0 }
  ],
  "budget_utilization_pct": 72.5,
  "budgets_breached": 1,
  "recurring_generated_count": 4
}
```

### 4.3 investing_summary Shape
```json
{
  "status": "complete",
  "portfolio_value_start": "120000.00",
  "portfolio_value_end": "122500.00",
  "cash_start": "5000.00",
  "cash_end": "5200.00",
  "week_change": "2500.00",
  "week_change_pct": "2.08",
  "currency": "USD",
  "start_snapshot_date": "2026-05-19",
  "end_snapshot_date": "2026-05-25"
}
```

Portfolio value means holdings market value only. Cash is reported separately and never
contributes to portfolio gain/loss. Values come from persisted portfolio snapshots at the
week boundaries, including their reporting-currency FX conversions. If compatible start
and end snapshots do not exist, the section returns `status: unavailable` with nullable
metrics rather than substituting cost basis, current holdings, or zero.

### 4.4 highlights Shape
```json
{
  "flags": [
    { "type": "budget_breach", "message": "Entertainment exceeded budget by 15%" },
    { "type": "high_completion", "message": "Completed 8 tasks — best week this month" },
    { "type": "portfolio_milestone", "message": "Portfolio crossed $120K" }
  ]
}
```

## 5. API Surface

### Summaries
- `GET /v1/summaries/weekly` — list past weekly summaries (paginated)
- `GET /v1/summaries/weekly/latest` — most recent summary
- `GET /v1/summaries/weekly/{public_id}` — specific summary by ID

Query parameters for list:
- `from` / `to` date range filter
- Cursor pagination

### Dashboard Integration
The dashboard summary endpoint (Spec 007) gains an optional `latest_weekly_summary` field:
```json
{
  "todo": { ... },
  "spending": { ... },
  "investing": { ... },
  "latest_weekly_summary": {
    "public_id": "uuid",
    "week_start": "2026-05-19",
    "highlights": { "flags": [...] },
    "generated_at": "2026-05-26T01:30:00Z"
  }
}
```

## 6. Scheduler Job

### Job: `weekly_summary_job`
- **Trigger:** every Monday at 01:30 UTC (configurable)
- **Scope:** iterate active workspaces

Per workspace:
1. Determine the previous week window (Monday 00:00 to Sunday 23:59 UTC).
2. **Todo aggregation:**
   - Count tasks created, completed, and overdue within the window.
   - Calculate completion rate.
   - Snapshot open count at start and end of period.
3. **Spending aggregation:**
   - Sum income and expense transactions in the window.
   - Rank top-N categories by expense amount.
   - Calculate budget utilization (actual vs. budgeted across all categories).
   - Count recurring transactions generated.
4. **Investing aggregation:**
   - Compare the latest snapshot before the week with the latest snapshot on or before
     week end (from Spec 014).
   - Require matching snapshot currencies.
   - Keep cash separate from holdings value and weekly return.
   - If no snapshots available, mark section as `unavailable`.
5. **Highlight generation:**
   - Apply rule-based flags (budget breach, high completion, portfolio milestones).
6. Persist `WeeklySummary` row.
7. Dispatch notification via Spec 015: category `system`, severity `info`.
8. Emit audit event.

### Idempotency
- If a summary for `(workspace_id, week_start)` already exists, the job skips that workspace (no overwrite).
- To regenerate, an admin endpoint or manual DB correction is needed (future concern).

### Partial Data Handling
- If a module has no data for the week, its summary section is populated with zeros/nulls, not omitted.
- If investing snapshots are unavailable (Spec 014 not yet active), `investing_summary` contains `{ "status": "unavailable" }`.

## 7. Highlight Rules (V1)
Configurable thresholds for flag generation:

| Rule | Condition | Flag Type |
|------|-----------|-----------|
| Budget breach | Any category actual > budget | `budget_breach` |
| High completion | Completion rate ≥ 90% | `high_completion` |
| Low completion | Completion rate < 50% with ≥ 5 tasks | `low_completion` |
| Portfolio milestone | Value crossed a round number (10K, 50K, 100K, etc.) | `portfolio_milestone` |
| Big spend week | Total expense > 150% of 4-week average | `high_spending` |

## 8. Configuration
- `WEEKLY_SUMMARY_ENABLED`: feature flag, default `true`.
- `WEEKLY_SUMMARY_DAY`: day of week to run (0=Mon), default `0`.
- `WEEKLY_SUMMARY_HOUR`: hour UTC, default `1`.
- `WEEKLY_SUMMARY_TOP_CATEGORIES`: number of top categories in spending breakdown, default `5`.

## 9. Audit Events
- `weekly_summary_generated` — system creates a summary (module: `application`)

## 10. Test Plan
- **Unit tests:**
  - Aggregation logic for each module section
  - Highlight rule evaluation with boundary cases
  - Idempotency (skip if summary exists)
  - Partial data handling (missing investing snapshots)
- **Integration tests:**
  - Full pipeline: seed data → run job → verify summary content
  - Notification dispatch on summary creation
  - Dashboard endpoint includes latest summary
  - Workspace isolation

## 11. Acceptance Criteria
- Weekly summary job runs on schedule and produces persisted summaries.
- Summary aggregates data from todo, spending, and investing modules.
- Highlights surface notable events based on configurable rules.
- API endpoints expose summary history and latest summary.
- Dashboard includes latest summary highlights.
- Notification dispatched on summary generation.
- Partial module unavailability does not block summary creation.
- Idempotent: re-running for same week does not create duplicates.

## 12. Migration
- Alembic migration adds `weekly_summaries` table.
- No data backfill (summaries begin from first job run).
