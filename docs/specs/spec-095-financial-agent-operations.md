# Spec-095: Financial Agent & MCP Operations

**Status:** Implemented — pending deployment validation  
**Scope:** API (`app/capture`, `app/mcp`, `app/finance`, `app/spending`), Web (`SpendingPage`, `LedgerTab`), E2E  
**Depends on:** spec-040 transfer-inclusive ledger, spec-043 transfer edit/delete, spec-059 voice agent usability, spec-073 dividend income tracking, spec-090 voice tool idempotency, spec-093 voice transaction correction, spec-094 MCP investment research tools

---

## 1. Context & Problem

Lifestack users and connected agents require conversational (voice) and programmatic (MCP) interfaces to perform financial operations safely:
1. **Ordinary Income vs. Expense:** Ordinary cashflow income and expense share the same underlying transaction architecture (`spending_transactions`, `TransactionService`, `TransactionCreate/Update`), but voice and MCP tools were historically hardcoded to `expense`. Adding duplicate income-specific tools would cause unnecessary tool sprawl.
2. **Source Provenance:** As autonomous agents interact with Lifestack across distinct surfaces (voice assistants, MCP research/finance agents, manual UI, CSV imports), provenance metadata must clearly distinguish the origin channel without corrupting historical records (`assistant`).
3. **Transfers via Voice and MCP:** Moving capital between accounts (spending-to-spending, spending-to-investing, investing-to-spending, investing-to-investing) requires invoking `CapitalTransferService` to enforce currency invariants, FX arithmetic, brokerage cash snapshots, and conflict guards. These operations must be exposed as a purpose-built transfer tool family with strict preview/confirmation semantics and replay safety.
4. **Brokerage Investment Income Boundary:** Dividend, interest, and coupon events on brokerage accounts are owned by `DividendService` (`investing_dividends`) and credit investing cash without counterparty transfers. Ordinary income must not cross into brokerage accounts, and investment dividends must not route through spending transactions.
5. **Account Activity (Ledger) UI:** The account activity / ledger endpoint includes `category_id` for regular transactions and `null` for transfers. The Web UI must render resolved category themes for regular transactions on both desktop and mobile views while displaying clear transfer markers or em dashes for transfer rows.

---

## 2. Goals & Non-Goals

### Goals
- Make voice and MCP ordinary transaction tools (`log_spending_transaction`, `list_spending_transactions`, `find_spending_transactions`) type-aware with an explicit `transaction_type: expense | income` parameter, defaulting to `expense` for backwards compatibility.
- Ensure same-day duplicate detection and resumed-session replay deduplication differentiate between income and expense transactions.
- Introduce distinct `source_type` provenance values for `voice_agent` and `mcp_agent`, preserving legacy `assistant` entries.
- Expose a bounded transfer tool family (`list_transfers`, `find_transfers`, `create_transfer`, `update_transfer`, `delete_transfer`) with identical names across voice and MCP.
- Enforce explicit confirmation (`confirmed=true`) for all transfer mutations and provide complete preview responses when unconfirmed (`confirmed=false`).
- Derive transfer modules (`spending` vs `investing`) and currencies from resolved accounts rather than client input.
- Maintain brokerage cash balance snapshot side effects and 409 conflict detection for transfers.
- Provide voice parity for brokerage dividend income (`create_investment_dividend`) via `DividendService`.
- Render resolved category badges on regular transaction rows in the Web Account Activity tab and em dashes / transfer markers on transfer rows.

### Non-Goals
- Stock order execution / trading via voice or MCP.
- Unconfirmed arbitrary finance mutations.
- Unchecked cross-currency transfers without FX rates.
- Merging brokerage dividend income into spending transactions.

---

## 3. Product & Technical Architecture

### 3.1. Ordinary Transactions (Type-Aware)
- **Schema & Service:** `SpendingTransaction.type` (`TransactionType.expense` / `TransactionType.income`) is passed through `TransactionCreate`.
- **Tools:**
  - `log_spending_transaction`: accepts optional `transaction_type: "expense" | "income"` (default `"expense"`).
  - `list_spending_transactions`: accepts optional `transaction_type: "expense" | "income"` (default `"expense"`).
  - `find_spending_transactions`: accepts optional `transaction_type: "expense" | "income"` (default `"expense"`).
- **Duplicate Detection:** `TransactionRepository.find_same_day_duplicates` filters by `SpendingTransaction.type == type_filter`, preventing false collisions between identical income and expense amounts.
- **Replay Deduplication:** `CaptureToolDedupLedger` keys `log_spending_transaction` fuzzy entries on `(amount, occurred_at, transaction_type)` with omitted values normalized to `"expense"`.

### 3.2. Source Provenance
- `TransactionSourceType` enum values: `manual`, `imported`, `synced`, `assistant`, `order`, `voice_agent`, `mcp_agent`.
- API response helper `source_metadata_response`:
  - `voice_agent` → origin: `assistant_action`, label: `"Voice agent"`.
  - `mcp_agent` → origin: `assistant_action`, label: `"MCP agent"`.
  - `assistant` → origin: `assistant_action`, label: `"Assistant action"`.
- Web Zod `SourceMetadataSchema` accepts `voice_agent` and `mcp_agent`.
- `AgentTools` passes `source_channel="voice_agent"` in voice session context and `source_channel="mcp_agent"` in MCP context into `create_transaction` and `create_transfer`.

### 3.3. Transfer Tool Family (Voice & MCP)
- **Tools:**
  1. `list_transfers(day, from_day, to_day, account_name, amount, search, limit)`: Bounded listing in user's timezone.
  2. `find_transfers(from_day, to_day, account_name, amount, search, limit)`: Bounded candidate lookup requiring at least one clue; returns `needs_filter: True` if results exceed `limit`.
  3. `create_transfer(from_account_name, to_account_name, amount, fx_rate, fees, net_amount, occurred_at, notes, source_ref, confirmed)`:
     - Resolves spoken accounts across all active accounts (bank, wallet, card, gift card, brokerage) via `_resolve_any_account`.
     - Derives `from_module` / `to_module` (`investing` if brokerage, else `spending`).
     - Derives account currencies and validates one-account/one-currency invariants.
     - Calculates `net_amount` using Decimal precision when omitted.
     - If `confirmed=False`: returns preview with accounts, amounts, currencies, fees, FX rate, date, and `needs_confirmation: True`. Performs no database write.
     - If `confirmed=True`: invokes `CapitalTransferService.create_transfer` with audit logging and brokerage cash balance snapshots.
  4. `update_transfer(public_id, from_account_name, to_account_name, amount, fx_rate, fees, net_amount, occurred_at, notes, confirmed)`: Preview on `confirmed=False`; mutates via `CapitalTransferService.update_transfer` on `confirmed=True`.
  5. `delete_transfer(public_id, confirmed)`: Preview on `confirmed=False`; deletes via `CapitalTransferService.delete_transfer` on `confirmed=True`.
- **Replay Safety:** All transfer mutation tools (`create_transfer`, `update_transfer`, `delete_transfer`) are registered in `WRITE_TOOLS` in `CaptureToolDedupLedger`.

### 3.4. Brokerage Investment Income
- `create_investment_dividend` exposed in voice `AgentTools` with explicit confirmation (`confirmed=True`), calling `DividendService.create_dividend` to ensure investing cash balances are credited directly.
- Brokerage dividends are strictly separated from spending transactions.

### 3.5. Web UI: Account Activity Category Display
- `LedgerTab.tsx` receives `getCategoryTheme` from `SpendingPage.tsx`.
- Desktop table renders a `Category` column:
  - For regular transactions (`entry_kind === 'transaction'`), renders the category icon and badge styling.
  - For transfer rows (`entry_kind === 'transfer_out' | 'transfer_in'`), renders an em dash (`—`).
- Mobile card list renders category badges for regular transactions and transfer badges for transfer rows.

---

## 4. Verification Plan

1. **API Unit & Integration Tests (`pytest`):**
   - `test_log_spending_transaction_income_and_expense`: verify income creation, listing, candidate finding, and duplicate isolation.
   - `test_tool_dedup_income_vs_expense`: verify replay ledger distinguishes income and expense.
   - `test_transfer_tools_preview_and_confirmation`: verify preview performs no write, confirmation mutates, modules/currencies are derived, and 409 snapshots are enforced.
   - `test_mcp_transfer_and_income_tools`: verify MCP workspace authorization (`mcp:read`, `mcp:write`) and provenance tagging (`mcp_agent`).
   - Run: `uv run pytest app/tests/capture app/tests/test_mcp_auth.py -v` and `uv run ruff check .`.
2. **Web Tests (`vitest`):**
   - `LedgerTab.test.tsx`: verify category rendering for transaction rows and transfer markers for transfer rows on desktop and mobile.
   - `SpendingPage.test.tsx`: verify category map passing and ledger tab integration.
   - Run: `npm test -- --run`, `npm run build`, `npm run lint`.
3. **E2E Tests (`playwright`):**
   - Run capture, transfer, and account activity tests.
