# Lifestack Financial Concepts & User Handbook

Welcome to the Lifestack User Handbook. This guide explains how Lifestack's financial models work and details the distinction between the **Spending** module and the **Investing** module, focusing on cash balances, transaction tracking, and transfers.

---

## 1. Spending vs. Investing: The Conceptual Model

Lifestack splits its financial universe into two distinct domains to keep your budgeting clean and your net worth valuation accurate:

| Module | Core Purpose | Primary Data Shape | Example Actions |
| :--- | :--- | :--- | :--- |
| **Spending** | Tracks the **flow of funds** (income and expenses) over time. | Transaction Ledger (categorical flow) | Logging grocery expenses, receiving salary, paying rent. |
| **Investing** | Tracks the **state of assets** (holdings and cash positions) at a point in time. | Snapshots (positions/balances) | Appreciating stock prices, brokerage cash, savings balances. |

---

## 2. What are "Cash Balances"?

Cash balances are tracked within the **Investing** module.
* **What they mean:** A cash balance represents the liquid cash held inside a specific workspace account (such as a checking account, a brokerage cash account, or a foreign currency wallet) as of a specific date.
* **What they are NOT:** They are not a live ledger of *spending* transactions. Adding a cash balance does not create individual income or expense rows in your spending history.
* **How they change:** Each cash balance carries a `trigger_type` indicating how it was set:
  * `manual` — you entered or edited the snapshot yourself.
  * `transfer` — automatically created when you transfer money *into* an investing/brokerage account (see §4).
  * `order` — automatically created when you place a buy/sell order against a brokerage account (see §3).

---

## 3. How to Handle Common Scenarios

### 💵 Monthly Salary / Inflow
When your monthly salary is paid into your account, you should **record it as an Income Transaction in the Spending module**.
* **Why:** This ensures the salary flows into your monthly income/expense metrics, counts towards budget guardrails, and appears in cash flow dashboards.
* **How it affects Cash Balances:** Under Lifestack V1, creating a transaction does **not** automatically update your cash balances (there are *no hidden side effects*). To reconcile, you periodically update the Cash Balance snapshot for that account to reflect its new real-world balance.

### 🏁 Setting up an Initial Balance
When you first start using Lifestack, you will want to reflect the money you already have.
1. Create your Accounts in the Master Finance settings (e.g., "Main Bank Account" as a `bank` type, "Brokerage" as a `brokerage` type).
2. Go to the **Investing > Cash** tab, select the account, and add an initial cash balance.
3. This sets your baseline portfolio valuation and net worth. You do *not* need to log a historical spending transaction for your initial balances.

### 🔄 Periodic Balance Updates (Spending-Side Accounts)
Lifestack does **not** calculate your *spending* account balances dynamically by summing up historical transactions, so for bank/checking accounts you should **periodically update or log new Cash Balance snapshots**.
* If you only set an initial balance and never update it, the valuation of spending-side accounts will become stale as you spend money or receive salary.
* Periodically (e.g., once a week or at the end of each month), edit/update your Cash Balances to match the actual statement balance of your real-world bank accounts.
* *Brokerage accounts are different:* once you start using transfers and orders (below), their cash balances are maintained automatically.

### 📈 Investing: Buy & Sell Orders (Transaction-Based)
Investing in a **brokerage** account is transaction-based and updates balances automatically:
* Place a buy/sell **order** (`POST /v1/investing/orders` or via the Cash tab's Orders section): record date, symbol, quantity, price per unit, and fees (brokerage fee, tax, other fees).
* On a **buy**: Lifestack checks you have enough brokerage cash, then **auto-deducts** the total cost (`gross + fees`) from the brokerage cash balance and **creates/updates the Holding**. Buy fees are **capitalized into cost basis** (book value), matching broker statements.
* On a **sell**: it verifies you hold enough shares, **auto-adds** the net proceeds (`gross − fees`) to the brokerage cash balance, reduces the holding, and records the **realized gain/loss using FIFO lot consumption** — the sell is matched against your *oldest* open buy lots first, as mandated by Indian tax rules (Section 45(2A), per demat account). Cost basis is lot-based, not a moving average.
* Every order is kept in **Orders history**; holdings are recomputed from the full order history (deleting all orders removes the holding). Bulk CSV/Excel order import is supported for brokerage statements.

### 🔄 Moving Money Between Accounts (Transfers)
If you move money from your spending account to your brokerage account (e.g., transferring $1,000 to invest):
* Log a **Capital Transfer** (`POST /v1/finance/transfers` or via the Transfers UI).
* The transfer captures the event, including the gross amount, net amount received, tax, and FX fees/rates.
* **If the destination is an investing account, Lifestack automatically adds the net amount received to that account's cash balance** (a new snapshot with `trigger_type="transfer"`).
* **If the source is an investing account, Lifestack automatically decrements that account's cash balance by the gross amount** (also `trigger_type="transfer"`) — so a brokerage→bank withdrawal keeps brokerage cash correct. One transfer can therefore produce two snapshots.
* Transfers touching only spending-side accounts do **not** auto-update balances — reconcile those manually.

## 4. What Auto-Updates Balances, and What Doesn't

Lifestack splits balance behavior by module:

* **Spending ledger is decoupled from cash balances.** A spending transaction (income/expense) has an `account_id` but **logging it has zero mathematical impact on any cash balance snapshot**. You reconcile spending-side accounts manually or via CSV import. This keeps spending state explicit, auditable, and tolerant of un-categorized coffee runs.
* **Investing is transaction-based and auto-updates.** Transfers *into or out of* an investing account, and buy/sell *orders* against a brokerage account, automatically create new cash balance snapshots (`trigger_type` of `transfer` / `order`) and maintain holdings with FIFO lot-based cost basis. This removes error-prone manual cost-basis math and gives a per-trade audit trail. Each account holds exactly **one currency** (enforced), which keeps balances and valuations unambiguous.

Why the split:
1. **Explicit spending audit trail:** spending stays snapshot-reconciled so importing bank CSVs never fights with manual entries.
2. **Accurate, low-effort investing:** trades are the source of truth for brokerage cash and holdings, so portfolio value stays correct without manual snapshots.
3. **Multi-Currency Clarity:** FX rates fluctuate constantly. Read-time valuation conversion never corrupts the stored historical amounts.

---

## 5. Reconciling and Tallying Balances

### How Personal Finance Apps Handle This
* **Ledger-Based (e.g., Firefly III, GnuCash):** Every expense is automatically deducted from the account. If you miss logging a single coffee, your account balance is incorrect.
* **Snapshot-First (e.g., Monarch Money, Copilot, Lifestack):** Balance snapshots are treated as the "ground truth" (e.g., synced from bank APIs or updated manually), while the transaction ledger is for categorizing flow.

### Tallying Up in Lifestack (Automated Reconciliation UI)
Reconciliation is surfaced in two places: the **Spending Ledger** tab, and the **Investing Cash** tab (when a specific account is selected there, the same per-account reconciliation panel appears alongside that account's cash snapshots, orders, and transfers):
* **Projected Balance:** Calculated dynamically as:
  $$\text{Projected Balance} = \sum \text{Incomes} - \sum \text{Expenses} + \sum \text{Transfer Inflows} - \sum \text{Transfer Outflows}$$
* **Reconciliation Card:** Located below the balance summary on the Ledger tab, it compares this Projected Balance against the **Latest Cash Balance Snapshot** (your ground-truth statement balance).
* **Discrepancy Highlight:**
  - If the projected ledger and cash snapshot don't match, the discrepancy is displayed.
  - The card color-codes the gap (amber for minor differences, rose/red for discrepancies $\ge 5\%$ of the projected balance).
  - A "No snapshot recorded" state indicates when a cash balance snapshot has not yet been set for the account.

Any difference represents:
1. Missing/unlogged transactions or transfers.
2. Untracked interest, bank fees, or small cash expenses.
3. Errors in the logged date or amount of a transfer.

**Known limitation:** the projected balance currently reflects transactions and transfers only — buy/sell order cash impact and cross-currency flows are not yet included in the projection (the snapshots themselves *do* reflect orders, via `trigger_type="order"`). For brokerage accounts, treat the discrepancy as indicative rather than authoritative until per-currency reconciliation ships. Corporate actions (stock splits, reverse splits, bonus issues) are first-class since spec-051: record them via `POST /v1/investing/corporate-actions` and Lifestack replays your FIFO lots automatically.
