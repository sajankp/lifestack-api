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
| Transfer **into** an investing account | ✅ transfer row | ✅ `+net_amount_received` in `to_currency`, trigger `transfer` (`finance/service.py:629`) |
| Transfer **out of** any account | ✅ transfer row | ❌ from-side snapshot is **not** decremented (even out of a brokerage) |
| Order **buy** | ❌ orders aren't ledger rows | ✅ `−net` (net = gross + fees); validated vs available cash first (`investing/service.py:1967`, `:2039`) |
| Order **sell** | ❌ | ✅ `+net` (net = gross − fees) |

Key asymmetries to remember:
- **Spends never touch snapshots.** A wallet/bank account typically has *no* snapshot
  at all unless one is entered manually.
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
  (= holdings_value + cash_value). Single-currency workspaces were unaffected.
