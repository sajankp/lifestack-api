# Feature Spec: Gate 0 Foundation Remediation

**Status:** Proposed
**Spec ID:** 028

## 1. Overview

To conclude the **Gate 0: Foundation** milestone, several final hardening and reliability items must be implemented:
1. **Non-spending source metadata conventions**: Standardize `source_type`, `source_ref`, and `source_import_id` columns across non-spending tables (`investing_holdings` and `spending_budgets`) to track their origin (manual, imported, synced, assistant) and enable full bulk import rollback support.
2. **Remaining Decimal/valuation assumption review**: Return and visually display the exact FX rates used during conversion on the frontend, making multi-currency portfolio valuations fully transparent.
3. **Deterministic demo seed/reset**: Introduce an API endpoint to clear workspace-scoped transaction/holding data and seed a realistic, deterministic set of mock assets, categories, budgets, todos, and notifications to allow clean product demonstration.
4. **README/spec limitations refresh**: Update documentation to specify execution caveats, known limits, and CI configurations.

---

## 2. Requirements

### 2.1 Non-spending Source Metadata & Rollback
* **Database Columns (Alembic Migration 0025)**:
  * Add `source_type` (VARCHAR(32), default `'manual'`), `source_ref` (VARCHAR(255), NULL), and `source_import_id` (Integer, NULL, FK to `import_batches.id`) to:
    * `investing_holdings`
    * `spending_budgets`
* **API Schemas**:
  * Add `source_metadata: SourceMetadataResponse | None = None` to:
    * `HoldingResponse` (in `app/investing/schemas.py`)
    * `BudgetResponse` (in `app/spending/schemas.py`)
* **Bulk Import Commit**:
  * In `ImportService.commit_import` (for `ImportModule.investing_holdings` and `ImportModule.spending_budgets`), populate:
    * `source_type = 'imported'`
    * `source_import_id = batch.id`
    * `source_ref = f"{batch.public_id}:{row.row_number}"`
* **Bulk Import Rollback (Deletion)**:
  * In `ImportService.delete_batch`, remove the module check block.
  * In `ImportRepository`, implement:
    * `delete_holdings_for_batch(workspace_id, batch_id)`
    * `delete_budgets_for_batch(workspace_id, batch_id)`
  * Delete the corresponding records before deleting the batch itself.

### 2.2 FX Rates Used & Valuation Transparency
* **API Summary Response**:
  * Update `InvestingSummaryResponse` to include `fx_rates_used: dict[str, Decimal] = Field(default_factory=dict)`.
  * In `InvestingService.get_summary`, populate `fx_rates_used` with the exact exchange rates lookup values (e.g. `{"EUR": Decimal("1.085")}`) used to convert non-reporting assets.
* **Frontend Rendering**:
  * In `InvestingPage.tsx`, if the summary contains `fx_rates_used` and the valuation status is `converted_available`, render a clean visual alert/badge list showing:
    * `1 [CURRENCY] = [RATE] [REPORTING_CURRENCY]` (e.g., `1 EUR = 1.085 USD`).

### 2.3 Deterministic Demo Seed/Reset
* **API Route (`POST /v1/platform/workspaces/{workspace_id}/reset-demo`)**:
  * Authenticated to workspace member/admin role.
  * Deletes all user-generated data for the workspace:
    * Todos, SpendingTransactions, SpendingBudgets, Holdings, CashBalances, HoldingPrices, InstrumentConstituents, Instruments, Companies, Accounts, Import batches/errors/previews, Export records, and Notifications.
  * Re-seeds:
    * **Accounts**: Brokerage (USD), Wallet (USD), EUR Wallet (EUR).
    * **Budgets**: Rent (1500.00), Food (400.00), Utilities (200.00).
    * **Transactions**: Rent expense (-1200.00), Groceries expense (-75.50), Coffee expense (-4.75), Salary income (3500.00).
    * **Holdings**: AAPL (10 shares @ 150.00 USD), MSFT (5 shares @ 300.00 USD) on Brokerage.
    * **Cash Balances**: Cash USD (5000.00) on Brokerage, Cash EUR (1200.00) on EUR Wallet.
    * **FX Rates**: EUR/USD (1.085), GBP/USD (1.250).
    * **Todos**: "buy groceries tomorrow", "review investing performance".
    * **Notifications**: "Welcome to your Lifestack workspace!".
* **Frontend Reset Action**:
  * Add a "Demo Data & Reset" section in the settings tab of `MasterConfigPage.tsx` with a button to trigger the workspace demo reset with success/error alerts.

### 2.4 Documentation Refresh
* **`README.md` updates**:
  * Document known sandbox/docker limits.
  * Add exact CI test matrices and command structures.

---

## 3. Verification Plan

### 3.1 Automated Tests
* Add `test_import_rollback_non_spending` verifying budget and holding import deletions roll back successfully.
* Add `test_workspace_demo_reset` verifying that all tables are purged and seeded deterministically.
* Assert all 246+ tests pass.
