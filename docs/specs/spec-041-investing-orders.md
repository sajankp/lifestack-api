# Spec-041: Transaction-Based Investing Orders

**Created:** 2026-06-27
**Status:** Approved
**Source:** `/root/projects/lifestack/docs/PLAN-INVESTING-ORDERS.md`

---

## Problem

The current investing flow requires 4+ manual steps with no audit trail: create transfer → manually update cash balance snapshot → manually create holding with hand-calculated avg_cost → manually recalculate on subsequent purchases. There is no sell flow and no per-trade history.

## Solution

Introduce an `InvestingOrder` model (buy/sell trades) that automatically:
- Deducts/adds to brokerage cash balance (creates new `CashBalance` record)
- Creates or updates `Holding` with computed weighted avg_cost
- Records realized gain/loss on sell orders

Also wire `CapitalTransfer` to auto-create a `CashBalance` record when `to_module = 'investing'`.

## New Model: `investing_orders`

| Column | Type | Notes |
|---|---|---|
| id | PK | internal |
| public_id | UUID | external |
| workspace_id | FK → workspaces | |
| user_id | FK → users | |
| account_id | FK → accounts (brokerage only) | composite FK with workspace_id |
| order_type | enum: buy/sell | |
| symbol | str(20) | uppercase |
| instrument_id | FK → investing_instruments, nullable | |
| quantity | Decimal(18,8) | > 0 |
| price_per_unit | Decimal(18,6) | > 0 |
| gross_amount | Decimal(18,2) | quantity × price_per_unit |
| brokerage_fee | Decimal(12,2) | default 0 |
| tax_amount | Decimal(12,2) | default 0 |
| other_fees | Decimal(12,2) | default 0 |
| net_amount | Decimal(18,2) | buy: gross + fees; sell: gross - fees |
| currency | str(10) | |
| exchange_name | str(50), nullable | e.g. NSE, NASDAQ |
| occurred_at | datetime(tz) | when trade happened |
| notes | str, nullable | |
| realized_gain_loss | Decimal(18,2), nullable | sell orders only |
| avg_cost_at_sale | Decimal(18,6), nullable | holding avg_cost at time of sell |
| source_type | str(32) | manual / csv_import / voice_agent |
| source_ref | str, nullable | |
| source_import_id | int FK → import_batches, nullable | cascade delete |
| created_at | datetime(tz) | |
| updated_at | datetime(tz) | |

**Indexes:** (workspace_id, symbol, account_id), (workspace_id, occurred_at), (workspace_id, source_import_id)

## CashBalance Changes

Add two nullable columns to `investing_cash_balances`:
- `trigger_type` str(20): `manual` | `transfer` | `order`
- `trigger_ref` UUID, nullable: public_id of the triggering record

Existing rows get NULL (treated as manual/legacy).

## avg_cost Computation (Weighted Average)

**Buy:** `new_avg_cost = (existing_qty × existing_avg_cost + buy_qty × buy_price) / (existing_qty + buy_qty)`
**Sell:** avg_cost unchanged; only quantity decreases.
**Realized gain/loss:** `sell_qty × (sell_price - avg_cost_at_time_of_sale)`

## Edge Cases

| Scenario | Behavior |
|---|---|
| First buy of new symbol | Create Holding |
| Buy more of existing | Weighted avg_cost update |
| Sell more than owned | Reject 422 |
| Sell all shares | quantity = 0, holding NOT deleted |
| Buy again after selling all | fresh avg_cost from new price |
| Same symbol in different accounts | separate holdings |
| Delete buy order with subsequent sells | recompute; reject if sells exceed buys |

## New API Endpoints

```
POST   /investing/orders                  place_order (single buy/sell)
GET    /investing/orders                  list (paginated, filterable)
GET    /investing/orders/{id}             single order
DELETE /investing/orders/{id}             delete + recompute holding
POST   /investing/orders/bulk             bulk import from CSV data
GET    /investing/orders/by-holding/{symbol}  trade history for symbol
```

## CapitalTransfer Integration

When `CapitalTransferService.create_transfer()` is called with `to_module = 'investing'`, after creating the transfer, auto-create a `CashBalance` record for the target brokerage account with `trigger_type = 'transfer'` and `trigger_ref = transfer.public_id`.

## Frontend Changes

- New **Orders** tab on InvestingPage (Holdings | Orders | Cash Balances | Analytics)
- Place Order modal with computed gross/fees/net preview
- Cash Balances tab shows trigger_type badge
- Holdings tab shows "Trade History" link per row

## Files Changed

**Backend:**
- `app/investing/models.py` — add `OrderType` enum, `InvestingOrder`, columns on `CashBalance`
- `app/investing/schemas.py` — add order schemas, update `CashBalanceResponse`
- `app/investing/repository.py` — add `InvestingOrderRepository`, `get_latest_for_account_currency`
- `app/investing/service.py` — add `InvestingOrderService`
- `app/investing/router.py` — add 6 order endpoints
- `app/finance/service.py` — `CapitalTransferService.create_transfer` integration
- `app/core/dependencies.py` — DI wiring for new service
- `alembic/versions/0033_add_investing_orders.py` — migration

**Frontend:**
- `src/services/investing.ts` — order types and service methods
- `src/pages/InvestingPage.tsx` — Orders tab and UI
