# Spec-044: FIFO Lot-Based Cost Basis

**Created:** 2026-06-30
**Status:** Implemented (`2b4adb0`, merged 2026-06-30)
**Depends on:** spec-041 (Transaction-Based Investing Orders)

---

## Problem

Lifestack computes `Holding.avg_cost` with a **moving-average** model in two places: the inline incremental update in `place_order` (`app/investing/service.py:1871`, run on every new order) and the full chronological replay in `_replay_orders` (`app/investing/service.py:1709`, run by `delete_order`/`update_order`). Every buy blends into one running average across the entire order history, and a sell only decrements `quantity` — `avg_cost` is left as-is except that it resets to 0 when the position closes fully.

Real brokers (verified against Groww production data, 2026-06-30) report cost basis using **FIFO lot consumption**: a sell is matched against the *oldest* open buy lot(s) first, and the remaining position's cost basis reflects only the lots that haven't been consumed yet.

### Confirmed discrepancy (Bandhan ELSS Tax Saver Fund, account `Groww`)

Order history (all 3 buys + 1 sell, qty/price from `investing_orders`):

| Date | Type | Qty | Price |
|---|---|---|---|
| 2022-08-18 | buy | 180.573 | 110.75 |
| 2022-10-14 | buy | 119.528 | 108.76 |
| 2023-12-12 | buy | 140.319 | 142.53 |
| 2025-12-19 | sell | 300.101 | 182.30 |

300.101 (sold) = 180.573 + 119.528 exactly — the sell fully consumed the first two lots under FIFO, leaving only the third lot open.

| | Lifestack (moving-avg) | Groww (FIFO) |
|---|---|---|
| Remaining qty | 140.319 | 140.319 |
| Avg cost / NAV | ₹120.33 (blended across all 3 buys) | ₹142.53 (cost of the one open lot) |
| Invested / book value | ₹16,884.59 | ₹19,999.00 |
| Unrealized gain | ₹8,087.71 (47.90%) | ₹4,973.00 (24.87%) |

Quantity and current value match (they don't depend on cost-accounting method); cost basis and gain do not.

This matters beyond display accuracy: **India SEBI/IT capital-gains rules require FIFO** for redemption cost-basis. Lifestack's realized-gain-loss figures recorded on sell orders (`avg_cost_at_sale`, `realized_gain_loss`) are not just cosmetically off — they would be wrong inputs for tax reporting.

### Regulatory basis (why FIFO, not a configurable method)

FIFO isn't a Groww-specific display choice — it's mandated by Indian tax law for any securities held in dematerialized form, confirmed across multiple account types relevant to this app:

- **Section 45(2A), Income-tax Act 1961** — cost of acquisition and holding period for demat-held securities must be determined FIFO.
- **CBDT Circular 768 (24 June 1998)** — FIFO is applied **per demat/brokerage account**, not pooled across all accounts holding the same symbol. This matches `Holding`'s existing `(workspace_id, symbol, account_id)` uniqueness — lots in this spec are scoped the same way, which is why no cross-account lot matching is needed.
- **Mutual funds** (e.g. the Bandhan ELSS case above) follow the same FIFO default for redemptions.
- **US stocks bought via Indian platforms** (INDmoney, Vested, Paasa, etc.) remain subject to the same Section 45(2A) principle once routed through LRS and reported on an Indian ITR (Schedule FA) — INDmoney's own published methodology confirms FIFO for US-stock capital gains, with worked examples matching the algorithm below. Paasa similarly advertises "cost basis reconciliation" for Schedule FA filing, consistent with the same compliance requirement.

Net effect: every brokerage account type this app needs to support (Indian equities, Indian mutual funds, US stocks via LRS) is bound by the same FIFO rule. There's no broker or asset class in scope that legitimately requires a different method, so a configurable cost-basis method is explicitly not needed (see Out of scope).

## Solution

Replace the moving-average replay in `_replay_orders` with FIFO lot tracking. A holding's avg_cost becomes a derived view over its open lots, not a single stored running average.

**Single write path.** Today `place_order` keeps its own inline moving-average computation (`service.py:1871–1897` for the buy holding update; `service.py:1835–1836` for sell `realized_gain_loss`/`avg_cost_at_sale`) and never calls `_replay_orders`/`_recompute_holding_from_orders`. To avoid maintaining two cost-basis implementations, `place_order` will delete that inline logic and, after persisting the new order, call `_recompute_holding_from_orders` — the same full-replay path used by `delete_order`/`update_order`. All three write paths then derive lots, realized G/L, and avg_cost from the one FIFO replay. (Full replay on every order is acceptable: order counts per holding are small and this already happens on edit/delete.)

> Note on precision: `Holding.avg_cost` is `Numeric(12, 2)` — it stores 2 decimals. Lot `cost_per_unit` and the intermediate `final_avg_cost` are carried at higher precision (below), but the value persisted to `Holding.avg_cost` is still rounded to 2dp, unchanged from today.

### New model: `investing_order_lots`

Tracks the open/consumed state of each buy order as a queue of lots.

| Column | Type | Notes |
|---|---|---|
| id | PK | internal |
| workspace_id | FK → workspaces | |
| holding_id | FK → investing_holdings | |
| buy_order_id | FK → investing_orders | the buy this lot originated from |
| original_quantity | Decimal(18,8) | qty at buy time (immutable) |
| remaining_quantity | Decimal(18,8) | decremented as sells consume the lot |
| cost_per_unit | Decimal(18,6) | price_per_unit of the originating buy |
| acquired_at | datetime(tz) | = buy order's `occurred_at`, used for FIFO ordering |

**Indexes:** (workspace_id, holding_id, acquired_at) — drives FIFO consumption order.

### New model: `investing_lot_consumptions`

Records which lots a given sell order drew from, and how much — needed for audit trail and to reverse/replay correctly on order edit/delete.

| Column | Type | Notes |
|---|---|---|
| id | PK | internal |
| sell_order_id | FK → investing_orders | |
| lot_id | FK → investing_order_lots | |
| quantity_consumed | Decimal(18,8) | |
| cost_per_unit | Decimal(18,6) | copied from lot at consumption time (immutable record) |

### Replay algorithm (`_replay_orders` rewrite)

Replace the single `(qty, avg)` accumulator with a FIFO deque of `(remaining_qty, cost_per_unit, order_id)` tuples, ordered by `occurred_at`. (Pseudocode uses `order.buy`/`order.price_per_unit` for brevity; the real discriminator is `order.order_type == "buy"`.)

```
lots: deque[Lot] = []
for order in orders_sorted_by_occurred_at:
    if order.buy:
        lots.append(Lot(remaining=order.quantity, cost=order.price_per_unit, order_id=order.id))
    else:  # sell
        to_consume = order.quantity
        realized = Decimal("0")
        consumptions = []
        while to_consume > 0:
            if not lots:
                raise ValidationError("sell exceeds shares held")
            lot = lots[0]
            take = min(lot.remaining, to_consume)
            realized += take * (order.price_per_unit - lot.cost)
            consumptions.append((lot, take))
            lot.remaining -= take
            to_consume -= take
            if lot.remaining == 0:
                lots.popleft()
        order.realized_gain_loss = realized.quantize(MONEY_QUANT)
        order.avg_cost_at_sale = (
            sum(take * lot.cost for lot, take in consumptions) / order.quantity
        ).quantize(AVG_COST_PRECISION)  # weighted cost of the consumed lots, for display only
        persist consumptions to investing_lot_consumptions

final_qty = sum(lot.remaining for lot in lots)
final_avg_cost = (
    sum(lot.remaining * lot.cost for lot in lots) / final_qty if final_qty > 0 else Decimal("0")
)  # Holding.avg_cost = average cost of OPEN lots only, matching Groww's display
```

`Holding.avg_cost` keeps its existing meaning to callers (average cost of the *current* position) but is now computed from open lots instead of a blended running average — this is what makes it match Groww.

### Edge cases

| Scenario | Behavior |
|---|---|
| Sell consumes exactly one lot fully | Lot removed from queue, matches today's "qty=0 → avg=0" reset only if *all* lots empty |
| Sell spans multiple lots | `realized_gain_loss` is the sum across consumed lots; `avg_cost_at_sale` is the quantity-weighted cost of just the lots consumed |
| Edit a buy order's quantity/price | Full recompute: drop all lots/consumptions for the holding, replay every order from scratch (same pattern as today's `_recompute_holding_from_orders`) |
| Delete a buy order with dependent sells | Reject if remaining buys can't cover existing sells (same check as today, now enforced per-lot during replay rather than against an aggregate) |
| Backdating an order across existing lots | Re-sort by `occurred_at`, full replay — already done in `update_order`, unaffected by this change |

### Migration

- Alembic migration creates `investing_order_lots` and `investing_lot_consumptions`, both FK-cascade-deleted from their parent order.
- Backfill: for every existing `(workspace_id, symbol, account_id)` holding, replay its full order history with the new FIFO algorithm and populate lots/consumptions retroactively. This will silently change `avg_cost` and historical `realized_gain_loss`/`avg_cost_at_sale` on existing sell orders — call this out explicitly to the user before running in production, since it rewrites figures that may already appear in exported reports.
- No schema change to `investing_holdings` or `investing_orders` — `avg_cost` column stays, just sourced differently.

## Out of scope

- Configurable cost-basis method (FIFO vs LIFO vs moving-average) — FIFO only, matching the regulatory default. A method toggle can be a later spec if a use case appears.
- Wash-sale / short-term vs long-term capital gains classification — lot `acquired_at` is captured (needed for a future LTCG/STCG spec) but no holding-period logic is added here.

## Files Changed

**Backend:**
- `app/investing/models.py` — add `OrderLot`, `LotConsumption`
- `app/investing/repository.py` — add lot/consumption CRUD, FIFO-ordered lot fetch
- `app/investing/service.py` — rewrite `_replay_orders` (FIFO + persist lots/consumptions); route `place_order` through `_recompute_holding_from_orders` and delete its inline moving-average buy/sell logic (`service.py:1835–1836`, `1871–1897`); `delete_order`/`update_order` already call `_recompute_holding_from_orders` and need no caller change beyond the new replay behavior
- `alembic/versions/00XX_add_investing_order_lots.py` — new tables + backfill migration

**Frontend:** none required — `avg_cost`/`book_value`/realized G/L fields are already surfaced from existing endpoints; only their computed values change.
