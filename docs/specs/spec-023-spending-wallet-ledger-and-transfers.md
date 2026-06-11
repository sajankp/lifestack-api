# Spec 023 - Spending Wallet Ledger and Transfers

**Status:** Implemented (Account/Transfer V1)

Implementation note (2026-06-11): finance accounts, account-backed spending transactions, transfer APIs, tenant-safe account constraints, and import/export compatibility are implemented. Ledger/balance-projection notes remain context only; future sequencing lives in the product roadmap.

## 1) Problem
Spending currently stores `wallet_name` as free text on transactions, which helps tagging but does not provide:
- wallet/account balance tracking,
- reliable transfer semantics,
- consistent money movement history between bank/wallet/card/gift-card containers.

Users need practical personal-finance flows such as:
- bank -> Amazon Pay wallet top-up,
- wallet -> gift card funding,
- card payment settlement tracking.

## 2) Goal
Introduce a wallet-ledger model for spending that supports:
- multiple wallets/accounts per workspace,
- explicit transfers between wallets/accounts,
- accurate wallet balances and transfer history,
- compatibility with existing finance account/transfer model where possible.

## 3) Scope
### In Scope (Phase 1)
- Spending transactions may optionally reference a concrete wallet/account id.
- Wallet CRUD for spending-friendly account types (bank, wallet, card, gift_card).
- Transfer flow UI + API for internal moves between wallets/accounts.
- Transfer entries recorded as first-class records (no "fake expense + fake income" workaround in UI).
- Backward compatibility for existing `wallet_name` imports.

### Out of Scope (Phase 2+)
- Bank sync/open-banking connectivity.
- Reconciliation engine and statement matching.
- Split transactions across multiple wallets in one row.

## 4) Domain Model
### 4.1 Wallet/Account
Option A (preferred): extend/reuse `finance.accounts` with richer `account_type` enum values:
- `bank`
- `wallet`
- `card`
- `gift_card`
- existing `brokerage` retained for investing.

### 4.2 Spending Transaction Link
- Add `account_id` (nullable) on spending transactions.
- Keep `wallet_name` as legacy field during migration window; deprecate for write paths after UI migration.

### 4.3 Transfers
- Reuse `capital_transfers` for account-to-account movement where feasible.
- Add spending-focused UX wrapper so users can execute "transfer" without module-level complexity.
- Preserve fee/tax/FX support fields for cross-currency moves.

## 5) Functional Requirements
### 5.1 Wallet Management
- Users can create, list, archive, and view balances of wallets/accounts in workspace.
- Wallet names unique per workspace (case-insensitive normalized rule).

### 5.2 Spending Entry
- Transaction create/edit supports selecting wallet/account.
- If wallet/account absent, transaction remains valid but is flagged as "unassigned source."

### 5.3 Transfer Flow
- Create transfer between two accounts with:
  - source account,
  - destination account,
  - source amount/currency,
  - optional FX rate/fees/taxes,
  - timestamp and notes.
- Transfer should appear in transfer history and optionally in spending timeline as transfer-tagged events.

### 5.4 Balance Semantics
- Wallet balance projection:
  - inflows: income transactions to wallet + received transfers,
  - outflows: expense transactions from wallet + sent transfers.
- Cross-currency balances shown per currency unless converted view is configured.

## 6) API Surface (Target)
- `GET /v1/finance/accounts` (enhanced account types)
- `POST /v1/finance/accounts`
- `PATCH /v1/finance/accounts/{id}`
- `GET /v1/finance/transfers`
- `POST /v1/finance/transfers`
- Spending transaction create/update accepts `account_id` (public id).

## 7) Migration Strategy
1. Add nullable `account_id` to spending transactions.
2. Backfill heuristic:
  - map existing `wallet_name` values to created wallet accounts per workspace.
  - set `account_id` when deterministic match exists.
3. Keep `wallet_name` readable for history/import compatibility.
4. Gradually shift UI writes to `account_id` only.

## 8) Import/Export Alignment
- CSV imports accept both:
  - `wallet_name` (legacy),
  - `account_name` (new preferred alias).
- If account does not exist:
  - auto-create account (configurable), or
  - strict-mode fail-all (default for Phase 1 imports remains fail-all unless explicitly toggled).

## 9) UX Requirements
- Spending page should expose:
  - wallet filter,
  - transfer action entry,
  - clear transfer badges in timeline/table.
- Avoid overloading "expense" for transfer-only actions.

## 10) Security and Isolation
- Accounts and transfers are workspace-scoped.
- Transfer creation must validate both accounts belong to the same workspace unless explicit cross-workspace transfer is introduced (not in scope).
- Audit events required for account create/update/archive and transfer create.

## 11) Acceptance Criteria
1. Multiple wallet/account records can be created and selected in spending flows.
2. User can transfer between two wallets/accounts; transfer appears in history.
3. Spending transactions can bind to account_id and legacy rows remain readable.
4. Balance calculations reflect both spending rows and transfers.
5. Existing imports with `wallet_name` continue to work.

## 12) Test Plan
### Backend
- Integration:
  - account CRUD with new types,
  - transfer create/list/get happy + isolation paths,
  - spending transaction with account_id validation.
- Migration test:
  - wallet_name to account mapping and non-destructive fallback.

### Frontend
- Component/integration:
  - account selector in transaction modal,
  - transfer modal submit + listing,
  - wallet-filtered transaction list.

## 13) Rollout
1. Backend schema + API extension and compatibility layer.
2. Frontend account selector + transfer UX on spending page.
3. CSV import alias support (`account_name`) and optional auto-create guardrails.
4. Documentation updates across api/web/e2e repos.
