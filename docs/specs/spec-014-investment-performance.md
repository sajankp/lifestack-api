# Feature Spec: Investment Performance & Returns
**Status:** Proposed
**Spec ID:** 014

## 1. Overview
The investing module (Spec 008) tracks holdings and cash balances, and Spec 011 added currency governance and FX valuation. However, there is no mechanism to track portfolio value over time, calculate returns, or surface gain/loss metrics. This spec adds historical valuation snapshots and return calculations.

This builds on:
- Spec 008 (investing MVP): holdings, cash balances
- Spec 011 (FX & currency): persisted FX rates, valuation semantics
- Spec 012 (look-through): instrument/constituent model

## 2. Goals
- Record daily portfolio value snapshots for historical tracking.
- Calculate holding-level and portfolio-level gain/loss (absolute and percentage).
- Support time-weighted return (TWR) calculation for meaningful performance comparison.
- Enable performance visualization over configurable time windows.
- Maintain full auditability of valuation inputs.

## 3. Non-Goals (for this slice)
- Real-time or intraday pricing (batch daily snapshots only).
- Broker API integration for automatic price feeds.
- Money-weighted return (MWR/IRR) — TWR is simpler and sufficient for V1.
- Benchmark comparison (e.g., vs. S&P 500).
- Tax lot tracking or realized vs. unrealized gain separation.
- Dividend tracking and total return inclusive of income.
- Automated price fetching (manual or import-based in V1).

## 4. Data Model

### HoldingPrice
Daily price records for holdings:
- `id`: internal PK
- `workspace_id`: tenant FK
- `holding_id`: FK to `investing_holdings`
- `price_date`: date
- `unit_price`: `NUMERIC(18, 6)` — price per unit in holding's currency
- `source`: enum `manual` | `import` | `api` (future)
- `created_at`

Constraints:
- unique `(holding_id, price_date)`
- `unit_price > 0`

### PortfolioSnapshot
Daily workspace-level portfolio valuation:
- `id`: internal PK
- `workspace_id`: tenant FK
- `snapshot_date`: date
- `total_value`: `NUMERIC(18, 2)` — sum of all holdings + cash in base currency
- `total_cost`: `NUMERIC(18, 2)` — sum of all cost bases in base currency
- `holdings_value`: `NUMERIC(18, 2)` — holdings only (excl. cash)
- `cash_value`: `NUMERIC(18, 2)` — cash balances in base currency
- `currency_code`: workspace base currency used for this snapshot
- `fx_rates_used`: JSONB — snapshot of FX rates applied for multi-currency conversion
- `created_at`

Constraints:
- unique `(workspace_id, snapshot_date)`

### HoldingSnapshot
Daily per-holding valuation (optional granularity):
- `id`: internal PK
- `workspace_id`: tenant FK
- `holding_id`: FK to `investing_holdings`
- `snapshot_date`: date
- `quantity`: `NUMERIC(18, 6)`
- `unit_price`: `NUMERIC(18, 6)`
- `market_value`: `NUMERIC(18, 2)` — quantity × unit_price
- `cost_basis`: `NUMERIC(18, 2)` — quantity × avg_cost
- `gain_loss`: `NUMERIC(18, 2)` — market_value - cost_basis
- `gain_loss_pct`: `NUMERIC(8, 4)` — percentage gain/loss
- `currency_code`: holding's native currency
- `created_at`

Constraints:
- unique `(holding_id, snapshot_date)`

## 5. API Surface

### Price Management
- `POST /v1/investing/prices` — submit price(s) for one or more holdings for a date
- `GET /v1/investing/prices?holding_id={id}&from={date}&to={date}` — price history

Request body for bulk price submission:
```json
{
  "price_date": "2026-05-25",
  "prices": [
    { "holding_public_id": "uuid", "unit_price": "152.30" }
  ]
}
```

### Performance Endpoints
- `GET /v1/investing/performance/summary` — current portfolio performance metrics
- `GET /v1/investing/performance/history?from={date}&to={date}&interval={daily|weekly|monthly}` — time-series portfolio values

#### Summary Response Shape
```json
{
  "total_value": "125000.00",
  "total_cost": "100000.00",
  "total_gain_loss": "25000.00",
  "total_gain_loss_pct": "25.00",
  "day_change": "500.00",
  "day_change_pct": "0.40",
  "twr_ytd": "12.50",
  "twr_1y": "18.30",
  "snapshot_date": "2026-05-25",
  "currency": "USD",
  "holdings": [
    {
      "holding_public_id": "uuid",
      "symbol": "VWRA",
      "market_value": "80000.00",
      "cost_basis": "65000.00",
      "gain_loss": "15000.00",
      "gain_loss_pct": "23.08",
      "weight_pct": "64.00"
    }
  ]
}
```

#### History Response Shape
```json
{
  "from": "2026-01-01",
  "to": "2026-05-25",
  "interval": "monthly",
  "currency": "USD",
  "data_points": [
    { "date": "2026-01-31", "total_value": "105000.00", "twr_cumulative": "5.00" }
  ]
}
```

## 6. Scheduler Job

### Job: `portfolio_snapshot_job`
- **Trigger:** daily at 01:00 UTC (configurable via `PORTFOLIO_SNAPSHOT_HOUR`)
- **Scope:** iterate active workspaces with investing holdings

Per workspace:
1. For each active holding, find the latest available price (from `holding_prices` or fall back to `avg_cost` if no price recorded).
2. Calculate holding-level market values.
3. Apply FX conversion using latest persisted rates (Spec 011) for multi-currency portfolios.
4. Aggregate into `portfolio_snapshots` row for today.
5. Write `holding_snapshots` rows for each holding.
6. Emit audit event.

### TWR Calculation
Time-weighted return is calculated from the snapshot series:
- Split periods at each external cash flow (capital transfer from Spec 011).
- Sub-period return = (end_value - start_value - net_flow) / start_value_adjusted.
- Chain sub-period returns geometrically: TWR = ∏(1 + r_i) - 1.

TWR is computed on-read from snapshots, not stored, to avoid stale cached values.

## 7. Configuration
- `PORTFOLIO_SNAPSHOT_HOUR`: hour (UTC) to run snapshot job, default `1`.
- `PERFORMANCE_HISTORY_MAX_DAYS`: max range for history queries, default `1825` (5 years).

## 8. Graceful Degradation
- If no prices are available for a holding on snapshot day, use last known price with a `stale_prices: true` flag in the snapshot metadata.
- Performance endpoints return `data_available: false` with an appropriate message if no snapshots exist yet.
- TWR calculation requires at least 2 snapshots; endpoints return `null` for TWR fields if insufficient data.

## 9. Audit Events
- `holding_prices_submitted` — user submits price data
- `portfolio_snapshot_created` — system generates daily snapshot (module: `application`)

## 10. Test Plan
- **Unit tests:**
  - TWR calculation with known inputs (verify against manual calculation)
  - Gain/loss computation at holding and portfolio level
  - FX conversion in multi-currency portfolios
  - Stale price fallback logic
  - Interval aggregation (daily → weekly/monthly)
- **Integration tests:**
  - Price submission → snapshot generation → performance query roundtrip
  - Multi-currency portfolio with FX rates applied
  - History endpoint with various date ranges and intervals
  - Workspace isolation for snapshots and prices

## 11. Acceptance Criteria
- Price submission endpoint accepts bulk prices for a date.
- Daily snapshot job produces portfolio and holding snapshots.
- Performance summary returns current gain/loss and TWR metrics.
- History endpoint returns time-series data with configurable intervals.
- Multi-currency portfolios use persisted FX rates for base-currency conversion.
- Stale price fallback is clearly surfaced in API responses.
- All mutations and snapshot generations emit audit events.

## 12. Migration
- Alembic migration adds `holding_prices`, `portfolio_snapshots`, `holding_snapshots` tables.
- No data backfill (snapshots begin from first job run after deployment).
