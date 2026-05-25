# Feature Spec: Spending Analytics & Trends
**Status:** Proposed
**Spec ID:** 017

## 1. Overview
The spending module (Spec 003) supports transaction recording and budgets, but provides no analytical views. Users cannot answer "how has my food spending trended over 6 months?" or "what percentage of income goes to housing?" without manual calculation. This spec adds read-only analytics endpoints that compute trends, breakdowns, and comparisons from existing transaction data.

This builds on:
- Spec 003 (spending module): transactions, categories, budgets
- Spec 013 (recurring transactions): auto-generated entries included in analytics
- Spec 007 (dashboard): SQL aggregation mandate pattern

## 2. Goals
- Provide month-over-month spending trends per category.
- Surface income vs. expense ratios and net savings rate.
- Rank categories by spend volume with percentage breakdowns.
- Show budget vs. actual comparisons over time.
- Enable date-range-flexible queries for custom analysis windows.
- Push all aggregation to SQL (per Spec 007 mandate — no in-memory summing of rows).

## 3. Non-Goals (for this slice)
- Predictive analytics or forecasting.
- AI-generated spending insights or recommendations.
- Materialized views or pre-computed summary tables (compute on-read in V1).
- Export of analytics as reports (use Spec 006 export module for raw data).
- Comparison across workspaces.
- Custom user-defined metrics or KPIs.
- Real-time streaming analytics.

## 4. API Surface

### 4.1 Monthly Trends
`GET /v1/spending/analytics/trends`

Returns month-by-month totals for income and expense over a date range.

Query parameters:
- `from`: start month (YYYY-MM), default: 6 months ago
- `to`: end month (YYYY-MM), default: current month
- `type`: optional filter (`income` | `expense`)
- `category_id`: optional filter by category public_id

Response:
```json
{
  "from": "2025-12",
  "to": "2026-05",
  "months": [
    {
      "month": "2025-12",
      "total_income": "5000.00",
      "total_expense": "3800.00",
      "net": "1200.00",
      "transaction_count": 42
    }
  ]
}
```

### 4.2 Category Breakdown
`GET /v1/spending/analytics/breakdown`

Returns spending breakdown by category for a given period.

Query parameters:
- `from`: start date (YYYY-MM-DD), default: first of current month
- `to`: end date (YYYY-MM-DD), default: today
- `type`: `income` | `expense` (required)
- `limit`: max categories to return, default `10`

Response:
```json
{
  "from": "2026-05-01",
  "to": "2026-05-25",
  "type": "expense",
  "total": "3200.00",
  "categories": [
    {
      "category_public_id": "uuid",
      "category_name": "Food & Dining",
      "amount": "800.00",
      "pct_of_total": 25.0,
      "transaction_count": 15
    }
  ],
  "other": {
    "amount": "400.00",
    "pct_of_total": 12.5,
    "category_count": 3
  }
}
```

The `other` bucket aggregates categories beyond the `limit`.

### 4.3 Budget vs. Actual
`GET /v1/spending/analytics/budget-performance`

Returns budget utilization for each budgeted category over a time range.

Query parameters:
- `from`: start month (YYYY-MM), default: current month
- `to`: end month (YYYY-MM), default: current month

Response:
```json
{
  "from": "2026-05",
  "to": "2026-05",
  "categories": [
    {
      "category_public_id": "uuid",
      "category_name": "Food & Dining",
      "budget_amount": "1000.00",
      "actual_amount": "800.00",
      "utilization_pct": 80.0,
      "remaining": "200.00",
      "status": "on_track"
    }
  ],
  "totals": {
    "total_budgeted": "5000.00",
    "total_actual": "3200.00",
    "overall_utilization_pct": 64.0
  }
}
```

Status values: `on_track` (< 90%), `warning` (90-100%), `exceeded` (> 100%).

### 4.4 Savings Rate
`GET /v1/spending/analytics/savings-rate`

Returns net savings metrics over a time range.

Query parameters:
- `from`: start month (YYYY-MM), default: 6 months ago
- `to`: end month (YYYY-MM), default: current month

Response:
```json
{
  "from": "2025-12",
  "to": "2026-05",
  "months": [
    {
      "month": "2026-05",
      "income": "5000.00",
      "expense": "3200.00",
      "savings": "1800.00",
      "savings_rate_pct": 36.0
    }
  ],
  "period_totals": {
    "total_income": "30000.00",
    "total_expense": "19500.00",
    "total_savings": "10500.00",
    "average_savings_rate_pct": 35.0
  }
}
```

## 5. Implementation Guidance

### 5.1 SQL Aggregation Mandate
All analytics endpoints must push computation to PostgreSQL:
- Use `DATE_TRUNC('month', occurred_at)` for monthly grouping.
- Use window functions for running totals where needed.
- Use `SUM`, `COUNT`, and percentage calculations in SQL.
- The service layer assembles the response shape but does not iterate rows to compute sums.

### 5.2 Query Performance
- All queries filter by `workspace_id` first (leverages existing index).
- Add composite index on `(workspace_id, occurred_at)` for time-range queries.
- Add composite index on `(workspace_id, category_id, occurred_at)` for category-filtered trends.
- Date range cap: max 24 months per query to prevent unbounded scans.

### 5.3 Empty Data Handling
- Months with no transactions return zero values, not omissions.
- Categories with no budget return `null` for budget fields in budget-performance.
- If no data exists at all, endpoints return empty arrays with the requested date range.

## 6. Architecture Placement
Analytics endpoints live within the spending module:
- `app/spending/router.py` — new analytics sub-router or grouped endpoints
- `app/spending/service.py` — analytics methods
- `app/spending/repository.py` — SQL aggregation queries

No new models or tables needed. Analytics are pure read operations over existing `spending_transactions` and `spending_budgets` tables.

## 7. Test Plan
- **Unit tests:**
  - Date range parsing and validation
  - Status classification (on_track/warning/exceeded)
  - Savings rate calculation
  - "Other" bucket aggregation logic
- **Integration tests:**
  - Trends endpoint with known transaction data (verify monthly sums)
  - Category breakdown with limit and "other" bucket
  - Budget vs. actual accuracy
  - Empty workspace returns valid empty response
  - Date range cap enforcement (reject > 24 months)
  - Workspace isolation (no cross-workspace leakage)

## 8. Acceptance Criteria
- Four analytics endpoints operational with workspace scoping.
- All aggregation performed in SQL (no in-memory row iteration for sums).
- Date range validation enforces max 24-month window.
- Empty months/categories represented with zero values.
- Budget performance shows utilization status per category.
- Savings rate computed correctly for multi-month ranges.
- Appropriate composite indexes added for query performance.
- Workspace isolation verified in tests.

## 9. Migration
- Alembic migration adds composite indexes:
  - `ix_spending_transactions_workspace_occurred` on `(workspace_id, occurred_at)`
  - `ix_spending_transactions_workspace_category_occurred` on `(workspace_id, category_id, occurred_at)`
- No new tables required.
