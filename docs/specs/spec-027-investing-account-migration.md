# Feature Spec: Investing Account Identity Migration

**Status:** Implemented
**Spec ID:** 027

Implementation note (2026-06-11): investing holdings and cash balances now reference workspace accounts by `account_id`, with compound tenant-safe foreign keys, API UUID contracts, capture-tool account-name resolution, migration/backfill support, and integration coverage.

## 1. Overview

Currently, the investing module tables (`investing_holdings` and `investing_cash_balances`) store references to workspace accounts via a free-form `account_name` text column. This creates multiple issues:
- Drift: Renaming a finance account does not propagate to investing records, causing broken references.
- Unreliable analytics: Joins and aggregations across modules must rely on case-insensitive string matching of account names instead of database-enforced foreign keys.
- Isolation risk: Lacks compound workspace safety constraints.

This specification outlines the schema migration to replace `account_name` with `account_id` (pointing to the integer primary key in the `accounts` table), with a transitional database backfill and compound tenant-safe constraints.

## 2. Requirements

### 2.1 Database Schema Changes
- **New Columns**:
  - `investing_holdings.account_id` (Integer, NOT NULL)
  - `investing_cash_balances.account_id` (Integer, NOT NULL)
- **Foreign Key Constraints**:
  - Enforce compound workspace safety constraint:
    - `investing_holdings(account_id, workspace_id)` -> `accounts(id, workspace_id)`
    - `investing_cash_balances(account_id, workspace_id)` -> `accounts(id, workspace_id)`
- **Unique Constraints**:
  - Replace the unique constraint on `investing_holdings`:
    - Drop `uq_holding_workspace_symbol_account` on `(workspace_id, symbol, account_name)`.
    - Add `uq_holding_workspace_symbol_account` on `(workspace_id, symbol, account_id)`.
- **Drop Columns**:
  - Drop `account_name` from both `investing_holdings` and `investing_cash_balances`.

### 2.2 Migration & Backfill Strategy (Alembic)
The migration must backfill existing data safely:
1. Add `account_id` columns as nullable integers.
2. For each unique `(workspace_id, account_name)` pair present in `investing_holdings` or `investing_cash_balances`:
   - Check if a matching `Account` exists in the `accounts` table (exact name and workspace match).
   - If not found, create a new `Account` with:
     - `name` = original `account_name`
     - `workspace_id` = original `workspace_id`
     - `account_type` = `'brokerage'`
     - `default_currency_code` = the currency of the holding/cash balance (default to `'USD'`)
     - `is_active` = `True`
3. Update all rows in `investing_holdings` and `investing_cash_balances` to set `account_id` to the corresponding account's integer primary key.
4. Alter the `account_id` columns to be `NOT NULL`.
5. Apply the foreign keys and unique constraints.
6. Drop the old `account_name` columns.

### 2.3 API & Pydantic Hardening
- **Request Schemas**:
  - Update `HoldingCreate` and `CashBalanceCreate` to accept `account_id: uuid.UUID` (the public UUID of the account) instead of `account_name: str`.
- **Response Schemas**:
  - Update `HoldingResponse` and `CashBalanceResponse` to include:
    - `account_id: uuid.UUID` (the public UUID of the account).
    - `account_name: str` (resolved dynamically from the linked account for display compatibility).
- **Service Layer**:
  - Resolve the incoming account UUID (`account_id` in the API payload) using `AccountRepository.get_by_public_id(workspace_id, account_id)`.
  - Validate that the account exists, belongs to the correct workspace, and is active. Raise a `ValidationError` if not.
  - Set `holding.account_id` / `cash.account_id` to the integer primary key.

### 2.4 Voice Agent / Capture Tool Compatibility
- The agent tool `log_cash_balance` accepts `account_name: str` as an argument from LLM tool calling.
- Resolve the `account_name` to the correct account's UUID using `AccountRepository.get_by_name(workspace_id, account_name)` within the tool execution layer before invoking the service.
- Return a descriptive error if the account name is unknown.

### 2.5 Frontend / Web Alignment
- Update the API client service definitions in `lifestack-web/src/services/investing.ts` and type definitions in `lifestack-web/src/types/investing.ts`.
- In `InvestingPage.tsx`:
  - Fetch accounts from `/v1/finance/accounts`.
  - Populate the holding and cash balance creation forms with dropdown menus using the accounts, mapping `account.public_id` to form value and `account.name` to display label.
  - Filter lists by `holding.account_id === selectedAccountId` rather than by name string.

## 3. Testing Plan

- **Unit/Integration Tests**:
  - Update existing tests in `app/tests/integration/test_investing.py` and `app/tests/capture/test_agent.py` to use account UUIDs instead of names.
  - Assert that cross-workspace account assignment attempts fail with a `ValidationError` / `404 Not Found`.
  - Verify that the Alembic migration runs successfully without data loss and correctly backfills existing test data.
