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
* **What they mean:** A cash balance represents a static **snapshot** of the liquid cash held inside a specific workspace account (such as a checking account, a brokerage cash account, or a foreign currency wallet) as of a specific date.
* **What they are NOT:** They are not a live ledger of spending transactions. Adding a cash balance does not create individual income or expense rows in your spending history.

---

## 3. How to Handle Common Scenarios

### 💵 Monthly Salary / Inflow
When your monthly salary is paid into your account, you should **record it as an Income Transaction in the Spending module**.
* **Why:** This ensures the salary flows into your monthly income/expense metrics, counts towards budget guardrails, and appears in cash flow dashboards.
* **How it affects Cash Balances:** Under Lifestack V1, creating a transaction does **not** automatically update your cash balances (there are *no hidden side effects*). To reconcile, you periodically update the Cash Balance snapshot for that account to reflect its new real-world balance.

### 🏁 Setting up an Initial Balance
When you first start using Lifestack, you will want to reflect the money you already have.
1. Create your Accounts in the Master Finance settings (e.g., "Main Bank Account" as a `bank` type, "Brokerage" as a `brokerage` type).
2. Go to **Investing > Cash Balances** and add an initial cash balance for each account.
3. This sets your baseline portfolio valuation and net worth. You do *not* need to log a historical spending transaction for your initial balances.

### 🔄 Periodic Balance Updates (Not a One-Time Setup)
Since Lifestack does **not** calculate your account balances dynamically by summing up historical transactions, **you must periodically update or log new Cash Balance snapshots**.
* If you only set an initial balance and never update it, your portfolio valuation and net worth will become stale as you spend money or receive salary.
* Periodically (e.g., once a week or at the end of each month), you should edit/update your Cash Balances to match the actual statement balance of your real-world bank/brokerage accounts.

### 🔄 Moving Money Between Accounts (Transfers)
If you move money from your spending account to your brokerage account (e.g., transferring $1,000 to invest):
* Log a **Capital Transfer** (`POST /v1/finance/transfers` or via the Transfers UI).
* The transfer captures the event, including the gross amount, net amount received, tax, and FX fees/rates.
* Just like transactions, creating a transfer records the *historical event* but does not automatically change the cash balance snapshots. You should update the respective cash balance snapshots to reflect the transfer's arrival.

## 4. Why are Spending Ledger and Cash Balances Decoupled?

Lifestack deliberately avoids automatic balance mutations when logging transactions or transfers:
1. **No Hidden Side Effects:** It keeps the database state explicit and highly auditable.
   * *Note:* While a spending transaction/transfer has an associated `account_id`, **logging it has absolutely zero mathematical impact on the account's cash balance snapshot**. Lifestack does not subtract from or add to cash balance snapshots dynamically when you log transactions.
2. **Reconciliation Flexibility:** You can import bank CSVs or update your cash balance snapshots manually, without having to perfectly categorize and align every single cup of coffee to keep your balance correct.
3. **Multi-Currency Clarity:** FX rates fluctuate constantly. Decoupling ensures that read-time valuation conversion doesn't corrupt historical transaction data.

---

## 5. Reconciling and Tallying Balances

### How Personal Finance Apps Handle This
* **Ledger-Based (e.g., Firefly III, GnuCash):** Every expense is automatically deducted from the account. If you miss logging a single coffee, your account balance is incorrect.
* **Snapshot-First (e.g., Monarch Money, Copilot, Lifestack):** Balance snapshots are treated as the "ground truth" (e.g., synced from bank APIs or updated manually), while the transaction ledger is for categorizing flow.

### Tallying Up in Lifestack (Automated Reconciliation UI)
In Lifestack, the **Spending Ledger** tab has built-in reconciliation support:
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
