# Spec 045 — Rename Symbol on Order-Derived Holdings

## Problem

`Holding.symbol` is the field used to fetch market prices (`_fetch_stock_price`, keyed off
`Holding.symbol` in `refresh_workspace_prices`, `app/investing/service.py:758`). When a holding
is entered incorrectly — most commonly a mutual fund where the wrong AMFI scheme code was typed
into an order — there is currently no way to fix it if the holding was derived from orders:

- The frontend hides the Edit Holding button whenever `h.source_type === 'order'`
  (`lifestack-web/src/pages/InvestingPage.tsx:1194`).
- Even if it were shown, the existing `PATCH /v1/investing/holdings/{id}` → `update_holding`
  (`app/investing/service.py:295`) only renames the `Holding` row. It does not touch the
  `InvestingOrder.symbol` rows that back it. Since `_recompute_holding_from_orders`
  (`app/investing/service.py:1826`) looks up/creates holdings by `(workspace_id, symbol,
  account_id)` derived from `InvestingOrder.symbol`, the next order mutation (create/update/
  delete) for that account would recompute a *new* holding under the old symbol, while the
  renamed holding is orphaned with stale `quantity`/`avg_cost` forever.
- Order-derived `quantity`/`avg_cost` are recomputed from orders on every order mutation
  (`service.py:1865-1866`), so letting the edit form also patch those fields is misleading for
  order-derived holdings — edits would be silently discarded next time an order changes.

## Goal

Let a user correct the symbol (and, incidentally, currency/instrument type) of an order-derived
holding, with the rename cascading atomically to every linked `InvestingOrder` row so the
holding/order link — and future recomputes — stay consistent. Quantity/avg_cost remain
read-only for order-derived holdings since orders are the source of truth for those fields.

## Non-Goals

- Editing `quantity`/`avg_cost` directly on order-derived holdings (edit the underlying orders
  instead).
- Symbol editing on individual `InvestingOrder` rows (`InvestingOrderUpdate` still has no
  `symbol` field) — renames only happen holding-wide, via the holding edit endpoint.
- Cross-account symbol moves. Rename is scoped to `(workspace_id, account_id)`; symbol + account
  + workspace remains the holding's unique key.

## API Changes

### `PATCH /v1/investing/holdings/{holding_id}` (existing endpoint, behavior change)

No schema change to `HoldingUpdate` — `symbol`, `currency`, `instrument_type` already exist
(`quantity`/`avg_cost` remain accepted for manual holdings only; see Service Changes).

**New behavior in `InvestingHoldingService.update_holding`** (`app/investing/service.py:295`):

After the existing duplicate-check / instrument-resolution block (lines 311-339), when
`symbol_changed` is true:

1. If `holding.source_type == "order"`:
   - Reject `quantity`/`avg_cost` in the request — return `422` with a message pointing the user
     to edit the underlying orders instead (mirrors the existing pattern of rejecting invalid
     input early, see `HoldingUpdate` validators).
   - Call a new repository method `order_repository.rename_symbol(workspace_id, account_id,
     old_symbol, new_symbol)` that bulk-updates `InvestingOrder.symbol` (and clears
     `instrument_id` so it's re-resolved lazily, matching existing order-creation behavior) for
     every order matching `(workspace_id, account_id, symbol=old_symbol)`, in the same DB
     transaction as the holding save.
   - This must run *before* the duplicate-key check against `Holding` so a 409 on the holding
     check still leaves orders untouched (i.e., validate before mutating).
2. If `holding.source_type != "order"` (manual holding): behavior unchanged, no order rows to
   touch.

Existing cached-price invalidation (`holding_price_repo.delete_for_holding`, line 342) is
unchanged — a renamed symbol means yesterday's cached price is for the wrong instrument.

**Error cases (new):**
- `422` — `quantity` or `avg_cost` present in the request body for a `source_type == "order"`
  holding.
- `409` — unchanged: target symbol already in use for another holding in the same
  `(workspace_id, account_id)`.

## Repository Changes

**`InvestingOrderRepository`** (`app/investing/repository.py:182`):
- `rename_symbol(workspace_id: int, account_id: int, old_symbol: str, new_symbol: str) -> int` —
  bulk `UPDATE investing_orders SET symbol = :new, instrument_id = NULL, updated_at = now() WHERE
  workspace_id = :wid AND account_id = :aid AND symbol = :old`. Returns rows affected (used in a
  test assertion, not returned to the API caller).

## Frontend Changes

**`InvestingPage.tsx`:**
- Remove the `h.source_type !== 'order'` guard at line 1194 — the Edit Holding button now shows
  for all holdings.
- In the Edit Holding modal (`lines 2076-2172`), when `selectedHolding.source_type === 'order'`:
  - Render `Quantity` and `Avg Cost` as read-only (`disabled` input, same styling as the
    read-only `Account` field at line 2128-2133) with helper text: "Computed from orders — edit
    the order history to change this."
  - Keep `Symbol`, `Asset Type`, `Currency` editable as today.
  - Omit `quantity`/`avg_cost` from the `onUpdateHolding` payload for order-derived holdings so
    the request matches what the backend now rejects.
- On `422` (the new reject case) or `409`, surface the API error message in the existing modal
  error banner (reuse whatever pattern `onUpdateHolding`'s mutation error handler already uses
  for other holding edit failures).
- After a successful rename, invalidate `['holdings']` *and* `['investing', 'orders']` /
  trade-history queries (whatever query keys back the Trade History view for that holding), since
  order rows changed too.

## Testing

**Backend (integration):**
1. Rename symbol on an order-derived holding with 3 linked orders → holding renamed, all 3
   orders' `symbol` updated, `instrument_id` cleared on the orders.
2. Rename to a symbol that collides with an existing holding in the same account → `409`, orders
   untouched.
3. Attempt to also patch `quantity`/`avg_cost` on an order-derived holding rename → `422`, nothing
   persisted.
4. Rename on a manual (`source_type == "manual"`) holding → unchanged existing behavior, no order
   rows touched, `quantity`/`avg_cost` still editable.
5. After rename, create a new order for the holding (using the new symbol) → recompute finds the
   renamed holding (no orphan/duplicate holding created).

**Frontend (Playwright / mock):**
1. Edit button now visible for an order-sourced holding row.
2. Edit modal shows Quantity/Avg Cost as disabled for an order-sourced holding, enabled for a
   manual one.
3. Submitting a symbol rename calls `PATCH` without `quantity`/`avg_cost` for an order-sourced
   holding.
4. `409`/`422` error surfaces in the modal.

## Migration

No schema changes required.

## Rollout

Single PR per repo: `feat/order-holding-symbol-rename`. Backend first, then frontend.
