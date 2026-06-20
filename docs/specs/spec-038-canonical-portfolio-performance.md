# Spec 038: Canonical Portfolio Performance

**Status:** Implemented
**Approved:** 2026-06-20

## Contract

- Portfolio value is current holdings market value, excluding investment-account cash.
- Invested value is holdings cost basis in the reporting currency.
- Total gain/loss is portfolio value minus invested value.
- Daily change compares holdings market value with the latest valid prior-day snapshot.
- Cash total is reported separately and never contributes to gain/loss.
- Dashboard and Investing consume the same performance service and expose matching values.
- Missing reporting currency, FX rates, prices, or a prior snapshot produces an unavailable or
  partial state and nullable metrics instead of misleading zeroes.

## Response Fields

The performance and dashboard investing summaries expose `portfolio_value`, `invested_value`,
`total_gain_loss`, `total_gain_loss_pct`, `daily_change`, `daily_change_pct`, `snapshot_date`,
`previous_snapshot_date`, `valuation_status`, `holdings_count`, and `cash_total`.

The existing `total_value` and `total_cost` performance fields remain as compatibility aliases for
`portfolio_value` and `invested_value`.

## Acceptance Criteria

- Dashboard and Investing display identical portfolio values and performance metrics.
- Cash is visible separately and does not change profit/loss.
- Daily change is nullable without a valid prior-day snapshot.
- Positive, negative, zero-cost, multi-currency, stale/unavailable, and workspace-isolation paths
  have meaningful automated coverage.

## Clarification: Newly Entered Historical Holdings

The current holding model stores quantity and average cost but does not ask when units were bought
or maintain a transaction ledger. A user may therefore enter an existing holding today even though
it was purchased months or years earlier.

Adding that holding must not make its entire market value appear as one-day portfolio movement.
Daily change represents market-price movement only:

`sum(current quantity × latest close) - sum(current quantity × previous close)`

The same current quantity is used on both sides so that adding or editing a holding does not itself
look like market profit or loss.

Interim missing-history policy:

- If both the latest and previous close exist, include the holding's price movement.
- If a newly entered holding has no previous close, its contribution to daily change is `0`.
- Do not substitute average cost, the holding's full market value, or a newly created portfolio
  snapshot as the previous-day value.
- The response may still expose a partial or stale valuation status so the UI can indicate that the
  daily figure excludes holdings without comparable price history.
- Total gain/loss continues to use current value minus average-cost basis and is unaffected by this
  daily-change policy.

This zero-contribution rule is an interim product decision. It avoids a misleading one-day jump
while keeping the portfolio-level daily metric available. A future implementation may instead show
`N/A` when comparison coverage falls below an explicitly defined threshold.

## Deferred Plan: Investment Transaction Ledger

A future investing slice should replace direct quantity/average-cost maintenance with
transaction-aware holdings. It should support:

- dated buys, additional buys, sells, transfers, fees, and optional dividends;
- future purchases that increase quantity and recalculate weighted average cost;
- partial sells that reduce quantity without incorrectly rewriting the remaining cost basis;
- backdated transactions and deterministic rebuilding of quantity and cost basis;
- transaction currency and FX treatment;
- daily performance based on the quantity actually held on each valuation date;
- separation of market return from cash flows and deposits;
- auditable corrections rather than silently overwriting historical ownership data;
- migration and compatibility rules for existing holdings that currently contain only quantity and
  average cost.

Until that ledger is implemented, `quantity` and `avg_cost` remain user-maintained summary fields,
and historical performance before the holding was entered cannot be reconstructed accurately.
