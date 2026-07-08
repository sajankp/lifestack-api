# Spec-065: Net Worth Over Time (live cash + daily net-worth history + graph)

**Created:** 2026-07-07
**Status:** Implemented (2026-07-08, api#132, web#87). The daily `net_worth_snapshot_job` was implemented against the on-demand `portfolio_snapshots` table during review, which would have silently skipped nearly every workspace (that table is only populated by a dashboard visit and is dropped daily by the price job); fixed pre-merge to compute holdings/cash live via `InvestingSummaryService`, matching the `GET /finance/net-worth` path. The job also wasn't wired into `app/main.py`'s APScheduler registration — added post-merge (`fix/register-net-worth-snapshot-job`) alongside this doc pass.
**Scope:** multi-repo, user-facing — `lifestack-api` (data + API) and `lifestack-web` (graph). Delivered as two PRs, api merged first (per one-PR-per-repo rule).
**Depends on:** spec-048 (orders in reconciliation), spec-050 (brokerage-only cash filter), PR #130 (create_snapshot brokerage filter). Related domain doc: `docs/domain/cash-model-ledger-snapshots-reconciliation.md`.

---

## Problem

Two things are true at once:

1. **Investing cash is computed two ways and one is stale-prone.** The Dashboard/Investing "Cash" stat reads a cached `portfolio_snapshots.cash_value`, computed by `create_snapshot` — a different code path from the live, brokerage-filtered `InvestingSummaryService.get_summary` used by net worth. They drifted (the brokerage-filter bug PR #130 patched) and the cached figure only refreshes when the snapshot is dropped, including as a side effect of the unrelated daily price job. Cash never depends on market prices; it should always be computed live for display.

2. **There is no history of net worth over time.** Net worth is only ever computed for *now*. Reconstructing it per-day on the fly is expensive (replay every order × historical price × FX for every day) and partially impossible: imported holdings (Demat/CAS, spec-060) have no backing order history to replay, so their past quantities are unrecoverable unless captured at the time. To ever show a "net worth over time" graph, each day's net worth must be **materialized** as it happens.

`portfolio_snapshots` already proves the materialization pattern for holdings value (which is why it exists — holdings quantity is mutated in place and, for imported holdings, non-reconstructable). But it is investing-only and bundles cash in a way that couples the live "Cash" display to a daily cache. The clean split: **display cash live; materialize a full daily net-worth series for history.**

## Solution

Three parts, one coherent feature:

- **A. Live cash for display** — one shared brokerage-filtered, FX-converted `_live_cash_total(...)`; the Dashboard and Investing "Cash" stats compute on the fly, never from a snapshot. `daily_change` becomes holdings-only on every surface.
- **B. `net_worth_snapshots`** — a new per-workspace, per-day materialized row: holdings + investing cash + spending cash + total, in the reporting currency, with the FX rates used. Populated daily and on-demand; the sole data source for the graph.
- **C. Net-worth-over-time graph** — a finance history endpoint + a web chart.

### Invariants (must hold)

- **INV-1 — Display cash is always live.** No live-facing figure (Dashboard "Cash", Investing "Cash", net-worth investing cash) ever reads a stored snapshot's cash. `net_worth_snapshots` is written for history and read *only* by the history endpoint. This is what makes the drift class structurally impossible.
- **INV-2 — `daily_change` is holdings-only** everywhere (cash movements are not performance). Resolves the documented "daily_change scope" gap.
- **INV-3 — Non-retroactive.** History begins accumulating from ship date. No backfill of `net_worth_snapshots` for dates before the feature existed (imported-holding history genuinely cannot be reconstructed; order-derived history could be but backfill is explicitly out of scope and would need its own spec). The graph honestly starts where data starts.
- **INV-4 — Module boundary.** `net_worth_snapshots` lives in the finance domain (it aggregates spending + investing). The population job reads investing via existing services, not by reaching into investing tables.

### A. Live cash + daily_change (lifestack-api)

- Extract `_live_cash_total(workspace_id, reporting_currency, as_of) -> Decimal | None` from the logic already in `get_summary` (brokerage-filtered, FX-converted, `None` when a required rate is missing). Both `PerformanceService.summary` and `get_summary` call it.
- `PerformanceService.summary.cash_total` is computed live, not read from `snapshot.cash_value`.
- `get_summary` daily_change switches from `previous.total_value` (holdings+cash) to `previous.holdings_value`. `PerformanceService.summary` is already holdings-only — unchanged.
- `portfolio_snapshots` schema is left untouched (no migration); its `cash_value`/`total_value` columns simply stop feeding any live display. They may be retired in a later cleanup spec once nothing references them.

### B. `net_worth_snapshots` table + population (lifestack-api)

New table (finance domain), one row per (workspace_id, snapshot_date):

| Column | Type | Notes |
|---|---|---|
| id | int PK | |
| workspace_id | FK, indexed | |
| snapshot_date | date | unique with workspace_id |
| reporting_currency | str(10) | the currency all figures are expressed in |
| holdings_value | Numeric(18,2) | holdings, reporting currency |
| investing_cash | Numeric(18,2) | brokerage cash, reporting currency |
| spending_cash | Numeric(18,2) | wallet/bank/card ledger balances, reporting currency |
| total_net_worth | Numeric(18,2) | sum of the three above |
| fx_rates_used | JSON | the rates applied that day (stable history) |
| created_at | timestamptz | |

- **Migration**: `create_table` with the enum/constraint patterns per change-control (inline, working `downgrade()` that drops the table). Unique constraint on (workspace_id, snapshot_date).
- **Population**: a per-workspace daily job (new `net_worth_snapshot_job`, or fold into an existing daily job) computes the three components in the workspace's reporting currency and **upserts** the row (idempotent on the unique key — safe to re-run same day). When reporting currency is unset or an FX rate is missing, the row is skipped for that day (documented; the graph shows a gap, consistent with `partial`/`conversion_required` elsewhere). Also upsert opportunistically when net worth is fetched, so today's point is fresh without waiting for the job.
- Reuses existing computations: holdings + investing cash from the investing summary service, spending cash from `account_service.get_spending_balances_bulk` — the same inputs `GET /finance/net-worth` already assembles. No new valuation logic.

### C. History endpoint + graph

- **API**: `GET /finance/net-worth/history?from=<date>&to=<date>` → ordered list of `{snapshot_date, reporting_currency, holdings_value, investing_cash, spending_cash, total_net_worth}`. Default range last 90 days; capped range. Returns only materialized rows (gaps are gaps).
- **Web** (`lifestack-web`): a net-worth-over-time chart on the Net Worth page — a stacked area (holdings / investing cash / spending cash) or a total line, honoring the theme and the currency display preference, with an empty/short-history state ("history builds from here"). Follows the dataviz house style.

## Now vs. Proposed

| Aspect | Now | Proposed |
|---|---|---|
| Dashboard / Investing "Cash" | cached `snapshot.cash_value` | **live** `_live_cash_total` (INV-1) |
| "investing cash" implementations | 2 (drift-prone) | 1 shared helper |
| `daily_change` (net worth / `/investing/summary`) | holdings-only vs `total_value` (incl. cash) → deposits look like gains | holdings-only (INV-2) |
| Net worth over time | not stored, not viewable | `net_worth_snapshots` daily series + graph |
| Historical net worth reconstructability | impossible for imported holdings | captured going forward (INV-3) |
| `portfolio_snapshots` | drives live cash + holdings history | holdings/gain-loss history only; cash columns legacy, live cash moved out |

## Tasks (TDD, after approval)

**api PR (merged first):**
1. `test:`→`feat:` `_live_cash_total` extracted; `summary.cash_total` live; brokerage filter enforced (extends PR #130 tests). (INV-1)
2. `test:`→`feat:` `get_summary` daily_change excludes a same-day cash deposit. (INV-2)
3. `feat:` Alembic migration creating `net_worth_snapshots` (+ working downgrade, unique constraint).
4. `test:`→`feat:` population upsert (idempotent same-day; skips on missing reporting currency / FX); on-demand upsert on net-worth fetch.
5. `test:`→`feat:` `GET /finance/net-worth/history` (range default/cap, ordering, gaps preserved, workspace isolation).
6. Update `docs/domain/cash-model-ledger-snapshots-reconciliation.md` (remove daily_change-scope gap; document live-cash + net_worth_snapshots) and `docs/JOBS.md` (new job; note price job no longer touches cash) **in the same pass**.
7. `uv run pytest --cov=app` (≥80), ruff; re-derive any dollar-figure assertions encoding cash-inclusive daily_change.

**web PR (after api merges):**
8. `test:`→`feat:` types + query hook for the history endpoint.
9. `test:`→`feat:` net-worth-over-time chart + empty/short-history state; `npm test`, `npm run build`, `npm run lint`.

## Owner decisions (2026-07-07)

- **Graph starts empty (INV-3): APPROVED.** Forward-only, no backfill. Fine to launch with a near-empty chart that fills over time.
- **daily_change becomes holdings-only (INV-2): APPROVED.** The user-visible shift on cash-movement days is accepted as correct.
- **Chart form: DECIDED — stacked area of components (holdings / investing cash / spending cash) with a total line overlaid.** Per dataviz house style.
- **Job placement: DECIDED — dedicated `net_worth_snapshot_job`** (clean idempotent unit), plus the on-demand upsert on net-worth fetch.
- **Bundle scope: DECIDED — full bundle (A + B + C).** Ship the live-cash decouple, the `net_worth_snapshots` materialization, and the graph together; A is not deferred.

## Risks / open questions

_All open questions resolved — see Owner decisions above._
