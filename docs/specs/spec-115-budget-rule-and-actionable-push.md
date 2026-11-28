# Spec-115: One budget rule, an actionable morning push, and a Saturday net-worth push

**Status:** Approved by the owner (2026-11-28); not yet implemented.
**Scope:** `lifestack-api` (most of it) and one small `lifestack-web` change. E2E comes after both merge.
**Part of:** spec-113, phase 1.
**Branches:** API `fix/spec-115-budget-rule-and-push`, web `fix/spec-115-budget-card`.
**Delivery slot:** step 1 of 6 in spec-113 §7.

---

## 1. Problems (verified 2026-11-28)

### 1.1 Four answers to "am I on budget?"

Seeded workspace: one budget (Entertainment $120, spent $57.99), total month spend $750.04.

| Where | Answer | Cause |
|---|---|---|
| Dashboard card | "No group budgets set" | `DashboardSummaryWorkflow` builds `budget_spotlight` from `perf.groups` only (`app/application/workflows.py` ~L212) |
| Spending ▸ Budgets | on track | correct |
| Spend pacing | "Projected Over Budget", 625% | `get_spend_pacing` (`app/spending/service.py` ~L2525) divides **all** spend by the sum of **all** budget rows. Spending in categories without a budget inflates it, and a category budget inside a budgeted group is counted twice. |
| Weekly summary | "Budget utilization 111%" with "0 breached" | `app/summaries/service.py` ~L520-590 compares **one week's** spend with **monthly** category budgets and divides all spend by the budgeted total |
| Morning briefing | group budgets only | `_budget_guardrail_lines` (`workflows.py` ~L503) loops `perf.groups` only |

### 1.2 The morning push leads with things the owner doesn't act on

Prod, workspace 1, last 30 days:
- 26 briefing pushes; 15 contained "X spending is up this week" and 12 **led** with it.
- 22 spike notifications were written.

How it gets there:
- The push body is the first three briefing lines (`app/application/jobs.py` ~L1582).
- Spike lines come from `_detect_spending_anomalies` (`app/application/insights.py` ~L93), which also sums
  amounts **across currencies without conversion**.
- The daily "Portfolio +X today" line (`_net_worth_lines`) is pure information, yet it makes almost every
  briefing non-"all clear", so a push goes out every day.

### 1.3 The owner wants the week's change, not a summaries page

The owner opens Weekly Summaries only to see how net worth and investments moved over the week. No screen
separates **new money** (salary saved, SIP transfers) from **growth**. The weekly summary's change is
end − start (`summaries/service.py:878`, `_investing_summary` L598), so a SIP week looks like a great return.

## 2. Behaviour

### 2.1 One budget rule (`MonthBudgetStatus`)

A new service method `BudgetService.get_month_budget_status(workspace_id, month, as_of=None)` returns:

| Field | Meaning |
|---|---|
| `lines[]` | Every active budget row, as a category line or a group line. Each has `limit`, `spent`, `remaining`, `used_pct`, `status` (`on_track` / `warning` / `exceeded`), `daily_left` and `projected_month_end` (spent ÷ days elapsed × days in month). Reuse `get_budget_performance` (`spending/service.py` L2252) for the per-line numbers. |
| `covered_spend` | Spend in categories covered by any budget. Covered means the category has its own budget **or** its group has a group budget. |
| `covered_limit` | Sum of group budgets, plus category budgets whose group has **no** group budget. A category budget inside a budgeted group is a sub-limit and is **not** added, which removes the double count. |
| `uncovered_spend` | Total expense − `covered_spend`, shown as "not covered by a budget" |
| `used_pct` | `covered_spend / covered_limit` (null if there's no budget) |
| `breached_count` | Lines whose own `spent > limit` |
| `total_spend` | All expense for the month (unchanged meaning) |

All amounts are in the workspace reporting currency, using the conversion the pacing endpoint already
applies. There's no overall monthly limit in this spec; it needs a migration and lands with spec-113
phase 4.

**Consumers switch to it:**
- **Pacing** (`get_spend_pacing`, `SpendPacingResponse` in `spending/schemas.py` ~L551):
  - `total_budget = covered_limit`
  - `budget_consumed_pct = used_pct`
  - status compares projected **covered** spend with `covered_limit`
  - add `covered_spend`, `uncovered_spend`, `breached_count`
  - `actual_spend` and `projected_month_end_spend` keep their all-spend meaning (additive change)
- **Weekly summary**: `budget_utilization_pct` and `budgets_breached` become **month-to-date as of the week
  end**, from the helper. Add `uncovered_expense` and label the field "month to date". The per-category
  `budget_breached` flag uses the month-to-date spend against the monthly limit, not one week's spend.
- **Dashboard summary**: `budget_spotlight` takes category **and** group lines, top 2 by `used_pct`.
  Extend `BudgetSpotlightItem` with nullable `category_id` / `category_name`, so the existing group fields
  become nullable.
- **Briefing**: `_budget_guardrail_lines` covers category and group lines with the existing thresholds.

### 2.2 The briefing separates "act on" from "nice to know"

- `BriefingLine` (`app/dashboard/schemas.py` ~L59) gets `actionable: bool = False`.
- **Actionable:**
  - overdue todos
  - todos due today
  - health doses
  - budget guardrails
  - recurring items due today/tomorrow
  - imports awaiting review
- **Not actionable:** net-worth/portfolio line, weekly summary ready, insight lines.
- `BriefingResponse.all_clear` = **no actionable lines**. Info lines are still returned, and the Today page
  shows them.
- `morning_briefing_job`:
  - writes a notification and push **only if at least one actionable line exists**
  - builds the body from actionable lines (first 3, same sort)
  - otherwise logs `morning_briefing_skipped_nothing_actionable` with `workspace_id` (info level), so a
    quiet day can be told apart from a broken job
- Web: `DashboardPage` already uses `all_clear`. Nothing to change beyond verifying "All clear" isn't shown
  while a degraded-valuation card is visible. If it is, that card is information and stays below.

### 2.3 Turn the spending-spike detector off by setting

- `app/config.py`: `INSIGHT_SPENDING_ANOMALY_ENABLED: bool = False`.
- `generate_workspace_insights` (`insights.py` L397) skips `_detect_spending_anomalies` when it's false.
- The budget-pace and recurring-candidate detectors are unchanged; their lines are non-actionable (2.2).
- Stored spike notifications stay in the bell history. `_INSIGHT_ENTITY_ROUTES` stays.

### 2.4 Period change with new money split out (`PeriodChangeService`)

`PeriodChangeService.get_change(workspace_id, start: date, end: date)`, in a new module
`app/finance/period_change.py` (router → service → repositories):
- Points: the latest `NetWorthSnapshot` on or before `start` and on or before `end`. If either is missing,
  `status="unavailable"`. If the reporting currencies differ, `status="unavailable"`.
- **Net worth:**
  - `change = end.total − start.total`
  - `saved` = income − expense recorded in `(start, end]` (spending transactions, converted to the reporting
    currency with the same conversion the monthly summaries use)
  - `markets_and_other = change − saved`. This remainder includes market moves, FX on cash and manual
    balance edits; the label must say "markets & other".
- **Investments:**
  - value = `holdings_value + investing_cash`
  - `new_money` = capital transfers into accounts of type `brokerage` from non-brokerage accounts in the
    period (received amount), minus transfers out of brokerage to non-brokerage (gross), converted at each
    transfer's date
  - `growth = change − new_money`. Buys and sells inside a brokerage are neutral; dividends and fees are part
    of growth.
- If any conversion is missing: `status="partial"`, return the totals and set the split fields to null.
  Never return a split that's wrong.
- Response:

  ```
  {status, start_date, end_date, currency,
   net_worth: {start, end, change, saved, markets_and_other},
   investments: {start, end, change, new_money, growth}}
  ```

  Money values are strings.
- REST: `GET /finance/period-change?period=day|week|month|year` (end = today in the user's timezone, start =
  end − 1 / 7 / 30 / 365 days) or `?from=&to=`. Spec-113 phase 3 (Today) uses it.
- Cost: two snapshot reads, one transactions aggregate, one transfers query. **No investing analytics
  calls**; the prod VM has 1 GB.

### 2.5 Saturday weekly push

- New job `weekly_wealth_push_job`, registered in `app/main.py` with cron `day_of_week="fri", hour=23,
  minute=50, timezone="UTC"`, which is **Saturday 05:20 IST**. Settings
  `WEEKLY_WEALTH_PUSH_ENABLED: bool = True`, `WEEKLY_WEALTH_PUSH_DOW_UTC="fri"`, `..._HOUR_UTC=23`,
  `..._MINUTE_UTC=50`.
  - It runs after the 23:15 UTC net-worth snapshot and the 23:45 briefing.
  - **Trap**: Saturday morning IST is Friday in UTC. A test asserts the trigger's next fire time converts
    to a Saturday in Asia/Kolkata.
  - Uses `run_workspace_job` with a new advisory lock key added to the registry in `app/core/constants.py`.
- For each active workspace's primary user:
  - compute `get_change(end=today_in_user_tz, start=end−7)`
  - if `status != "unavailable"`, write one notification: category `weekly_wealth`, severity `info`, title
    "Your week", module `finance`
  - Body, complete case: "Net worth ₹28.71L, up ₹1,02,813 this week (₹40,000 saved, ₹62,813 markets &
    other). Investments up ₹95,000: ₹60,000 new money, ₹35,000 growth."
  - Body, partial case: just the totals.
- Money formatting: INR uses Indian grouping (₹1,02,813; lakh/crore abbreviation for the headline value).
  Check `app/core/currency.py` for an existing formatter before adding one. Other currencies use standard
  grouping.
- Push default: `NotificationService.notify` turns push on by default for `briefing` when a push
  subscription exists (`app/notifications/service.py` ~L57). Extend that to `weekly_wealth`.
- Web: add the `weekly_wealth` label ("Weekly net worth") to `src/utils/notificationLabels.ts`. The
  Notifications page lists preferences per category, so the owner can switch it off there.

## 3. Implementation plan (TDD; each task RED → GREEN → commit)

Setup:
```bash
cd /home/sajan/lifestack/lifestack-api && git fetch origin
git worktree add /home/sajan/lifestack-wt/spec-115 -b fix/spec-115-budget-rule-and-push origin/main
cd /home/sajan/lifestack-wt/spec-115
export PY=/home/sajan/lifestack/lifestack-api/.venv/bin/python PRECOMMIT=/home/sajan/lifestack/lifestack-api/.venv/bin/pre-commit
```
Fast loop: `$PY -m pytest <file> -p no:cacheprovider --no-cov -q`.

| # | Task | RED tests (new or extended) | Commit |
|---|---|---|---|
| 1 | `get_month_budget_status` | `app/tests/spending/test_month_budget_status.py`: one category budget + unbudgeted spend → `used_pct` from covered spend only, `uncovered_spend` correct · category budget inside a budgeted group not double-counted · `breached_count` per line · no budgets → `used_pct` None | `fix(spending): one month budget status (spec-115)` |
| 2 | Pacing uses it | Extend the pacing tests: the seeded case (Entertainment $120, $57.99, total $750.04) → not "over budget", `uncovered_spend == 692.05` | `fix(spending): pacing compares covered spend with covered limits` |
| 3 | Weekly summary uses it | Week spend $300 in a category with a monthly $1,000 budget → not breached; utilization is month-to-date | `fix(summaries): month-to-date budget fields` |
| 4 | Dashboard spotlight + briefing guardrails include category budgets | Workflow tests: a category-only budget appears in the spotlight and, at ≥ the warning %, in the briefing | `fix(dashboard): category budgets in spotlight and briefing` |
| 5 | `actionable` + all_clear + push only when actionable | Briefing workflow tests per domain flag · job test: info-only briefing → no notification and the skip log · actionable present → body has only actionable lines | `feat(briefing): push only actionable lines (spec-115)` |
| 6 | Spike detector setting | `generate_workspace_insights` with the flag off doesn't call the detector; flag on is unchanged | `chore(insights): disable spending-spike detector by default` |
| 7 | `PeriodChangeService` + REST | Unit tests with seeded snapshots, transactions and transfers: SIP-style transfer → `new_money`, growth excludes it · saved = income − expense · missing FX → `partial`, null split · missing snapshot → `unavailable` · router test for `period=week` and the timezone end date | `feat(finance): period change with new-money split (spec-115)` |
| 8 | Weekly push job | Trigger test (fires Saturday in Asia/Kolkata) · job writes one `weekly_wealth` notification with the formatted body · partial case · unavailable → nothing · push default on with a subscription | `feat(notifications): Saturday weekly net-worth push (spec-115)` |

Finish:
- `git add -A && $PRECOMMIT run --all-files`
- Full suite with `--cov-fail-under=80` (background; more than 10 minutes)
- Update `docs/specs/spec-067-morning-briefing.md` and `spec-058-dashboard-insights.md` with a short
  "Superseded in part by spec-115" note

**Web PR (after the API PR merges):** branch `fix/spec-115-budget-card` in `lifestack-web`.
- `DashboardPage.tsx` (~L205-216): the budget card counts category and group budgets. The note becomes
  "N budgets tracked" / "No budgets set" plus "₹X not covered by a budget".
- `types/dashboard.ts`: nullable group fields, new category fields.
- `notificationLabels.ts`: `weekly_wealth`.
- The pacing card shows `uncovered_spend` as its own line and stops saying "Runaway burn rate". Wording:
  "On pace to spend ₹X this month" / "₹Y not covered by a budget".
- Vitest for each change.

**E2E (after both merge):** in `lifestack-e2e`, one spec covering:
- a category budget shows on the dashboard
- the pacing card isn't "over budget" when only unbudgeted spend is high

## 4. Acceptance
- Pacing, the weekly summary, the dashboard and the briefing agree on budget status for the same data (one
  test seeds the data and asserts all four).
- Over a month of runs on prod: no morning push leads with a spending spike; on a day with nothing to act
  on, no push is sent and the skip log line appears.
- The Saturday 05:20 IST push shows the week's change split into saved vs. markets & other, and new money
  vs. growth.
- Coverage ≥ 80%, pre-commit green. Deploy only when the owner authorises it.

## 5. Not in scope
- An overall monthly limit and merging Budgets with KPIs (spec-113 phase 4).
- Today page UI (spec-113 phase 3; it consumes `/finance/period-change`).
- Tax (spec-114).
