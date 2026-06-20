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
