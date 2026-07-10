# Spec-046: Investing Cost-Basis Accuracy — Fee Capitalization and Book-Value Precision

**Created:** 2026-06-30
**Status:** Implemented (`18c341d`, merged 2026-06-30)
**Depends on:** spec-044 (FIFO Lot-Based Cost Basis), spec-041 (Transaction-Based Investing Orders)

---

## Problem

Two independent defects make a holding's **book value (invested amount)** disagree with what the broker reports, both traced to how cost basis is derived in `InvestingOrderService._replay_orders` (`app/investing/service.py`).

### 1. Brokerage/fees are excluded from cost basis

`_replay_orders` builds each FIFO buy lot with `cost_per_unit = order.price_per_unit` — the raw trade price, with `brokerage_fee`/`tax_amount`/`other_fees` ignored. Cash already moves by the fee-inclusive `net_amount` (`service.py` `place_order`, `net_amount = gross ± fees`), but the holding's `avg_cost`/`book_value` does not. Standard practice — and Indian tax cost-of-acquisition (Section 48, Income-tax Act 1961) — **capitalizes** buy-side transaction costs into the asset's cost and **nets** sell-side costs from sale proceeds.

**Confirmed against IND Money (DriveWealth) production data, 2026-06-30:**

| Symbol | Broker invested | Broker avg | Lifestack invested (gross only) | Lifestack avg | Gap |
|---|---|---|---|---|---|
| GOOGL | ₹/$957.63 | $174.97 | $955.49 | $174.58 | $2.14 = exactly the 4 buy brokerage fees ($0.46+$0.34+$0.90+$0.44) |
| NVDA | $1,141.42 | $106.29 | $1,138.48 | — | $2.94 ≈ buy brokerage ($0.01+$0.01+$0.36+$0.32+$1.18+$1.05 = $2.93) |

The broker's formula is `invested = Σ(price×qty) + buy_fees`, `avg = invested / qty`. Across the IND Money account the total gap equals the total brokerage (~$44). Imported **GROWW** orders carry $0 fees, so they are already gross-only and unaffected until fees are backfilled — a separate data task, not part of this spec.

### 2. `Holding.avg_cost` precision loss inflates book value for low-priced, high-quantity holdings

`Holding.avg_cost` is `Numeric(12, 2)` — 2 decimal places — while the FIFO lot `cost_per_unit` is `Numeric(18, 6)` and `_replay_orders` already computes `final_avg_cost` quantized to `AVG_COST_PRECISION` (6 dp). The 6-dp value is **truncated to 2 dp** when persisted to the holding, and `book_value = quantity × avg_cost` (`app/investing/router.py`) then recomputes from the rounded figure.

For a stock priced in the hundreds this is invisible. For a **low-NAV mutual fund with thousands of units** it is not.

**Confirmed against IND Money Shahma production data, 2026-06-30** (one MF holding, single buy, zero fees):

| | Value |
|---|---|
| Quantity | 8,924.397 units |
| True NAV (order Net ÷ qty) | ₹9.0758 |
| Order Net (precise) | ₹80,995.94 |
| Stored avg_cost (2 dp) | ₹9.08 |
| Book value shown (qty × 9.08) | ₹81,033.52 |
| **Discrepancy** | **₹37.58** |

The ₹0.0042/unit rounding compounds over 8,924 units. The existing replay already produces the correct 6-dp avg_cost (₹9.075790); the `Numeric(12, 2)` column throws the precision away. Existing unit tests already assert 6-dp avg_cost (e.g. `avg_cost == Decimal("150.000000")`), confirming the service contract is 6 dp and the column is the outlier.

## Solution

### Part A — capitalize fees into FIFO lot cost (`_replay_orders`)

- **Buy lot cost** becomes fee-inclusive per unit:
  `cost_per_unit = price_per_unit + (brokerage_fee + tax_amount + other_fees) / quantity`,
  quantized to `AVG_COST_PRECISION` (6 dp). This flows through every existing consumer for free: `avg_cost`, `book_value`, FIFO `realized_gain_loss`, `avg_cost_at_sale`, and the persisted `OrderLot.cost_per_unit` / `LotConsumption.cost_per_unit`. A partially-consumed lot keeps a per-unit share of its fee automatically.
- **Sell fees** net from realized gain: after FIFO consumption,
  `realized_gain_loss = Σ take×(sell_price − lot_cost) − (brokerage_fee + tax_amount + other_fees)`.
  `avg_cost_at_sale` stays the fee-inclusive **cost** of the units sold (sell-side fees reduce gain, they do not raise the cost of what was sold).

No change to cash movement — `net_amount` already includes fees on both sides.

### Part B — preserve book-value precision

- Widen `Holding.avg_cost` from `Numeric(12, 2)` to `Numeric(18, 6)`, matching `OrderLot.cost_per_unit`, `avg_cost_at_sale`, and the 6-dp value `_replay_orders` already emits. `book_value = quantity × avg_cost` then carries through to ~5 dp of rupee accuracy (residual < ₹0.05 vs the ₹37.58 error today).
- No service-code change is required for Part B — the service already computes 6 dp; only the column width and the manual-entry schema bound change.

### Migration (0036)

1. `alter_column investing_holdings.avg_cost` → `Numeric(18, 6)`.
2. Clear and re-derive lots: `DELETE` from `investing_lot_consumptions` and `investing_order_lots`, then re-run the FIFO backfill (standalone re-implementation, per the migration-must-not-import-app-code rule established in 0035) **with the Part A effective-cost and sell-fee-netting rules**, rewriting `investing_order_lots.cost_per_unit`, `investing_lot_consumptions.cost_per_unit`, `investing_holdings.avg_cost`, and each sell order's `realized_gain_loss` / `avg_cost_at_sale`.

**Backfill note:** every holding with non-zero order fees, and every low-NAV holding, will have its `avg_cost`/`book_value` rewritten — by design, to match broker/tax reporting. Realized-gain figures on sell orders with fees also change. Historical exported numbers may differ after this revision.

## API / schema impact

- `HoldingResponse.avg_cost` is an unconstrained `Decimal` — now serializes up to 6 dp. Frontend formats to currency for display, so no UI change is required; more precision is strictly more accurate.
- `HoldingUpdate.avg_cost` bound relaxes from `decimal_places=2` to `decimal_places=6` so manual holdings can carry the same precision.
- No new endpoints. A separate brokerage/fees **display column** in the UI is explicitly **out of scope** (nice-to-have; book value already includes fees after this change).

## Out of scope

- Backfilling brokerage/STT onto GROWW (and other fee-less) imported orders — a data-import task.
- A configurable cost-basis method — FIFO is mandated (see spec-044); fees are capitalized unconditionally.
- Corporate actions / stock splits — separate roadmap item (Product Roadmap → Investing and Market Data).

## Test plan

- Buy with fees: `avg_cost` and `book_value` equal `(Σ price×qty + buy_fees) / qty`.
- Sell with fees: `realized_gain_loss` reduced by sell fees; `avg_cost_at_sale` is the fee-inclusive buy cost of consumed lots.
- Zero-fee orders: unchanged (all existing replay tests stay green).
- Precision: low-NAV high-qty single buy — `book_value` matches `net_amount` within < ₹0.05; assert avg_cost retains 6 dp.
- Partial sell with fees on both legs: remaining lot keeps proportional buy-fee share; realized nets the sell fee.
