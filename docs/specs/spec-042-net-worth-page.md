# Spec 042 — Net Worth / Balance Sheet Page

**Status:** Implemented (api#83, merged 2026-07-04)
**Branch:** `feat/net-worth-page`

## Problem

There is no cross-module view that shows the user their complete financial position in one place. Spending account balances live on the Spending page; investing cash and portfolio value live on the Investing page. To see total net worth the user must mentally add three separate screens.

## Solution

Add a **Net Worth** page (`/net-worth`) that aggregates:
1. **Spending accounts** — projected ledger balance (income − expenses + net transfers) per account, already computed by the transfer-inclusive ledger service.
2. **Investing cash** — latest cash balance snapshots per account, converted to reporting currency.
3. **Portfolio holdings** — current market value of holdings, converted to reporting currency.

All three are summed to a single **Total Net Worth** figure in the workspace reporting currency.

## Backend

### New repository method
`AccountRepository.get_all_spending_balances(workspace_id)` — bulk version of `get_spending_balance`, returns all accounts in one query grouped by `account_id`.

### New schema (`finance/schemas.py`)
```
SpendingAccountBalance
  account_public_id: UUID
  account_name: str
  account_type: str
  currency_code: str
  balance: Decimal                          # native currency
  balance_in_reporting_currency: Decimal | None   # None if FX rate unavailable

NetWorthResponse
  reporting_currency: str | None
  spending_accounts: list[SpendingAccountBalance]
  spending_total: Decimal | None            # sum in reporting currency
  investing_cash_total: Decimal | None      # from InvestingSummary
  holdings_value: Decimal | None            # from InvestingSummary
  investing_total: Decimal | None           # cash + holdings
  total_net_worth: Decimal | None           # spending + investing total
  valuation_status: str                     # "ok" | "partial" | "no_reporting_currency" | "empty"
  fx_as_of: datetime | None
```

### New endpoint
`GET /v1/finance/net-worth` — authenticated, read-only, no new DB tables.

The handler calls:
- `account_service.account_repository.list_workspace_accounts()` + bulk balance query
- `InvestingSummaryService.get_summary()` (reused — already FX-converts investing assets)
- `FxRateRepository` to convert spending balances to reporting currency

## Frontend

### New page `NetWorthPage.tsx`
- Route: `/net-worth`
- Nav entry: "Net Worth" with `PieChart` icon, between Investing and Weekly Summaries
- **Summary cards row**: Spending Cash · Investing Cash · Portfolio · Total Net Worth
- **Spending accounts table**: account name, currency, native balance, reporting-currency balance
- **Status banner** when valuation is partial (FX missing, no reporting currency configured)

### Service
`financeService.getNetWorth()` in `services/finance.ts`

### No new DB migrations required.

## Out of scope
- Historical net worth chart (future)
- Liability / debt tracking (future)
- Manual overrides of account balances (use existing cash balance snapshots)
