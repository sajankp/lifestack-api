# Spec-051: Corporate Actions (Splits, Reverse Splits, Bonus Issues)

**Created:** 2026-07-03
**Status:** Approved (implementation)
**Depends on:** spec-044 (FIFO lot-based cost basis), spec-046 (fee-inclusive buy cost)

---

## Problem

Holding quantity and FIFO cost basis are replayed entirely from `investing_orders`
(`InvestingOrderService._replay_orders`, `app/investing/order_service.py`). Nothing in that
replay accounts for a corporate action. When a symbol splits, reverse-splits, or issues
bonus shares after a buy was recorded, every number derived from that buy silently drifts
from the broker's:

- **Quantity understates** the true post-action share count (splits, bonus issues) or
  overstates it (reverse splits).
- **FIFO lot cost basis** (`OrderLot.cost_per_unit`) stays at the pre-action price, so
  unrealized gain is wrong per share.
- **A sell placed for the true post-action share count can fail outright** (splits/bonus)
  or under-consume lots (reverse splits), because the negative-holding guard in
  `_replay_orders` still thinks the position is the pre-action quantity.

### Confirmed discrepancy (worked example, 2:1 split)

| Date | Event | Qty | Price |
|---|---|---|---|
| 2026-01-15 | buy | 10 | ₹1,000.00 |
| 2026-03-01 | *(real-world split, 2 new shares per 1 old — not recorded in Lifestack)* | | |
| 2026-06-01 | sell attempt | 20 | ₹600.00 |

Today, the sell attempt above raises `ValidationError: "This order would result in a
negative holding"` — Lifestack still believes the holding is 10 shares, but the broker
statement shows 20 (post-split) and the sell is valid. The only workaround today is to
hand-edit the original buy order to `quantity=20, price_per_unit=500.00`, which destroys
the original transaction record — the price the user actually paid, and the order's audit
trail, are gone.

This is a live, imported-data problem: NVDA's 10:1 split (2024) and GOOGL's 20:1 split
(2022) both appear un-applied in IND Money CSV imports, per
`docs/product/PRODUCT_STRATEGY_AND_ROADMAP.md`.

### Why all three actions are one spec, not three

Splits, reverse splits, and bonus issues share one table, one set of endpoints, and
(eventually) one `lifestack-web` entry form — a user recording a corporate action picks a
type from one dropdown, not three different features. Splitting the schema/API across
separate specs would mean three migrations and, later, three UI PRs for what is one entry
point. They are combined here for that reason, **not** because the underlying mechanics are
identical — they aren't (see below) — the replay algorithm has two genuinely different code
paths sharing one event-stream integration point.

### Splits and reverse splits are the same mechanism; bonus issues are not

Under Indian tax law, a **stock split (or reverse split)** is not a new acquisition — the
original acquisition date and total cost of the existing shares carry forward pro-rata to
the new share count. This is standard practice (see any broker's console/statement
handling): you still hold "the same investment," just subdivided (or consolidated)
differently.

A **bonus issue is a distinct, separate acquisition**: the bonus shares get a cost of
acquisition of **nil** and a holding period starting from the bonus **allotment date**
(Section 55(2)(aa)(iiia) / Explanation 1(i)(h) to Section 2(42A), Income-tax Act 1961) — the
*original* shares' cost and date are untouched. Modeling a bonus issue correctly means
creating a **new zero-cost lot dated at allotment**, computed off the total quantity held
at that point — not scaling every existing lot the way a split does.

Net effect: a split/reverse-split is a **per-lot scaling transform** (existing lots, same
identity, same `acquired_at`, new qty/cost). A bonus issue is a **new zero-cost lot**
(new identity, new `acquired_at`, existing lots untouched). Same event-stream slot, two
different transforms.

## Solution

Add a first-class **corporate action** event, replayed inside `_replay_orders` alongside
buy/sell orders, chronologically ordered by its ex-date. This fits the existing
architecture directly — the app already derives `Holding.quantity`/`avg_cost` and every
`OrderLot` by chronological replay, so a corporate action is just another event type in
that same stream, not a new mechanism.

**The original `InvestingOrder` buy record is never touched.** Only the *derived* FIFO
lots (`OrderLot`, already fully recomputed on every replay per its own docstring — see
`app/investing/models.py`) are adjusted or added to. This is the key improvement over the
manual-edit workaround: the user's real purchase price and the audit trail survive.

### New model: `investing_corporate_actions`

| Column | Type | Notes |
|---|---|---|
| id | PK | internal |
| public_id | UUID | external identifier, matches other investing entities |
| workspace_id | FK → workspaces | |
| user_id | FK → users | who recorded it |
| account_id | FK → accounts (composite with workspace_id, like `investing_orders`) | brokerage account this action applies to |
| symbol | varchar(20) | matches `InvestingOrder.symbol` / `Holding.symbol` |
| action_type | enum: `"split"`, `"bonus"` | reverse splits are `"split"` with `ratio_base > ratio_quote` — same transform, just direction |
| ratio_base | Decimal(12,4) | units *held* per unit of entitlement: `1` in a 10-for-1 split, `10` in a 1-for-10 reverse split, `2` in a "1-for-2" bonus |
| ratio_quote | Decimal(12,4) | units *received* per `ratio_base` units held: `10` in a 10-for-1 split, `1` in a 1-for-10 reverse split or a "1-for-2" bonus |
| ex_date | date | effective date; applied before any order occurring on this date (see ordering rule below) |
| notes | varchar(255), nullable | free text |
| created_at / updated_at | timestamptz | |

**Constraints:** composite FK `(account_id, workspace_id)` → `accounts(id, workspace_id)`
(matches `investing_orders`); `UNIQUE(workspace_id, account_id, symbol, ex_date, action_type)`
(a symbol can in principle have both a split and a bonus issue on the same date — rare, but
not modeling-invalid); index on `(workspace_id, symbol, account_id)` for replay lookups;
both `ratio_base`/`ratio_quote` `> 0` (schema-level, enforced in `CorporateActionCreate` too).

Storing `ratio_base`/`ratio_quote` as a pair (rather than a single ratio) matches how both
splits and bonus issues are actually announced ("10-for-1", "1-for-2 bonus") and keeps the
replay math as exact Decimal multiplication/division rather than a pre-divided float-ish
ratio. The names read identically for both action types — "per `ratio_base` units held,
`ratio_quote` units received" — with one nuance carried by `action_type`, not by the
fields: a split's received units *replace* the held units, while a bonus's are
*additional*. Called out in the schema docstring so this isn't a trap for whoever edits
this next.

### Schema change: `OrderLot.buy_order_id` becomes optional

A bonus-issue lot doesn't originate from a buy order, so `OrderLot` needs a way to record
provenance from a corporate action instead:

- `buy_order_id: int | None` (was required) — FK to `investing_orders`, `ondelete=CASCADE`, now nullable.
- New `corporate_action_id: int | None` — FK to `investing_corporate_actions`, `ondelete=CASCADE`, nullable.
- New `CHECK` constraint: exactly one of `buy_order_id`/`corporate_action_id` is set.

Split/reverse-split events don't create new lots (they scale existing ones), so this only
matters for bonus-issue lots. Existing rows all have `buy_order_id` set and
`corporate_action_id` null, satisfying the check without a data migration beyond the schema
change itself.

### Replay algorithm (`_replay_orders` in `app/investing/order_service.py`)

`_replay_orders` gains a second input, merged into one chronological event stream:

```
events = sorted(
    [(order.occurred_at, 1, order) for order in orders]
    + [(datetime.combine(action.ex_date, time.min, UTC), 0, action) for action in corporate_actions],
    key=lambda e: (e[0], e[1], e[2].id or 0),
)
```

The `0`/`1` tiebreak makes a corporate action apply *before* any order recorded on the same
calendar date — splits/bonus allotments take effect before market open, so same-day trades
already see the post-action price/quantity. This is a deliberate ordering choice, called
out explicitly since it's not derivable from the data alone.

**Split / reverse split** — scale every lot tracked so far (`all_lots`), preserving each
lot's identity and `acquired_at`:

```
elif action.action_type == "split":
    factor_qty = action.ratio_quote / action.ratio_base     # e.g. 10/1 = 10 (split), 1/10 = 0.1 (reverse split)
    factor_cost = action.ratio_base / action.ratio_quote    # inverse
    for lot in all_lots.values():
        lot.original_quantity *= factor_qty
        lot.remaining *= factor_qty
        lot.cost_per_unit = (lot.cost_per_unit * factor_cost).quantize(AVG_COST_PRECISION)
```

Total cost per lot (`remaining * cost_per_unit`) is invariant under this transformation —
the action changes share count and per-share price, never the money value of the position.
All lots are scaled uniformly, including fully- and partially-consumed ones, because
`cost_per_unit` is a single per-lot value shared by the consumed and open portions — it
cannot be scaled for one portion and not the other. **Consumption records already persisted
for sells that happened before the action are never touched** — those sells genuinely
happened at the pre-action share count and price, exactly as the broker recorded them, and
each `LotConsumption` row carries its own `cost_per_unit`, so historical
`realized_gain_loss`/`avg_cost_at_sale` values are unaffected.

One invariant is knowingly broken by this choice: for a lot **partially consumed before the
action**, `Σ(consumptions.quantity_consumed) + remaining_quantity ≠ original_quantity`
afterwards — e.g. buy 10, sell 4, then a 2:1 split leaves `original_quantity=20`,
`remaining_quantity=12`, and consumption rows totalling 4 (pre-split units). Nothing in the
codebase relies on that identity today (`OrderLot` is internal and never returned by any
endpoint), but any future diagnostic that cross-checks lots against their consumptions must
either normalize consumption quantities to post-action units or skip lots that straddle a
corporate action.

**Bonus issue** — compute the bonus quantity off the *total currently open* quantity, and
add one new zero-cost lot:

```
elif action.action_type == "bonus":
    held_qty = sum(lot.remaining for lot in queue)          # currently open, not all_lots
    bonus_qty = (held_qty * action.ratio_quote / action.ratio_base).quantize(LOT_QTY_PRECISION)
    if bonus_qty > 0:
        bonus_lot = _OpenLot(
            buy_order_id=None,
            corporate_action_id=action.id,
            original_quantity=bonus_qty,
            remaining=bonus_qty,
            cost_per_unit=Decimal("0"),
            acquired_at=datetime.combine(action.ex_date, time.min, UTC),
        )
        queue.append(bonus_lot)
        all_lots[f"bonus:{action.id}"] = bonus_lot          # all_lots keyed by buy_order_id today; bonus lots need a distinct key
```

A zero-cost lot is not a special case in the FIFO consumer — a later sell against it simply
realizes the full sale proceeds as gain (`take * (sell_price - 0)`), which is exactly the
nil-cost-basis tax treatment. `all_lots`'s key type changes from `int` (buy_order_id) to a
small tagged union / `str` key to accommodate bonus lots that have no `buy_order_id`; this
is an internal implementation detail with no external effect.

`LOT_QTY_PRECISION` is a **new** constant in `order_service.py` (it does not exist today —
only `AVG_COST_PRECISION` and `MONEY_QUANT` do): `Decimal("0.00000001")`, matching the
`Numeric(18, 8)` scale of every quantity column (`InvestingOrder.quantity`,
`OrderLot.original_quantity`/`remaining_quantity`).

Real-world bonus issues allot whole shares, with fractional entitlements paid out as cash
in lieu; this spec deliberately keeps the fractional quantity (correct for mutual funds,
and simpler). Where a broker pays cash for a fraction, that cash is an unmodelled event —
the user closes it via the existing manual snapshot edit, the sanctioned mechanism for
exactly this class of flow (dividends, interest, cash-in-lieu).

`_recompute_holding_from_orders` (renamed `_recompute_holding` since it now also depends on
corporate actions) fetches both `order_repository.list_by_holding(...)` and the new
`corporate_action_repository.list_by_holding(...)` before calling `_replay_orders`. Because
`OrderLot`/`LotConsumption` rows are already deleted-and-recreated wholesale on every
replay, a corporate action write needs no special-case persistence logic — creating or
deleting a `CorporateAction` row and re-running the existing recompute path is sufficient
and automatically idempotent (delete + re-add reproduces identical state, since replay has
no hidden accumulator outside the DB rows it reads).

### Snapshot / cash neutrality

`create_corporate_action` / `delete_corporate_action` call `_recompute_holding` and
**nothing else** — no call to `_update_cash_balance`, no new `investing_cash_balances` row.
None of split, reverse split, or bonus issue moves cash; this is enforced by construction
(the method simply has no cash-balance dependency), and the golden tests below assert the
cash-balance row count for the account is unchanged before/after each action type.

### Worked examples

**Split (2:1, from the Problem section):**

| | Pre-fix (today, wrong) | Post-fix |
|---|---|---|
| Lot after buy (10 @ ₹1,000) | remaining=10, cost=1,000.00 | remaining=10, cost=1,000.00 |
| Lot after split (`ratio_base=1, ratio_quote=2`) | *(no split recorded)* | remaining=20, cost=500.000000 |
| Sell 20 @ ₹600 | **rejected** — exceeds recorded 10-share holding | accepted; consumes the one lot fully |
| `realized_gain_loss` | n/a | `20 × (600 − 500) = 2,000.00` |
| Resulting holding | n/a (order rejected) | qty=0, avg_cost=0 (lot fully closed) |

This matches exactly what the manual-edit workaround would have produced (rewriting the buy
to `qty=20, price=500`) — proving the fix is equivalent in output, without destroying the
original order.

**Bonus issue (1-for-2, i.e. `ratio_base=2, ratio_quote=1`):**

| | Value |
|---|---|
| Lot after buy (10 @ ₹1,000) | remaining=10, cost=1,000.00, `acquired_at`=buy date |
| Bonus event (held=10, 1-for-2) | new lot: remaining=5, cost=0.00, `acquired_at`=ex_date |
| Sell 12 @ ₹800 (FIFO: 10 from original lot, 2 from bonus lot) | realized = `10×(800−1000) + 2×(800−0)` = `−2,000 + 1,600` = `−400.00` |
| Resulting holding | qty=3 (of the bonus lot), avg_cost=0.00 |

The negative component from the original lot and the fully-taxable-gain component from the
bonus lot net out correctly in one `realized_gain_loss` figure, and the two lots' distinct
`acquired_at` dates are preserved for a future LTCG/STCG classification spec.

## Backend impact (`lifestack-api`)

- `app/investing/models.py`:
  - New `CorporateAction` model (`investing_corporate_actions` table).
  - `OrderLot.buy_order_id` becomes nullable; new nullable `corporate_action_id` FK; new
    `CHECK` constraint (exactly one of the two set).
- `app/investing/repository.py`:
  - New `CorporateActionRepository` — `create`, `get_by_public_id`,
    `list_by_holding(workspace_id, symbol, account_id)` (ordered by `ex_date`),
    `list_by_workspace` (paginated, for the list endpoint), `delete`.
  - `LotRepository.create_lots` unchanged in signature; `OrderLot` instances it receives may
    now have `buy_order_id=None, corporate_action_id=<id>` for bonus lots.
- `app/investing/order_service.py`:
  - `InvestingOrderService.__init__` gains `corporate_action_repository: CorporateActionRepository`.
  - `_replay_orders` gains a `corporate_actions: Sequence[CorporateAction] = ()` parameter and
    the merged-event-stream / two-branch logic above.
  - `_recompute_holding_from_orders` renamed `_recompute_holding`; fetches corporate actions
    alongside orders. All existing call sites (`place_order`, `delete_order`, `update_order`)
    updated for the rename only — no behavior change to the order-only paths.
  - New methods: `create_corporate_action`, `list_corporate_actions`, `delete_corporate_action`
    (each validates the account is a brokerage account, like `place_order`; create/delete both
    call `_recompute_holding` for the affected `(symbol, account_id)`).
- `app/investing/schemas.py`: `CorporateActionCreate`, `CorporateActionResponse` (mirroring
  `InvestingOrderCreate`/`Response` field-validator style — `symbol` normalized to uppercase,
  `ratio_base`/`ratio_quote` both `gt=0`; no cross-field direction constraint, since reverse
  splits need `ratio_base > ratio_quote` to be valid).
- `app/investing/router.py`: `POST /investing/corporate-actions`,
  `GET /investing/corporate-actions` (optional `symbol`/`account_id` filters),
  `DELETE /investing/corporate-actions/{public_id}`.
- `app/core/dependencies.py`: `get_investing_corporate_action_repo`; `get_investing_order_service`
  wires in the new repository.
- `alembic/versions/0037_add_investing_corporate_actions.py`: new table, plus the
  `OrderLot.buy_order_id` nullable change + `corporate_action_id` column + CHECK constraint,
  in the same migration. The `action_type` enum must be declared **inline** in
  `op.create_table` — no explicit `sa.Enum(...).create(...)` pre-create, which causes
  `DuplicateObjectError` in CI (follow migration 0010's pattern); downgrade drops the enum
  with `checkfirst=True`. No data backfill — existing un-split holdings stay as-is until a
  user records the action (see Out of scope).
- `docs/domain/cash-model-ledger-snapshots-reconciliation.md` §6: entry noting corporate
  actions are cash-neutral, lot-level-only adjustments (no snapshot/ledger interaction).

## API / schema impact

- `POST /investing/corporate-actions` — body `{account_id, symbol, action_type,
  ratio_base, ratio_quote, ex_date, notes?}` → `201` with `CorporateActionResponse`. Recomputes
  the affected holding synchronously (same request/response cycle as `place_order`).
- `GET /investing/corporate-actions?symbol=&account_id=` — paginated list, same shape as
  `GET /investing/orders`.
- `DELETE /investing/corporate-actions/{public_id}` — `204`; recomputes the affected holding.
- No changes to existing order/holding endpoints' request/response shapes. `OrderLot` rows
  are internal (never returned directly by any endpoint today), so no response schema needs
  a new field to expose action-adjusted lots — `Holding.quantity`/`avg_cost` already surface
  the adjusted numbers through the existing endpoints.

## Golden test scenarios (required before merge)

New `app/investing/tests/test_corporate_actions.py`, alongside `test_order_service.py`:

1. **Split** — buy 10 @ ₹1,000; record `split` (`ratio_base=1, ratio_quote=2`) after the buy;
   assert holding shows qty=20, avg_cost=500.000000; sell 20 @ ₹600 succeeds (today it
   raises `ValidationError`); assert `realized_gain_loss=2000.00`,
   `avg_cost_at_sale=500.000000`; assert cash-balance row count unchanged by the split
   itself (only buy/sell write rows); delete the corporate action and assert the holding
   reverts to the pre-action state (idempotent replay).
2. **Reverse split** — buy 100 @ ₹50; record `split` (`ratio_base=10, ratio_quote=1`); assert
   holding shows qty=10, avg_cost=500.000000 (same total cost, 1/10th the shares).
3. **Bonus issue** — buy 10 @ ₹1,000; record `bonus` (`ratio_base=2, ratio_quote=1`); assert
   two lots exist with distinct `acquired_at`; sell 12 @ ₹800 (crosses both lots via FIFO);
   assert `realized_gain_loss=-400.00` per the worked example above; assert cash-balance row
   count unchanged by the bonus event itself.
4. **Reconciliation neutrality (campaign G4 gate)** — fund the brokerage account via a
   capital transfer (not a manual cash-balance edit, which is an unmodelled event by design
   and would itself produce a discrepancy); buy; record the split; sell across the boundary;
   assert `GET /finance/accounts/{id}/reconciliation` returns `discrepancy == 0` — the
   corporate action must add no term to either side of the reconciliation identity.

## Out of scope

- **Automatic detection.** No attempt to auto-detect an un-applied corporate action from
  price discontinuities in this spec — that's explicitly called out as CAMS-CAS-import
  preview UX in Task 6 of `docs/AGENT-TASKS.md` at the **lifestack workspace root** (one
  level above this repo, not this repo's `docs/`), which depends on this spec being merged
  first.
- **Backfill / auto-migration of existing holdings.** No script retroactively finds and fixes
  already-imported un-split holdings (e.g. existing NVDA/GOOGL rows). The user records the
  corporate action manually per the new endpoint; this spec only removes the *need* to
  hand-edit the original order to do so.
- **Update endpoint for a recorded corporate action.** Delete + re-create covers correction
  of a fat-fingered entry and keeps the API surface small; `PATCH` can be added later if this
  proves inconvenient in practice.
- **UI.** This spec is backend-only (per Task 4 in the workspace-root `docs/AGENT-TASKS.md`);
  a `lifestack-web` form to record a corporate action is a separate, quick follow-up once this is merged —
  and now only needs to be built once for all three action types, which is the whole
  motivation for combining them here.
- **Cross-account corporate-action entry.** An action is entered per-account (matching how
  orders/lots are already scoped per `(workspace_id, symbol, account_id)` — see spec-044's
  CBDT Circular 768 citation on per-demat-account FIFO). A user holding the same symbol in
  two brokerage accounts must record the action twice, once per account. This mirrors the
  existing per-account lot scoping and is not a new limitation introduced by this spec.
- **Stock mergers/demergers, spin-offs, rights issues.** Materially different mechanics
  (new instrument involved, cash components, or subscription rights) — out of scope entirely,
  not just deferred; would need their own spec from scratch if ever prioritized.
