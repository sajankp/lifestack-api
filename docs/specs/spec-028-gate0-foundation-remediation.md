# Feature Spec: Gate 0 Foundation Remediation

**Status:** Partially Implemented - Auth/Session Follow-up In Progress
**Spec ID:** 028
**Last updated:** 2026-06-11

## 1. Overview

To conclude the **Gate 0: Foundation** milestone, several final hardening and reliability items must be implemented:
1. **Non-spending source metadata conventions**: Standardize `source_type`, `source_ref`, and `source_import_id` columns across non-spending tables (`investing_holdings` and `spending_budgets`) to track their origin (manual, imported, synced, assistant) and enable full bulk import rollback support.
2. **Remaining Decimal/valuation assumption review**: Return and visually display the exact FX rates used during conversion on the frontend, making multi-currency portfolio valuations fully transparent.
3. **Deterministic demo seed/reset**: Introduce an API endpoint to clear workspace-scoped transaction/holding data and seed a realistic, deterministic set of mock assets, categories, budgets, todos, and notifications to allow clean product demonstration.
4. **README/spec limitations refresh**: Update documentation to specify execution caveats, known limits, and CI configurations.
5. **Current-branch product hardening**: Close the remaining demo-safety and workspace/session correctness gaps before treating Gate 0 as public-demo ready.

Current implementation note (2026-06-11): non-spending source metadata and rollback support, summary FX-rate display, deterministic demo reset, workspace-select refresh-token persistence, active-workspace reset targeting, and investing performance FX conversion are implemented through Spec 029. The current follow-up branch hardens auth/session behavior by blocking inactive users on existing access tokens, clearing current browser cookies after password change, rejecting malformed Bearer headers, and making refresh-token grace retries idempotent. It also adds voice/capture frame, cumulative byte, duration, and text-size ceilings with sanitized provider errors, extracts auth/session dependency wiring into `app/auth/dependencies.py` while preserving existing import compatibility, and adds local/test-only E2E workflow hooks so the full-stack harness can trigger guardrails and recurring generation over authenticated HTTP. Remaining Gate 0 follow-ups are mostly broader maintainability and product-facing failure UX, not new product modules.

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
  * Authenticated to the target workspace.
  * Requires `owner` or `admin` role. `member` and `viewer` must receive `403 Forbidden`.
  * Enabled only when an explicit demo/reset feature flag is active. Production deployments should fail closed unless the operator intentionally enables demo reset.
  * Must verify that the requested `workspace_id` is the active or explicitly selected workspace for the current session.
  * Emits an audit event with actor, workspace, reset result, and seeded fixture version.
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
  * Render the reset affordance only when the backend reports demo reset is enabled and the current user has owner/admin rights.
  * Use the frontend's active workspace state, not the first workspace in a returned list.
  * Display the target workspace name before reset and require an explicit confirmation phrase for the destructive action.

### 2.4 Documentation Refresh
* **`README.md` updates**:
  * Document known sandbox/docker limits.
  * Add exact CI test matrices and command structures.
  * Describe the current product as a finance-led personal operations command center. `Done`
  * Keep health, documents, second brain, MCP, and personal coach tracks clearly marked as future roadmap. `Done`
  * Document the safe demo journey and reset constraints. `Done`

### 2.5 Workspace/Session Demo Readiness
* **Workspace Selection Session Rotation**:
  * Selecting a workspace may issue new access and refresh cookies, but it must also persist the new refresh-token hash to the active auth session.
  * Login, refresh, and workspace select should share the same session-rotation helper or equivalent invariants.
  * A workspace switch followed by `/auth/refresh` must succeed for a legitimate client.
* **Auth Session Revocation Semantics**:
  * Existing access tokens must be rejected when the user is deactivated before the token expires.
  * Password change must revoke server-side sessions and clear the browser's current auth, refresh, sid, and CSRF cookies.
  * Refresh retries using the just-rotated previous token within the grace period must not replace the current refresh token again.
  * Malformed Bearer authorization headers must fail closed instead of being partially parsed.
* **Voice/Capture Resource Ceilings**:
  * WebSocket voice sessions must enforce maximum single-frame size, cumulative client audio bytes, session duration, and text-message size.
  * Provider errors should be logged server-side and returned to the browser as generic retryable messages.
* **Frontend Active Workspace Model**:
  * The web app must have a single source of truth for the active workspace.
  * Workspace-aware destructive actions must never infer the target from `items[0]` or other list ordering.
* **Investing Performance Currency Semantics**:
  * Performance snapshots must convert holdings and cash into the reporting currency before storing or returning aggregate values.
  * Snapshot responses should expose the reporting currency and the FX rates used when conversion occurs.

---

## 3. Verification Plan

### 3.1 Automated Tests
* Add `test_import_rollback_non_spending` verifying budget and holding import deletions roll back successfully.
* Add `test_workspace_demo_reset` verifying that all tables are purged and seeded deterministically.
* Add reset authorization tests proving `viewer` and `member` receive `403 Forbidden`, while `owner` or `admin` succeeds only when demo reset is enabled.
* Add a workspace-switch regression test: login, select another workspace, then refresh successfully.
* Add auth/session regression tests for inactive access-token use, password-change cookie clearing, malformed Bearer headers, and refresh grace retry idempotency.
* Add capture regression tests for frame-size, cumulative byte, text-size, duration-limit, and provider-error sanitization behavior.
* Add a frontend or E2E test proving reset targets the active workspace, not the first workspace in the list.
* Add multi-currency investing performance tests using USD, GBP, and EUR accounts with known FX rates.
* Assert all 246+ tests pass.
