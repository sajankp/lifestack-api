# Spec-077: Custom Financial KPIs

**Created:** 2026-07-12
**Status:** Draft
**Depends on:** spec-064 (category-group budgets — the ceiling of what's configurable today), spec-058 (dashboard insights — notification precedent), budget guardrails job (evaluation-cadence precedent)
**Scope:** multi-repo, user-facing — `lifestack-api` (KPI model/evaluation/endpoints) + `lifestack-web` (builder UI, dashboard cards).

## Problem

Sequence #4. Budgets answer one question shape: "did spend in {category|group} exceed ₹X this
month?" Users cannot define other recurring financial questions they actually track — savings rate
≥ 30%, eating-out spend under ₹6k/month, income-to-EMI ratio, monthly net cash flow positive —
without exporting data. Spec-064 explicitly did not scope this ("not scoped by spec-064",
roadmap).

## Solution

A `financial_kpis` definition table + deterministic evaluation, **predefined metric types only in
v1** (no expression language — see Fenced-off below):

- **Definition:** name, `metric_type` enum (v1 — owner-confirmed 2026-07-12: `spend_total`,
  `income_total`, `net_cash_flow`; `savings_rate` and `category_ratio` are future enum
  extensions, not v1), filter (categories/groups/accounts), window (`calendar_month`,
  `calendar_week`, `rolling_30d`), optional target (value + direction ≤/≥), display format.
  **Single-currency constraint (v1):** every account in a KPI's filter must share one
  `default_currency_code`; the KPI evaluates and displays in that currency. Enforced as a hard
  validation error at definition create/update AND re-checked at evaluation time (account sets
  and filters can change after definition) — summing native amounts across mixed currencies is
  the spec-050 bug class and is never permitted. An unfiltered (all-accounts) KPI in a
  mixed-currency workspace is therefore invalid in v1 until spec-075's as-of conversion lands.
- **Evaluation:** computed from `spending_transactions` via the existing summary aggregation
  paths — read-only over the ledger, no new stored aggregates (KPI values are derived state,
  recomputable from events; storing them would create a second source of truth to drift).
- **Surfaces:** `GET /v1/spending/kpis` (definitions + current values), dashboard KPI cards,
  and target-breach notifications riding the budget-guardrails job cadence
  (`Notification(category="kpi")`, same threshold semantics as guardrails).

## Backend / API / schema impact

- New table `financial_kpis` (workspace-scoped composite FKs; enum inline in `create_table`;
  working downgrade). No changes to transactions/budgets tables.
- Router/service/repository in `app/spending/` (single-module); guardrails-job extension is the
  one `app/application/` touch.

## Fenced-off

- **No expression language / arbitrary formulas in v1.** Two money-math implementations diverging
  is the known failure mode (web PR #58 precedent); a formula engine is that risk squared, plus
  an injection surface. If the enum types prove insufficient, extending the enum is a follow-up
  spec.
- **No investing-side KPIs in v1** (XIRR targets, allocation drift): spec-071 return metrics
  already cover the read side; mixing ledger-derived and snapshot-derived quantities in one
  user-composed metric invites partition violations (cash-model rule: never mix the two stores in
  one number). Revisit as its own spec with the partition argument written out.

## Out of scope

- Historical KPI backfill/time-series charting of KPI values (v1 shows current window + target
  state; a series can derive later from the same read path).
- Sharing/templates.

## Resolved questions (owner, 2026-07-12)

1. v1 enum: **`spend_total`, `income_total`, `net_cash_flow`** — exactly these three.
2. `savings_rate` denominator (recorded for the future enum extension): **income in window**.
3. Surfaces: **both** — dashboard cards AND breach notifications in v1.
4. Currency: **native-currency v1 with the single-currency constraint** (see Definition above).
   Plainly: a KPI may only aggregate accounts that all share one currency — the KPI's number is
   in that currency. It may NOT sum a USD account and an INR account (₹100 + $100 = 200 of
   nothing — the spec-050 bug class). Cross-currency KPIs (converted to reporting currency via
   spec-075's daily rates) are a follow-up once 075 lands.
