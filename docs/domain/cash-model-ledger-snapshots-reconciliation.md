# Cash Model: Ledger, Snapshots, Reconciliation & Net Worth

**Purpose:** the canonical reference for how money is represented across Spending
and Investing — so we don't have to re-read the code every time. Reverse-engineered
from the implementation; keep it in sync when the behaviour changes.

Code pointers are `file:line` at time of writing — verify before relying on them.

---

## 1. Two separate stores (this is the crux)

Money lives in **two independent systems**. Confusing them is the source of most
confusion:

| Store | What it is | Table(s) |
|---|---|---|
| **Ledger** | Append-only *events*: income/expense transactions and capital transfers | `spending_transactions`, `capital_transfers` |
| **Cash snapshots** | Point-in-time *balances*, one append-only series per **(account, currency)** | `investing_cash_balances` |

- The **ledger** is what the *projected* balance is computed from.
- **Snapshots** are the "known" cash balance; each new row = previous latest + a delta
  (`_update_cash_balance`, `investing/service.py:1781`). `_get_cash_balance` just reads
  the newest row for an (account, currency).
- Snapshots are **per currency**. Invariant we rely on: **a brokerage account holds a
  single currency.** (e.g. Groww = INR, IND Money = USD.)

## 2. What writes to each store

| Event | Ledger | Cash snapshot |
|---|---|---|
| Spend (income/expense) | ✅ income/expense row | ❌ never |
| Transfer **into** an investing account | ✅ transfer row | ✅ `+net_amount_received` in `to_currency`, trigger `transfer` (`finance/service.py`, `create_transfer`) |
| Transfer **out of** an investing account | ✅ transfer row | ✅ `−gross_amount` in `from_currency`, trigger `transfer` (spec-049; previously not decremented at all) |
| Transfer between two non-investing accounts | ✅ transfer row | ❌ never (neither side is snapshot-managed) |
| Order **buy** | ❌ orders aren't ledger rows | ✅ `−net` (net = gross + fees); validated vs available cash first (`investing/service.py:1967`, `:2039`) |
| Order **sell** | ❌ | ✅ `+net` (net = gross − fees) |

Key asymmetries to remember:
- **Spends never touch snapshots.** A wallet/bank account typically has *no* snapshot
  at all unless one is entered manually.
- **An investing-to-investing transfer writes two snapshot rows** (from-side decrement
  + to-side increment) sharing one `trigger_ref = transfer.public_id`. Any lookup of
  "the snapshot for this transfer" must be scoped by `(trigger_ref, account_id)` via
  `CashBalanceRepository.get_by_trigger_ref_and_account` — the older unscoped
  `get_by_trigger_ref` assumes exactly one row per transfer and breaks
  (`MultipleResultsFound`) once both sides are investing accounts.
- **Orders never touch the ledger**, but they *do* move the snapshot.
- **Transfer outflows** move the ledger but not the snapshot.

## 3. Reconciliation (`GET /finance/accounts/{id}/reconciliation`)

Per account, compares the ledger-projected balance against the latest snapshot.

```
projected_balance = (income − expense)                 # spending_transactions
                  + (transfer_in − transfer_out)        # net_received in, gross out
                  + (sell.net − buy.net)                # investing orders  ← added, see §6
snapshot_balance  = newest investing_cash_balances row for the account   # NO currency filter
discrepancy       = projected − snapshot   (None when no snapshot)
```

Code: `repository.py:get_reconciliation_summary` (~425) + `get_spending_balance` (~217).

Notes / caveats:
- `transfer_in` uses `net_amount_received` (destination currency, correct for the
  receiver); `transfer_out` uses `gross_amount` (source currency).
- The snapshot query has **no currency filter** — it takes the single newest row. This
  is only sound because of the one-currency-per-brokerage invariant. If an account ever
  held two currencies, both this and the unconverted projected sum would be wrong; that
  would require per-(account, currency) reconciliation.
- **Reconcile against the latest snapshot of *any* `trigger_type`** (manual, transfer,
  order) — the current behaviour. Since orders/transfers auto-write snapshots, "latest
  of any kind" already embeds their deltas; the discrepancy then surfaces genuinely
  unmodelled events (dividends, interest, untracked fees) that a user captures by
  editing the snapshot to match a statement.

## 4. Net worth (`GET /finance/net-worth`, `router.py:343`)

Net worth is a **mix of both stores**, split by account type:

| Piece | Source |
|---|---|
| Spending accounts (wallet/bank/card) | **ledger** (`get_spending_balances_bulk`) |
| Brokerage **cash** | **snapshots** (`investing_cash_total`, latest per account/currency) |
| Holdings | portfolio value (investing summary) |

Brokerage accounts are **excluded from the spending side** (`router.py:360`) so their
cash isn't double-counted (it already arrives via `investing_cash_total`).

Consequence: **brokerage net worth is already correct** — it reads the snapshot (which
orders/transfers update) plus holdings. Orders were only ever missing from the
*reconciliation projected* side, not from net worth.

**History (spec-065, 2026-07-08):** net worth is otherwise only ever computed for *now*.
`net_worth_snapshots` (one row per workspace per day, unique on `(workspace_id,
snapshot_date)`) materializes a daily series so a history graph doesn't need to replay
every order/price/FX rate per day — which is also partially impossible, since imported
(Demat/CAS) holdings have no order history to replay. Written two ways: opportunistically
on every `GET /finance/net-worth` for today, and by the daily `net_worth_snapshot` cron
job (07:00 UTC). Both paths share one computation (`NetWorthService._compute_net_worth`)
so they can't drift.

## 5. Worked example

Accounts: **ICICI** (wallet, INR), **Groww** (brokerage, INR), **IND Money**
(brokerage, USD). Reporting currency INR; $1 = ₹83.

| # | Event | Ledger | Snapshot |
|---|---|---|---|
| E1 | Salary +₹300,000 → ICICI | ICICI income +300,000 | — |
| E2 | Rent −₹20,000 (ICICI) | ICICI expense +20,000 | — |
| E3 | Transfer ICICI→Groww ₹50,000 | ICICI out +50,000; Groww in +50,000 | Groww-INR → ₹50,000 (`transfer`) |
| E4 | Buy Groww: gross ₹40,000 + ₹100 fee, net ₹40,100 | — | Groww-INR → ₹9,900 (`order`) |
| E5 | Transfer ICICI→IND Money: gross ₹83,000 → net $1,000 | ICICI out +83,000; IND Money in +1,000 | IND Money-USD → $1,000 (`transfer`) |
| E6 | Buy IND Money: net $600 | — | IND Money-USD → $400 (`order`) |

**Reconciliation:**
- ICICI: projected = 300,000 − 20,000 − (50,000 + 83,000) = **₹147,000**; no snapshot → discrepancy = *"No snapshot yet"*.
- Groww: projected = 50,000 (transfer in) − 40,100 (buy) = **₹9,900**; snapshot = ₹9,900 → **discrepancy ₹0**. *(Before the §6 fix, projected omitted the buy and showed a false +₹40,100.)*
- IND Money: projected = 1,000 − 600 = **$400**; snapshot = $400 → **discrepancy $0**.

**Net worth:**
- Spending (ICICI ledger): ₹147,000
- Brokerage cash (snapshots): Groww ₹9,900 + IND Money $400→₹33,200 = ₹43,100
- Holdings: Groww ₹40,000 + VOO $600→₹49,800 = ₹89,800
- **Total = ₹279,900** — equals ₹300,000 salary − ₹20,000 rent − ₹100 brokerage fee. ✔

## 6. Change log

- **2026-07-01 (spec-048):** Added the investing-order term (`sell.net − buy.net`) to
  the reconciliation projected balance and a new `order_count` field. Before this,
  brokerage reconciliation showed a false discrepancy equal to net trade flow, because
  orders moved the snapshot but not the projected side. Safe as a per-account
  unconverted sum given the one-currency-per-brokerage invariant. Net worth was and is
  unaffected (brokerage cash comes from snapshots).
  Deferred: per-(account, currency) reconciliation, needed only if an account ever holds
  multiple currencies.
- **2026-07-01:** Fixed a net-worth cash double-count. In the FX-converted investing
  summary path (`investing/service.py`, multi-currency workspace with a reporting
  currency), `portfolio_value` erroneously included cash (`converted_portfolio +
  converted_cash`) while the single-currency paths returned holdings-only. The net-worth
  router adds `cash_total` to `portfolio_value`, so multi-currency net worth counted cash
  twice (holdings + 2×cash). Now `portfolio_value` is holdings-only in all paths;
  daily-change still compares the holdings+cash total against snapshot `total_value`
  (= holdings_value + cash_value). Single-currency workspaces were unaffected **by the
  net-worth double-count** (their `portfolio_value` was already holdings-only).
  Separately/pre-existing: the single-currency `daily_change` compares holdings-only
  against snapshot `total_value` (holdings+cash) — left unchanged here, since whether
  `daily_change` should include cash movements is a product question, not part of this
  fix.
- **2026-07-01 (spec-049):** Fixed transfers **out of** a brokerage account not
  decrementing the source account's cash snapshot (`create_transfer` only ever wrote a
  snapshot for the *to*-side). A Groww→ICICI transfer, for example, correctly wrote the
  ledger row but left Groww's cash balance (and therefore Net Worth's brokerage cash
  figure) unchanged. Added a symmetric from-side branch, plus
  `get_by_trigger_ref_and_account` so `delete_transfer`/`update_transfer` can
  disambiguate the two snapshot rows an investing-to-investing transfer now produces.
  Not retroactive — transfers created before this fix have no from-side snapshot and are
  treated as unmanaged on that side (no-op on delete/update), same as any side whose
  module was never `"investing"`.
- **2026-07-02 (spec-050):** Two fixes prompted by a manually-entered ICICI cash balance
  (backfilling pre-tracking history):
  1. **One account, one currency**, enforced going forward at cash-balance
     create/update, order placement, and transfer create/update (both sides
     independently). `default_currency_code` was previously decorative — nothing checked
     a cash balance/order/transfer's currency against the account it's on, which is
     exactly the class of bug behind the IND Money transfer incident. This removes the
     prior ability for one account to hold cash/holdings in multiple currencies (a real
     capability the schema allowed but nothing in the product actually needed once each
     account is single-currency). Not retroactive.
  2. **Net-worth aggregation no longer double-counts non-brokerage cash-balance
     snapshots.** `get_summary`'s `cash_total` previously summed every cash-balance row
     with no account-type filter; a bank/wallet account with both ledger activity
     (`spending_total`) and a manually-added cash-balance snapshot (legitimate — that's
     reconciliation's ground-truth mechanism) was counted twice. Filtered to brokerage
     accounts only; reconciliation itself is untouched (still works for any account type).
  Also fixed in the same pass: `GET /investing/cash-balances` had no way to query a
  specific account — the Cash tab always fetched a fixed 200-row page (`as_of` desc) and
  filtered client-side, so an old-dated backfill snapshot in a workspace with 200+ rows
  (easy — every order writes one) was invisible and undeletable in the UI despite
  existing in the DB. Added a server-side `account_id` filter.
- **2026-07-03 (spec-051):** Added corporate actions (stock splits, reverse splits, bonus
  issues) as a new `investing_corporate_actions` table, replayed inside
  `InvestingOrderService._replay_orders` (`order_service.py`) in the same chronological
  event stream as buy/sell orders, ordered by `ex_date`. **Cash-neutral by construction:**
  `create_corporate_action`/`delete_corporate_action` call the FIFO-lot recompute path
  only — neither has a cash-balance dependency, so a corporate action never writes an
  `investing_cash_balances` row and adds no term to either side of the reconciliation
  identity (asserted directly by a golden reconciliation test: a split recorded between a
  funding transfer and offsetting orders still reconciles to `discrepancy == 0`). A split
  or reverse split scales every open `OrderLot` in place (same lot identity, same
  `acquired_at`, preserving holding-period continuity — Indian tax law treats a split as
  the same investment subdivided, not a new acquisition); a bonus issue creates a new
  zero-cost lot dated at allotment instead (a bonus *is* tax-law a new, separate
  acquisition at nil cost — Section 55(2)(aa)(iiia)/Explanation 1(i)(h) to Section 2(42A)),
  which required `OrderLot.buy_order_id` to become nullable with a sibling
  `corporate_action_id` FK (exactly one of the two set). Not retroactive: existing
  un-split holdings (e.g. imported NVDA/GOOGL rows with real-world un-applied splits)
  stay wrong until a user records the corporate action via the new endpoints.
- **2026-07-12 (spec-075, backend as-of conversion):** Display-currency conversion
  (net worth, investing summary/live-cash, snapshot valuation, lookthrough exposure)
  now uses one FX rate per calendar day — the *previous* day's close — instead of
  whatever row happened to be most recently ingested, collapsing the historical-vs-live
  distinction into a single rule (`effective_display_as_of` in `app/core/currency.py`).
  A same-day/intraday rate is never used, even if present; missing a rate for the
  previous day degrades to the nearest earlier system/user row (existing
  `get_latest_rate`/`get_historical_rate_with_source` semantics), never forward. No
  change to what's written (ledger/snapshot rows, `fx_rate_used` on transfers) — this is
  a read/display-path rule only. The frontend display-profile (locale/grouping) and
  explicit per-value FX provenance fields from spec-075 are follow-up work, not yet
  implemented.
