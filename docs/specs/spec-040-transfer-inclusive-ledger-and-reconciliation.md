# Spec 040 – Transfer-Inclusive Ledger and Account Reconciliation

**Status:** Draft
**Spec ID:** 040
**Created:** 2026-06-26

---

## 1. Problem

The current spending ledger (`GET /spending/accounts/{id}/ledger`) computes a running balance solely from `spending_transactions`. Capital transfers (`capital_transfers` table) are completely excluded from the running balance calculation.

This makes the ledger **mathematically incomplete**:
- A bank → brokerage transfer of ₹10,000 will not reduce the bank account's projected balance.
- The `spending_balance` from `GET /finance/accounts/{id}/balance` and the ledger's closing balance both ignore money movement events.
- There is no mechanism to compare the projected ledger balance against the known real-world balance (cash snapshot), so discrepancies from missed transactions are invisible.

As a result, the User Handbook statement that "logging a transaction has absolutely zero mathematical impact on the account's cash balance snapshot" remains true, but the reverse is also now a problem: cash balance snapshots have no relationship to the projected spending ledger. Users have no reconciliation path.

---

## 2. Goals

1. **Transfer-Inclusive Ledger:** Include capital transfers in the per-account ledger timeline and running balance computation.
2. **Reconciliation Summary Endpoint:** A single endpoint that compares the projected ledger balance (from transactions + transfers) against the latest known cash balance snapshot for an account, and returns the discrepancy.
3. **Frontend Reconciliation Card:** Surface the reconciliation summary on the Ledger tab in the Spending page.

---

## 3. Out of Scope

- Statement matching / CSV upload bank-statement matching (V2 Phase 2, future spec).
- Automated cash balance snapshot writes triggered by transactions.
- Multi-account reconciliation view / aggregated net-worth reconciliation (future spec).
- Cross-currency reconciliation — this spec handles single-currency accounts only; multi-currency is out of scope for Phase 1.

---

## 4. Domain Model Changes

### 4.1 LedgerEntry – extended

Add a new `entry_kind` discriminator field to `LedgerEntry` to distinguish spending transactions from transfer events:

```
entry_kind: "transaction" | "transfer_out" | "transfer_in"
```

For transfer entries, `category_id` is set to `None` (transfers have no spending category). The existing `public_id` maps to `capital_transfer.public_id`.

### 4.2 ReconciliationSummary – new schema

```
account_public_id: UUID
account_name: str
currency_code: str
projected_balance: Decimal       # running balance per ledger (transactions + transfers)
snapshot_balance: Decimal | None # latest cash balance snapshot, or null if none exists
snapshot_as_of: datetime | None  # when the snapshot was recorded
discrepancy: Decimal | None      # projected - snapshot (positive = ledger > snapshot)
transaction_count: int
transfer_count: int
```

---

## 5. API Changes

### 5.1 Modified: `GET /spending/accounts/{id}/ledger`

**Change:** Union `capital_transfers` into the ledger page query wherever `from_account_id` or `to_account_id` matches the requested account.

- Transfer outflows (`from_account_id == account_id`): treated as **debit** (subtracted from running balance), `entry_kind = "transfer_out"`, `description` shows destination account name.
- Transfer inflows (`to_account_id == account_id`): treated as **credit** (added to running balance), `entry_kind = "transfer_in"`, `description` shows source account name.
- Transfers are ordered by `occurred_at` DESC alongside transactions (consistent tie-breaking by `id DESC`).

**Response:** `LedgerResponse.items` entries now include `entry_kind` and `category_id` is `None` for transfer rows.

**Backward compatibility:** `total_transactions` is renamed to `total_entries` and now counts transactions + transfers. Old `total_transactions` field is retained as a deprecated alias.

### 5.2 New: `GET /finance/accounts/{id}/reconciliation`

Returns a `ReconciliationSummary` for an account:

1. Compute `projected_balance` = same logic as the transfer-inclusive ledger balance (all-time, no date filter).
2. Look up the **most recent** `investing_cash_balances` row for the account ordered by `as_of DESC`.
3. Return both values, their difference, and metadata.

No writes. This is a pure read endpoint.

### 5.3 Modified: `GET /finance/accounts/{id}/balance`

Add `transfer_balance_contribution` field showing the net transfer effect (inflows minus outflows). The existing `spending_balance` field is extended to include transfers so the total projected balance matches the new ledger.

---

## 6. Backend Implementation Plan

### 6.1 `app/spending/repository.py`

**`TransactionRepository.get_ledger_page`**: Rewrite to use a `UNION ALL` of:
```sql
-- Leg 1: spending transactions
SELECT id, public_id, occurred_at, amount,
       'transaction' AS entry_kind,
       description, wallet_name, labels, source_type,
       category_id, NULL as counterpart_account_id, type
FROM spending_transactions
WHERE workspace_id = :ws AND account_id = :acct [date filters]

UNION ALL

-- Leg 2: capital transfers
SELECT id, public_id, occurred_at, gross_amount,
       CASE WHEN from_account_id = :acct THEN 'transfer_out' ELSE 'transfer_in' END,
       notes, NULL, NULL, source_type,
       NULL AS category_id,
       CASE WHEN from_account_id = :acct THEN to_account_id ELSE from_account_id END,
       CASE WHEN from_account_id = :acct THEN 'expense' ELSE 'income' END AS type
FROM capital_transfers
WHERE workspace_id = :ws AND (from_account_id = :acct OR to_account_id = :acct) [date filters]
```
Ordered by `occurred_at DESC, id DESC`. Pagination applied to the union.

**`TransactionRepository.get_account_net_balance`**: Extended similarly to include transfer contributions when computing the tail balance for running balance accumulation.

### 6.2 `app/finance/repository.py`

**`AccountRepository.get_spending_balance`**: Extend to include capital transfer net contributions (inflows - outflows for the account). Add `transfer_count` to return tuple.

**`AccountRepository`**: Add `get_reconciliation_summary(workspace_id, account_id)` method:
- Calls extended `get_spending_balance` for `projected_balance`.
- Fetches latest `CashBalance` row for the account from `investing_cash_balances`.
- Returns raw tuple for service layer to assemble.

### 6.3 `app/finance/service.py`

Add `AccountService.get_reconciliation_summary(workspace_id, account_public_id)` method assembling the `ReconciliationSummary` schema.

### 6.4 `app/finance/schemas.py`

Add `ReconciliationSummary` Pydantic model. Add `transfer_balance_contribution` field to `AccountBalanceResponse`.

### 6.5 `app/spending/schemas.py`

- Add `entry_kind: Literal["transaction", "transfer_out", "transfer_in"]` to `LedgerEntry`.
- Add `total_entries: int` to `LedgerResponse`; keep `total_transactions` as deprecated alias.

### 6.6 `app/finance/router.py`

Add `GET /accounts/{account_id}/reconciliation` route.

---

## 7. Frontend Changes

### 7.1 `lifestack-web/src/services/spending.ts`

- Update `LedgerEntrySchema` with `entry_kind` field.
- Update `LedgerResponseSchema` with `total_entries` field (keep `total_transactions` for backward compat).

### 7.2 `lifestack-web/src/services/finance.ts` (or new `financeService` method)

- Add `getAccountReconciliation(accountId)` → `ReconciliationSummary`.
- Add `ReconciliationSummarySchema` Zod schema.

### 7.3 `lifestack-web/src/pages/SpendingPage.tsx` — `SpendingLedgerTab`

- Fetch reconciliation summary when an account is selected.
- Show a **Reconciliation Card** below the balance summary cards:
  - Projected Balance vs Snapshot Balance
  - Discrepancy highlighted in amber (small gap) or rose (large gap ≥ 5% of projected balance)
  - "No snapshot recorded" state when `snapshot_balance` is null
- In the ledger table, render transfer rows differently from transaction rows:
  - `entry_kind === "transfer_out"`: Debit column in indigo/purple (distinct from expense red)
  - `entry_kind === "transfer_in"`: Credit column in cyan/teal (distinct from income green)
  - Description shows "Transfer → [dest account]" / "Transfer ← [src account]"

---

## 8. Security and Isolation

- All new queries remain workspace-scoped.
- `capital_transfers` has compound FK `(account_id, workspace_id)` — the union query enforces `workspace_id = :ws` on both legs.
- No cross-workspace account resolution.

---

## 9. Test Plan

### Backend Integration Tests

**File:** `app/tests/routers/test_spending_ledger.py` (extended) + new `app/tests/routers/test_reconciliation.py`

1. **Ledger with transfers:** Create two accounts, add spending transactions and a capital transfer; verify transfer appears in ledger page and running balance is correctly adjusted.
2. **Transfer pagination:** Enough entries (mix of transactions and transfers) to span multiple pages; verify running balance continuity across pages.
3. **Reconciliation — happy path:** Account with transactions, a transfer, and a cash snapshot; verify `projected_balance`, `snapshot_balance`, and `discrepancy` are correct.
4. **Reconciliation — no snapshot:** Returns `snapshot_balance = null`, `discrepancy = null`.
5. **Reconciliation — no transactions or transfers:** Returns `projected_balance = 0`.
6. **Workspace isolation:** Cannot retrieve ledger or reconciliation for an account belonging to a different workspace.

### Frontend (manual verification on Ledger tab)

- Transfer rows appear in table with distinct styling.
- Reconciliation card renders correctly for all three states: match, discrepancy, no snapshot.
- Pagination works with mixed entry types.

---

## 10. Acceptance Criteria

1. `GET /spending/accounts/{id}/ledger` includes capital transfers in the entry list and running balance.
2. Transfer entries have `entry_kind = "transfer_out" | "transfer_in"` in the response.
3. `GET /finance/accounts/{id}/reconciliation` returns `projected_balance`, `snapshot_balance`, `discrepancy`, and timestamps.
4. `GET /finance/accounts/{id}/balance` `spending_balance` includes transfer contributions.
5. Frontend Ledger tab shows transfer rows with distinct styling and a reconciliation card.
6. All integration tests pass; no regression in existing ledger or balance tests.

---

## 11. Migration / Rollout

No database schema changes are required. All changes are in query logic and API response shapes only.

1. Backend: transfer-inclusive ledger + reconciliation endpoint.
2. Frontend: ledger table transfer rows + reconciliation card.
3. Update `docs/USER_HANDBOOK.md` section 4 to reflect that spending balance and ledger now include transfer contributions.
