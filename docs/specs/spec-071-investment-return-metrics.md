# Spec-071: Investment Return Metrics (XIRR / annualized / realized split, per-account + open vs closed)

**Created:** 2026-07-10
**Status:** Draft (rev. 3, 2026-07-10) — awaiting approval. No code until approved. Rev. 2 resolved the multi-currency and drawdown open questions per owner decision (historical-FX aggregate; include drawdown) and added the dependency on spec-072. Rev. 3 pins drawdown to overall-only (verified: `net_worth_snapshots` is workspace-level — no per-account dimension exists) and defines its input as investing value; adds the sub-year annualization guard (INV-7); includes income flows in closed-position XIRR; pins the day-count convention.
**Scope:** multi-repo, user-facing — `lifestack-api` (metrics computation + API) and `lifestack-web` (display on Investing summary + per-account cards). Two PRs, api merged first (one-PR-per-repo rule).
**Depends on:** spec-044 (FIFO cost basis), spec-046 (cost-basis accuracy), spec-048 (orders in reconciliation), spec-065 (net-worth snapshots — the time series drawdown uses), **spec-072 (historical FX + holdings upload — provides the historical FX series the cross-currency XIRR aggregate requires)**. Related domain doc: `docs/domain/cash-model-ledger-snapshots-reconciliation.md`.

---

## Problem

Every performance surface today reduces to **absolute profit** and a single **total-return %** (`total_gain_loss`, `total_gain_loss_pct` on `/investing/performance/summary`, echoed in the Investing summary cards and the new "By account" cards). Two things this cannot express:

1. **Time is invisible.** ₹1,00,000 profit on a position held 5 years is a very different outcome from the same profit in 6 months, but both render identically. Absolute return % also can't be compared across positions of different ages, so a portfolio-level "+22%" tells you nothing about the *rate* your money compounded at.
2. **Money-in timing is invisible.** Total-return % ignores *when* capital went in. Someone who added most of their money last month sees the same headline as someone who invested it all five years ago. The number the rest of the finance world uses for this — **XIRR** (money-weighted annualized return over dated cash flows) — is not computed anywhere.

Two structural gaps compound this:

3. **No per-account return.** Metrics exist only portfolio-wide. The user wants each brokerage account judged on its own (the "By account" cards added in the Holdings tab are the natural home).
4. **Open and closed positions are blended.** A fully-sold position stays in the system as a `Holding` with `quantity == 0` and its realized gain recorded on the sell orders (confirmed: `_recompute_holding` only deletes a holding when *no orders remain*; a fully-exited symbol persists at qty 0). Today nothing separates **current holdings** (open, mostly unrealized, still exposed to the market) from **exited positions** (closed, pure realized return, risk over). Several investment apps show these as distinct sections because they answer different questions — "how are my current bets doing" vs "how did my past decisions turn out".

## Goal

Add a set of **time- and money-aware return metrics**, computed **overall, per brokerage account, and split into open vs closed positions**, surfaced on the Investing summary and the per-account cards.

## Non-goals (this spec)

- Benchmark / alpha (needs an index price series we don't store) — future spec.
- **Dividend/income *capture*** is out of scope here — it's spec-073. But this spec **consumes** dividend income once it exists: a dividend is a positive cash flow in XIRR that is **not** a contribution, and part of total return (see INV-6). Dividend *yield* as a display metric can be a fast-follow once 073 lands.
- Sharpe / volatility (needs a risk-free-rate input) — future spec. **Max drawdown is included** in this spec (per owner decision) since it needs no extra input beyond spec-065 snapshots.
- Backfilling metrics for historical accuracy beyond what current order/snapshot data supports (see INV-3). Reconstructing deeper history is spec-072's job (user-provided data).

---

## Measures & definitions

| Metric | Definition | Inputs | Applies to |
|---|---|---|---|
| **XIRR** | Money-weighted annualized return: the rate `r` solving `Σ cashflow_i / (1+r)^(days_i/365) = 0`. Day-count convention is **fixed at 365** (pinned so test fixtures don't churn). Buys are negative flows, sells positive, dividends positive (INV-6), and (for open positions) current market value is a terminal positive flow dated today. | Order `occurred_at` + signed `net_amount`; dividend `pay_date` + `net_amount` (spec-073); current holding value | Overall, per-account, open, closed |
| **Annualized return %** | `total_return_pct` annualized over the holding period: `(1 + total_return)^(365/holding_days) − 1`. Cheap approximation of XIRR for a single lump; shown as a fallback when XIRR can't solve. Subject to the sub-year guard (INV-7). | Existing `total_gain_loss_pct` + earliest buy date | Overall, per-account |
| **Realized vs unrealized split** | Booked gain (locked in, taxable) vs paper gain (still at risk), shown separately rather than summed. | `realized_gain_loss` on orders; holding market value − book value | Overall, per-account |
| **Max drawdown** | Worst peak-to-trough decline of **investing value** (`holdings_value + investing_cash`) over the available snapshot history — spending cash excluded; investment drawdown, not net-worth drawdown. | `net_worth_snapshots` | **Overall only** — snapshots are workspace-level (one row per (workspace, date)); no per-account dimension exists |

**Open vs closed** is a *partition of positions*, orthogonal to the metric:
- **Open positions** — holdings with `quantity > 0`. Return = XIRR over that symbol's buy flows **plus** current market value as a terminal flow; carries unrealized + any partial realized gain.
- **Closed positions** — symbols where net `quantity == 0` (fully exited). Return = XIRR over buy and sell flows **plus any income flows attributed to the symbol** (dividends per INV-6 — including one paid after the final sell), **no terminal value**; pure realized return, and the period is bounded (first buy → last flow) so the annualization is exact.

## Solution

### Invariants (must hold)

- **INV-1 — Historical-FX aggregate, gated on rate availability.** XIRR is meaningful within one currency; the **cross-currency aggregate converts each cash flow at the historical FX rate for *its own date*** (from the historical FX series spec-072 provides), never a single spot rate. When a required historical rate is missing for any flow in scope, the aggregate returns `conversion_required` rather than a wrong number, while per-currency and per-account (single-currency) blocks still compute. This mirrors the existing `valuation_status` discipline on `/investing/summary`. Per-currency metrics do **not** depend on spec-072; only the aggregate does.
- **INV-2 — Metrics inherit data quality; never fabricate.** XIRR is only as good as the order history feeding it. Where cost basis / fees are known-incomplete (imported orders, pre-spec-046 data), the response flags the affected scope (`data_quality: partial`) rather than presenting a confident wrong rate. No silent gap-filling.
- **INV-3 — Non-retroactive for snapshot-derived metrics.** Max drawdown / any TWR uses `net_worth_snapshots`, which only exist from spec-065 ship date forward (INV-3 there). XIRR is order-derived and therefore *does* reach back to the first order, but drawdown honestly starts where snapshot history starts.
- **INV-4 — Solver safety.** XIRR is a root-find; it must be bounded, capped in iterations, and return `null` (falling back to annualized %) when it fails to converge or the flows are degenerate (all same sign, single flow, <1 day span). Never throw, never hang.
- **INV-5 — Open/closed partition is exhaustive and disjoint.** Every order-derived position is in exactly one of open (`qty > 0`) or closed (`qty == 0`); their realized components sum to the portfolio realized total. A unit invariant test asserts this.
- **INV-6 — Dividends are income, not contribution.** When dividend events exist (spec-073), each is a **positive cash flow at its pay date** in the XIRR series but is **excluded from invested capital / contributions**, and is added to realized return. This is the precise correction for today's "dividend modeled as a wallet→brokerage transfer" bug, which mislabels a return as owner-contributed capital and understates XIRR. If 073 has not shipped, dividends are simply absent (metrics compute on buys/sells only) — never approximated from transfers.
- **INV-7 — No annualized display under one year.** Annualizing a sub-year span manufactures alarming numbers (+4% held 20 days annualizes to ~+100%). When a scope's flow span is **< 365 days**, the API still returns the solved `xirr` (it is mathematically defined) but sets `annualization_reliable: false`, and the **UI shows the simple total return labeled with the holding period** ("+4.0% · held 3mo") instead of any annualized figure. The annualized-% fallback is never computed for sub-year spans. This guard protects trust in exactly the feature meant to add rigor.

### A. Metrics computation (lifestack-api)

- New `ReturnMetricsService` (investing domain) building on `FIFO`/order replay already in `order_service.py`. Given a scope (workspace, optional account_id, currency), it:
  1. Assembles dated signed cash flows from orders (buy = −net_amount, sell = +net_amount).
  2. Partitions positions into open (current holding qty > 0) and closed (qty == 0).
  3. For open, appends current market value as a terminal flow (reusing `InvestingSummaryService` valuation).
  4. Computes XIRR via a bounded Newton→bisection solver (pure function, own unit tests with hand-checked fixtures), annualized-% fallback, and the realized/unrealized split.
- Pure XIRR solver lives in a standalone module (`app/investing/xirr.py`) with property/edge tests (known IRR fixtures, non-convergence, single-flow, same-sign) — highest-value place for a property test.

### B. API (lifestack-api)

Extend the performance surface rather than add a parallel one:

- `GET /investing/performance/returns` →
  ```
  {
    currency, valuation_status, data_quality,   // overall scope
    overall:  { xirr, annualized_return_pct, total_return_pct,
                realized, unrealized, open: {...}, closed: {...} },
    by_account: [ { account_id, account_name, currency, xirr,
                    annualized_return_pct, realized, unrealized,
                    open: {...}, closed: {...}, data_quality } ],
    by_currency: [ ... ]   // when portfolio is multi-currency
  }
  ```
  Each `open`/`closed` block carries its own `{ xirr, realized, unrealized, market_value, invested }`. Numeric fields are nullable with a reason when unsolved (INV-4). Every scope carrying an annualized figure also carries `annualization_reliable` + `holding_days` (INV-7). `overall` additionally carries `max_drawdown: { pct, peak_date, trough_date } | null` (overall only; null before enough snapshot history).
- Existing `/investing/performance/summary` is unchanged (backwards compatible); the new endpoint is additive.

### C. Display (lifestack-web)

- **Investing summary strip**: add an **XIRR** stat card beside "Total gain/loss", labeled as annualized, with the annualized-% fallback shown when XIRR is `null`, an `N/A` + tooltip when `conversion_required`/`partial` (consistent with existing summary states), and the simple-return-with-period form when `annualization_reliable` is false (INV-7).
- **Per-account "By account" cards** (the section added in the Holdings tab): each card gains XIRR and a realized/unrealized breakdown alongside the existing invested/market/P&L.
- **Open vs Closed**: a segmented toggle (or two labeled sub-sections) on the returns display — "Current holdings" vs "Exited positions" — each showing its own XIRR + realized/unrealized. Closed section shows realized-only (no unrealized column). Empty states: "No exited positions yet" / "No open positions".
- Follows the dataviz house style; theme- and currency-display-preference aware; gains/losses use the existing emerald/rose convention.

## Now vs. Proposed

| Aspect | Now | Proposed |
|---|---|---|
| Return expression | absolute profit + total-return % | + XIRR (money-weighted, annualized) + annualized-% fallback |
| Time sensitivity | none (5yr and 6mo look identical) | XIRR/annualized reflect holding period |
| Contribution-timing sensitivity | none | XIRR reflects when money went in |
| Granularity | portfolio-wide only | + per brokerage account |
| Realized vs unrealized | summed into one number | shown separately |
| Open vs closed positions | blended (closed positions invisible except as qty-0 holdings) | distinct "Current holdings" vs "Exited positions" sections |
| Multi-currency | single `valuation_status` gate | per-currency + per-account metrics; cross-currency aggregate via **historical FX per flow** (INV-1, spec-072) |
| Dividends in return | modeled as wallet→brokerage transfer → counted as owner capital, understates return | income flow, not contribution (INV-6, needs spec-073) |
| Max drawdown | not shown | worst peak-to-trough of investing value (holdings + investing cash) from spec-065 snapshots, overall only |
| Sub-year positions | would show inflated annualized % | simple return + holding period; no annualization under 365 days (INV-7) |

## Testing & evidence

- `xirr.py` unit tests: hand-computed IRR fixtures (e.g. single buy + single sell), non-convergence → `null`, degenerate flows, sub-day span.
- `ReturnMetricsService` tests: open/closed partition exhaustive & disjoint (INV-5); realized components reconcile to portfolio realized total; per-account sums; `data_quality: partial` surfaced when fees/basis incomplete.
- Multi-currency test: mixed INR/USD portfolio returns `conversion_required` for the aggregate while per-currency blocks still compute (INV-1).
- INV-7 test: scope with flow span < 365 days returns `annualization_reliable: false` and no annualized fallback; ≥ 365 days returns it true.
- Drawdown test: hand-built snapshot series → known peak/trough; investing value (`holdings_value + investing_cash`) is the input, spending cash excluded; `null` with < 2 snapshots.
- Web: render tests for XIRR card (value / annualized-fallback / N/A / sub-year simple-return states) and the open/closed toggle with empty states.
- Coverage gate respected; no threshold changes.

## Decisions (resolved 2026-07-10)

1. **Cross-currency XIRR — DECIDED: historical-FX aggregate now.** Every flow converted at its own date's historical rate (INV-1), sourced from spec-072. This makes spec-072 (at least its FX-upload half) a prerequisite for the *aggregate* number; per-currency/per-account metrics ship independently.
2. **Max drawdown — DECIDED: included** in this spec, behind the same endpoint, using spec-065 snapshots. **Scope resolved (rev. 3): overall only** — verified against the schema: `net_worth_snapshots` is one row per `(workspace_id, snapshot_date)` with aggregate components; no per-account dimension exists, so per-account drawdown is structurally off the table until snapshots ever gain one. Input is investing value (`holdings_value + investing_cash`), not total net worth.
3. **XIRR terminal date for open positions — DECIDED: latest valuation date** used elsewhere on the summary, for consistency with the portfolio value shown.

## Remaining open question

- **Dividend yield as a display metric** — include a `dividend_yield` field once spec-073 lands, or leave dividends flowing only into XIRR/total-return? *Recommendation: metrics-only first (INV-6); add a yield card as a fast-follow.*
