# Spec-096: Periodic Summaries, Investment Analytics Over Time, and Spend Pacing

**Created:** 2026-09-13
**Status:** Approved (implementation)
**Scope:** API (`app/summaries`, `app/investing`, `app/spending`), Web (`WeeklySummariesPage`, `investing/AnalyticsTab`, `spending/AnalyticsTab`)
**Depends on:** spec-016 weekly summary, spec-017 spending analytics, spec-065 net worth over time, spec-071 investment return metrics, spec-076 weekly summaries enhancements

---

## 1. Problem & Strategic Vision

Lifestack's analytics currently have three structural gaps that limit tracking effectiveness and actionable ROI:

1. **Summaries are weekly-only, while personal finance runs on monthly cycles:**
   Income (salaries), major recurring commitments (rent/EMI), utilities, and investments (monthly SIPs) are structured around monthly cycles. A weekly digest frequently flags artificial deficits (3 out of 4 weeks have zero salary) and captures noisy 7-day equity market fluctuations rather than meaningful accounting closures.
2. **Investment analytics lack time-series performance:**
   The Investing module offers day-level look-through constituent overlap (spec-012) and aggregate position return metrics (spec-071), but provides no historical time-series chart of portfolio growth. Users cannot visualize compounding: specifically, the divergence between cumulative capital invested (cost basis / net inflows) and current portfolio market value over time.
3. **Spending analytics lack real-time pacing & burn rate:**
   Spending analytics (spec-017) displays historical monthly bars and category breakdowns, but does not provide month-to-date pacing context. Users cannot answer: "Given that we are on Day 13 of the month, am I burning budget faster than expected, and what is my projected month-end spend?"

### The 3-Tier Analytics Roadmap

- **Tier 1 (Immediate High-ROI Wins):**
  - **Monthly Financial Close Summary:** Dedicated calendar-month summary workflow (`MonthlySummary`), rolling up tasks, spending, investing, dividends, net worth, and return metrics.
  - **Investment Performance Over Time:** Time-series endpoint (`GET /v1/investing/performance/history`) querying `PortfolioSnapshot` rows, paired with an interactive chart in the Investing section comparing portfolio market value against total invested cost basis over 1M, 3M, 6M, 1Y, and All-time windows.
  - **Monthly Spend Pacing & Burn Rate:** Real-time spending pacing analytics (`GET /v1/spending/analytics/pacing`), calculating daily burn rate, elapsed month progress, pacing vs. budget, and projected month-end spend.
- **Tier 2 (Strategic Depth & Granularity):**
  - **Fixed (Committed) vs. Discretionary Spend Categorization:** Tagging/classifying recurring obligations (rent, EMI, utilities, subscriptions) vs. discretionary lifestyle spend (dining, shopping, entertainment) to compute the true "survival burn rate" and discretionary surplus.
  - **Dividend Income & Yield-on-Cost Trajectory:** Dedicated dividend time-series and yield-on-cost charts over time, visualizing passive cash flow growth across months and years.
- **Tier 3 (Advanced Frontier & Lifestack Moat):**
  - **Benchmark Alpha Comparison:** Ingesting daily benchmark indices (Nifty 50 TRI, S&P 500 TRI) to calculate tracking error, beta, and user alpha over standardized horizons.
  - **Cross-Module Correlations:** Correlating productivity/habits (overdue tasks, missed doses, high stress) with financial discipline (impulse spending spikes, missed investment SIPs).

---

## 2. Tier 1 Solution & Specification

### A. Monthly Summaries Architecture
- **Data Isolation & Model:** A dedicated `monthly_summaries` table via migration `0066_monthly_summaries.py`, avoiding schema mutations or index contention on `weekly_summaries`.
- **Date Bounds:** `month_start` (first day of month) and `month_end` (last day of month).
- **Core Schema (`MonthlySummary`):**
  - `id`, `public_id`, `workspace_id`, `month_start`, `month_end`, `generated_at`
  - `todo_summary`, `spending_summary`, `investing_summary`, `health_summary`, `dividend_summary`, `net_worth_summary`, `return_metrics_summary`, `highlights`
  - `read_at`, `superseded_by_id`, `regenerated_at`, `regeneration_reason`
  - Unique partial index: `(workspace_id, month_start)` WHERE `superseded_by_id IS NULL`.
- **Composition Sharing:** Refactor `app/summaries/service.py` to extract date-range composition `_compose_range(workspace_id, start_date, end_date)`. Both `WeeklySummaryService` and `MonthlySummaryService` call the shared composition pipeline.
- **API Endpoints:**
  - `GET /v1/summaries/monthly`
  - `GET /v1/summaries/monthly/latest`
  - `GET /v1/summaries/monthly/{public_id}`
  - `POST /v1/summaries/monthly/{public_id}/read`
  - `POST /v1/summaries/monthly/{public_id}/regenerate`
  - `POST /v1/summaries/monthly/generate?year=...&month=...`
- **UI:** Add cadence switcher (`Weekly` | `Monthly`) in `WeeklySummariesPage.tsx`, rendering identical cards.

### B. Investment Performance Over Time
- **Data Source:** Daily `PortfolioSnapshot` (`portfolio_snapshots`) rows, which already store `holdings_value`, `total_cost` (cost basis), `cash_value`, `total_value`, and `currency_code`.
- **API Endpoint:**
  - `GET /v1/investing/performance/history?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD`
- **Response Format:**
  ```json
  {
    "currency": "USD",
    "points": [
      {
        "snapshot_date": "2026-08-01",
        "holdings_value": "120000.00",
        "total_cost": "105000.00",
        "total_value": "125000.00",
        "cash_value": "5000.00",
        "unrealized_gain_loss": "15000.00",
        "unrealized_gain_loss_pct": "14.29"
      }
    ]
  }
  ```
- **UI Component:** `PortfolioPerformanceChart.tsx` in `investing/AnalyticsTab.tsx`:
  - Time range selector: `1M`, `3M`, `6M`, `1Y`, `All`.
  - Responsive SVG line chart: Green/Teal for Market Value, Slate/Indigo for Invested Capital.
  - KPI summary pills: Current Value, Total Invested, Gain/Loss amount and %.

### C. Monthly Spend Pacing & Burn Rate
- **Calculation Logic:**
  - `days_in_month`: total calendar days in selected month.
  - `days_elapsed`: min(current_day, days_in_month) if current month, else days_in_month.
  - `actual_spend`: sum of expense transactions in workspace for the month.
  - `daily_burn_rate`: `actual_spend / days_elapsed`.
  - `projected_month_end_spend`: `daily_burn_rate * days_in_month`.
  - `total_budget`: sum of active monthly budgets.
  - `budget_consumed_pct`: `(actual_spend / total_budget) * 100` if budget exists.
  - `target_pace_pct`: `(days_elapsed / days_in_month) * 100`.
  - `pacing_delta_pct`: `budget_consumed_pct - target_pace_pct`.
  - `status`: `"under_budget" | "on_track" | "over_pacing" | "no_budget"`.
- **API Endpoint:** `GET /v1/spending/analytics/pacing?month=YYYY-MM`
- **UI Component:** `SpendPacingCard.tsx` positioned prominently above trends in `spending/AnalyticsTab.tsx`.

---

## 3. Out of Scope (Tier 2 & 3)
- Real-time third-party broker sync or webhooks.
- Benchmark index data feed ingestion (Tier 3).
- Automated NLP spending narrative generation.
