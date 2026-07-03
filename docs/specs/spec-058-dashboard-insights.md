# Spec-058: Dashboard Insights (Phase 1)

**Created:** 2026-07-04
**Status:** Approved (owner directive 2026-07-04, Rule 7 — Tasks 5–13 pre-approved; spec still written in house style, approval pause waived)
**Depends on:** none for Phase 1 (push delivery is additive-for-free if Task 10/spec-052 has merged by the time this ships — see "Interplay with push" below)

---

## Problem

Lifestack already tracks enough spending/budget history to surface proactive warnings ("this category is spending more than usual", "you're on pace to blow this month's budget", "this looks like a new subscription") instead of making the user notice them by scrolling analytics pages. There is no automated analysis today — `budget_guardrails_job` (spec-009) only checks *current-month spend vs configured budget threshold*, not trend/pace/pattern detection, and there is no job that writes proactive, non-actionable-todo notifications a user can just read and dismiss.

## Solution

A new daily job, `dashboard_insights_job`, runs three independent detectors per workspace and writes `Notification` rows (`category="insight"`, existing `Notification`/`NotificationPreference` infrastructure — no new tables). Phase 1 scope is detection + notification only; surfacing is the existing `GET /v1/notifications?category=insight` endpoint (already supports category filtering, zero backend changes needed there) rendered as dashboard cards.

### Detector 1 — Spending anomaly vs 4-week average

Per `SpendingCategory` with at least one `expense` transaction in the trailing 7 days:

```
current_week_total   = Σ(expense.amount) for occurred_at in [today-7d, today)
trailing_4wk_avg     = average of Σ(expense.amount) over the 4 prior non-overlapping
                        7-day windows, i.e. [today-35d, today-7d)
```

Trigger when **all** of:
- `trailing_4wk_avg > 0` (a category with zero prior history has nothing to be anomalous *relative to* — its first-ever purchase is just new spending, not a spike; skip to avoid false positives on brand-new categories)
- `current_week_total >= trailing_4wk_avg * Decimal("1.5")` (50%+ above baseline)
- `current_week_total >= trailing_4wk_avg + Decimal("500")` (absolute floor in the workspace's reporting currency — without this, a category with a $1 four-week average would flag on a single $5 purchase, which is a 5x ratio but financially meaningless)

Notification: `severity="warning"`, `title=f"{category.name} spending is up this week"`, `body` states both amounts. `entity_type="spending_category_anomaly"`, `entity_public_id=category.public_id`.

**Dedup key:** `(workspace_id, "spending_anomaly", category_id, week_start.isoformat())` — one per category per calendar week (Monday-anchored, matching `weekly_summary_job`'s existing week boundary convention).

### Detector 2 — Budget pace forecast

Per active `SpendingBudget` row for the current `month_start`:

```
days_elapsed = today.day
days_in_month = calendar.monthrange(today.year, today.month)[1]
spent_so_far  = Σ(expense.amount) for that category in the current month to date
projected     = spent_so_far / days_elapsed * days_in_month
```

Trigger when **all** of:
- `days_elapsed >= 5` (need at least 5 days of in-month data before extrapolating — projecting from day 1-2 swings wildly on a single large purchase)
- `projected > budget.amount * Decimal("1.1")` (10% buffer over the budget — avoids noise from projections that land right at the edge)

Notification: `severity="warning"`, `title=f"On pace to exceed {category.name} budget"`, `body` states `spent_so_far`, `projected`, `budget.amount`. `entity_type="spending_budget_pace"`, `entity_public_id=budget.public_id`.

**Dedup key:** `(workspace_id, "budget_pace", category_id, month_start.isoformat())` — created **once** per category per month (not re-triggered daily even if pace worsens further — a single heads-up per month is the point; re-alerting daily would be noise, not insight).

### Detector 3 — New recurring-charge detection

For `expense` transactions in the trailing ~65 days (covers two monthly cycles with slack), grouped by `category_id`:
- Within a category, bucket transactions whose amounts are within 5% of each other (`abs(a - b) <= max(a, b) * Decimal("0.05")`).
- A bucket is a **candidate** if it has transactions occurring in **2 or more distinct calendar months** (a same-amount purchase twice in one month is a coincidence, not a recurring pattern) **and** there is no existing *active* `RecurringTransaction` row for that `category_id` with an `amount` within the same 5% tolerance (the user may have already set this up — don't re-suggest what's already tracked).

Notification: `severity="info"`, `title=f"Recurring charge detected: {category.name}"`, `body` names the repeated amount and suggests adding a recurring rule. `entity_type="spending_category_recurring"`, `entity_public_id=category.public_id`.

**Cross-detector dedup note:** each detector uses a distinct `entity_type` tag (`spending_category_anomaly` / `spending_budget_pace` / `spending_category_recurring`) even where two detectors could otherwise share an `entity_public_id` (both Detector 1 and Detector 3 key off the same `category.public_id`) — sharing a tag would make one detector's existence check spuriously suppress the other's notification.

**Dedup key:** `(workspace_id, "recurring_candidate", category_id, bucket_amount_rounded)` — created **once ever** per (category, amount-bucket), not per run and not per month; re-suggesting the same detected pattern every day would be pure noise. If the user later adds the matching `RecurringTransaction`, the detector naturally stops re-triggering (the "no existing active rule" guard), but the original notification is not retroactively deleted — same "notifications are a log, not a live view" behavior every other notification category already has.

### Dedup mechanism

`Notification` has no unique constraint (unlike `Todo.system_key`). Rather than adding one (would require a migration + touching the shared `notifications` table's semantics for every other category), the job does a targeted existence check before writing: `entity_type` + `entity_public_id` + `body` containing the period marker is enough entropy in practice, but to be exact and cheap, `app/application/insights.py` queries `Notification` for `(workspace_id, category="insight", entity_type, entity_public_id)` rows created since the relevant period start (7d/month-start/none respectively) before creating — one indexed query per candidate, not a new schema construct. This mirrors the "check before write" idempotency style `budget_guardrails_job` uses via `Todo.system_key`, adapted to a table that intentionally has no such column.

### Interplay with push (Task 10 / spec-052)

Insights are just `Notification` rows with `category="insight"`; if push delivery (spec-052) has already shipped by the time this merges, insights are delivered over push automatically through the existing `NotificationPreference.channel_push` per-category opt-in — no code in this spec references push at all, so ordering between Task 8 and Task 10 doesn't matter functionally, only which one happens to ship first.

## Backend impact (`lifestack-api`)

- `app/application/insights.py` (new): `generate_workspace_insights(session, workspace) -> None`, calling the three detectors above and writing via `NotificationService.notify(...)` after the dedup check.
- `app/application/jobs.py`: new `dashboard_insights_job()` using the existing `run_workspace_job` wrapper (same advisory-lock + per-workspace-isolated-session pattern as `budget_guardrails_job`); new `ADVISORY_LOCK_DASHBOARD_INSIGHTS` constant (`1009`).
- `app/main.py`: `register_daily_job(dashboard_insights_job, job_id="dashboard_insights", hour_utc=6)` (after the overnight price/import jobs, well before a typical morning check-in).
- `docs/JOBS.md`: new entry (job 10).
- No schema migration, no new API endpoint — `GET /v1/notifications?category=insight` already exists and already supports the filter.

## Frontend impact (`lifestack-web`)

- `DashboardPage.tsx`: new "Insights" card section, fetching `GET /v1/notifications?category=insight&is_read=false&limit=5` via the existing notifications TanStack Query hook/service (reuse whatever `src/services/notifications*.ts` already exposes for the notifications bell — no new service file if one already covers list-with-filters). Renders each as a small card (title + body + severity-colored accent), empty state ("No insights right now — check back after your next few transactions") when the list is empty. No new components beyond this card section; no new routes.

## Golden test scenarios (required before merge)

Backend, `app/tests/application/test_dashboard_insights.py` (or `app/tests/integration/test_dashboard_insights.py` if it needs the full HTTP client to seed via existing endpoints — follow whichever existing job test file most resembles this, e.g. `test_scheduler.py` / the budget-guardrails tests):

1. **Spending anomaly triggers** — seed a category with a stable ~$1000/week trailing average, then a current week at $3000 (meets both the 1.5x ratio and the $500 absolute floor) → one `category="insight"` notification with `entity_type="spending_category_anomaly"`; re-running the job the same week does not create a second one (dedup).
2. **Spending anomaly does not trigger on a brand-new category** — a category with transactions only in the current week (no trailing history) → no anomaly notification (the `trailing_4wk_avg > 0` guard).
3. **Budget pace triggers** — a $1000 monthly budget, $700 spent by day 10 of a 30-day month (projected ≈ $2100) → one `category="insight"` notification with `entity_type="spending_budget_pace"`; running again later the same month does not duplicate it even though the projection changes.
4. **Recurring-charge candidate detected** — two $49.99 transactions in the same category in two different months, no matching `RecurringTransaction` → one `severity="info"` notification; adding a matching `RecurringTransaction` afterward and re-running does not create a new one going forward (though the original notification is not deleted — see Detector 3).
5. **Idempotent full run** — seed data that trips all three detectors, run `generate_workspace_insights` twice back-to-back → exactly the same notification count both times (no duplicates across the whole job, not just one detector).

## Out of scope

- **Email delivery.** Explicitly out per the task description — Phase 1 is in-app (+ push if Task 10 has shipped) only.
- **Per-insight-type notification preferences finer than the existing per-category (`category="insight"`) toggle.** A user can mute/configure all insights together via the existing `NotificationPreference` row for `category="insight"`; splitting anomaly vs pace vs recurring into separate preference categories is a follow-up if users ask for it.
- **Configurable thresholds.** The 1.5x/$500, 1.1x/5-day, and 5%/2-month constants above are fixed, not per-workspace settings — a follow-up spec can expose them if the fixed defaults prove wrong for enough users.
- **Cross-workspace or income-side insights.** Only `expense`-type transactions feed all three detectors; income anomalies (e.g. "you got paid early this month") are not addressed here.
- **Retroactively backfilling insights for historical weeks/months.** The job only ever looks at "as of today" windows; it does not generate insights for past periods on first deploy.
