# Spec-098: Manual Weekly Close Generation and Monthly Close Corrections

**Created:** 2026-10-04
**Status:** Under Review (pending approval)
**Scope:** API (`app/summaries`), Web (`lifestack-web/src/pages/WeeklySummariesPage`, `services/summaries.ts`)
**Depends on:** spec-016 weekly summary, spec-076 weekly summaries enhancements, spec-096 periodic summaries and monthly close

---

## 1. Problem Statement & Operational Findings

### A. Root Cause Analysis: Why Weekly Close Was Not Running Properly in Production
1. **Production Log Suppression (`LOG_LEVEL=ERROR`):**
   In `.env.production` (line 17), `LOG_LEVEL` is set to `ERROR`. As a result, all routine structlog logs (`logger.info`) in `weekly_summary_job`—including `weekly_summary_job_start`, `weekly_summary_workspace_success`, `weekly_summary_job_skipped_lock_held`, and `weekly_summary_job_completed`—are filtered out. Only critical exceptions are captured, making the job appear completely invisible in the logs during normal or skipped execution.
2. **Hourly Cadence Tick & No Missed-Run Catch-up:**
   The scheduler adds `weekly_summary_job` with `cron, minute=30` and `respect_cadence=True`. Every hour at `:30`, it calls `WorkspaceSummarySettingRepository.list_due(day_of_week, hour_utc)` matching against the workspace's configured cadence (default: Monday 01:00 UTC = 06:30 AM IST). If the host or container was offline, asleep, or restarting at 01:30 UTC on Monday, APScheduler's standard cron trigger misses that single 1-hour window for the entire week without backfilling.
3. **Date Math Issue on Non-Monday Cadences:**
   In `app/application/jobs.py` (lines 710–715):
   ```python
   today = start_time.date()
   days_since_monday = today.weekday()
   last_monday = today - timedelta(days=days_since_monday + 7)
   ```
   If a user configures their weekly cadence to Sunday (day 6, e.g. closing the week on Sunday evening): `today.weekday()` is 6, so `days_since_monday + 7 = 13` days. The job incorrectly reaches back 13 days to the Monday two weeks ago instead of the Monday 6 days ago (the week that just finished).
4. **No On-Demand Generation for Weekly Summaries:**
   While monthly summaries (spec-096) introduced `POST /v1/summaries/monthly/generate` and a "Generate Month Close" button in the UI, weekly summaries only provide `POST /v1/summaries/weekly/{public_id}/regenerate` (which fails with 404 if no summary was ever created for that week). There is currently no way in the API or UI to manually generate or backfill a weekly close.

### B. Inability to Correct / Edit Fields in Monthly Close
Monthly financial closes calculate automated rollups of income, expenses, net worth snapshots, and investing values. When automated feeds have minor gaps (e.g. unimported cash transfers, delayed dividends, offline asset revaluation, or untracked bank interest), users cannot adjust the snapshot fields to reflect their actual accounting close without manually hacking database rows.

---

## 2. Solution Specification

### A. Manual Weekly Close Generation (UI & API)

#### 1. Backend Endpoint
- **Endpoint:** `POST /v1/summaries/weekly/generate`
- **Request Body:**
  ```json
  {
    "date": "2026-09-28" // or "week_start": "2026-09-28"
  }
  ```
- **Validation & Logic:**
  - `week_start` is normalized: if user picks any day in a week, automatically snaps to Monday:
    `week_start = input_date - timedelta(days=input_date.weekday())`
  - Reuses `WeeklySummaryService.generate_for_workspace_week(workspace_id, user_id, week_start)`.
  - Upserts if an un-superseded summary for that week already exists, or creates a new one.
  - Returns `WeeklySummaryResponse` (HTTP 200).

#### 2. Frontend UI
- In `lifestack-web/src/pages/WeeklySummariesPage.tsx`:
  - When `cadence === 'weekly'`, display a **"Generate Week Close"** button in the header (styled consistently with "Generate Month Close").
  - Clicking opens a **Generate Week Close Modal** with:
    - Date picker input (defaults to previous week's Monday or current date).
    - Helper label showing the computed week range: e.g. `Monday, Sep 28, 2026 – Sunday, Oct 4, 2026`.
    - "Generate Close" action button that calls `summariesService.generateWeekly(date)`.
  - Also provide "Generate Week Close" in the empty state when no weekly summaries exist.

---

### B. Editing Fields in Monthly Close (UI & API)

#### 1. Backend Endpoint
- **Endpoint:** `PATCH /v1/summaries/monthly/{summary_id}`
- **Request Body:** `UpdateMonthlySummaryRequest`
  ```json
  {
    "spending_summary": {
      "total_income": "85000.00",
      "total_expense": "42150.00",
      "net": "42850.00"
    },
    "investing_summary": {
      "portfolio_value_end": "1250000.00",
      "cash_end": "50000.00",
      "week_change": "15000.00",
      "week_change_pct": "1.2"
    },
    "net_worth_summary": {
      "total_net_worth_end": "1850000.00",
      "movement": "35000.00",
      "movement_pct": "1.9"
    },
    "dividend_summary": {
      "total_net": "3200.00",
      "count": 4
    },
    "todo_summary": {
      "tasks_created": 15,
      "tasks_completed": 14
    },
    "reason": "Corrected offline cash salary and savings interest"
  }
  ```
- **Service & Repository Logic:**
  - Deep-merges or updates the provided JSON sections into the existing `MonthlySummary` row.
  - Stamps `regenerated_at = datetime.now(UTC)` and `regeneration_reason = reason`.
  - Flushes and returns the updated `MonthlySummaryResponse`.

#### 2. Frontend UI
- On each Monthly Summary card in `WeeklySummariesPage.tsx`:
  - Add an **"Edit Close"** button with a pencil icon next to "Regenerate".
  - Clicking opens an **Edit Monthly Close Modal**:
    - **Spending Section:** Total Income, Total Expense, Net Amount.
    - **Investing Section:** Portfolio Value End, Investment Cash End, Movement Amount.
    - **Net Worth Section:** Total Net Worth End, Movement Amount.
    - **Dividend Section:** Total Received, Payments Count.
    - **Correction Note:** Optional reason text field ("Saved to history header").
  - On save, executes `summariesService.updateMonthly(summaryId, payload)`, invalidates queries, and displays success toast.

---

### C. Weekly Summary Job Date Math Fix (Scheduler)
In `app/application/jobs.py`:
Fix the weekly calculation when `respect_cadence=True` so that if `start_time.weekday()` is Sunday (day 6), it correctly closes the week ending today:
```python
if week_start is None:
    today = start_time.date()
    days_since_monday = today.weekday()
    # If running on Monday (0), target last week's Monday (-7).
    # If running on Sunday (6) as a close-of-week cadence, target the Monday that began this week (-6).
    offset = 7 if days_since_monday == 0 else days_since_monday
    last_monday = today - timedelta(days=offset)
```

---

## 3. Testing Plan

1. **Unit & API Integration Tests (`lifestack-api`):**
   - Test `POST /v1/summaries/weekly/generate` creates/upserts weekly summary for a given date and snaps to Monday.
   - Test `PATCH /v1/summaries/monthly/{summary_id}` updates JSON summary fields and records regeneration timestamp & reason.
   - Test date offset calculation for Sunday cadences vs Monday cadences.
2. **Frontend Component Tests (`lifestack-web`):**
   - Test "Generate Week Close" button triggers dialog and submits mutation with selected date.
   - Test "Edit Close" button opens modal prefilled with current summary values and submits updates.
