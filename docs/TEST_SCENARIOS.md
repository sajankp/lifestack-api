# LifeStack E2E & Backend Integration Test Scenarios Reference

This reference document outlines all the end-to-end (E2E) and integration test scenarios currently implemented in LifeStack. The testing suite is divided into two primary layers:

1. **Frontend & Full-Stack E2E Suite**: Powered by Playwright (`lifestack-e2e/e2e/`).
2. **Backend Integration / E2E Suite**: Powered by pytest and `httpx.AsyncClient` (`lifestack-api/app/tests/`).

---

## 1. Frontend & Full-Stack E2E Test Suite (`lifestack-e2e/e2e/`)

These tests verify complete user journeys, interacting with the real frontend application and communicating with the backend API.

### 🔐 Authentication & Session Flows (`auth.spec.ts`)
*   **Registration Flow**:
    *   Navigate from the login screen to registration.
    *   Submit registration with unique credentials (email, username, and password).
    *   Verify registration success notification and auto-redirection back to `/login`.
    *   Handle backend rate limiting gracefully during registration attempts.
*   **Login Flow**:
    *   Authenticates using the newly registered credentials.
    *   Asserts successful redirection to `/` and checks that the Dashboard is fully visible.
*   **Logout Flow**:
    *   Click the logout button.
    *   Verify redirection back to `/login` and that session cookies are invalidated.
*   **Protected Access Gate**:
    *   Verify that attempting to access `/` while logged out immediately redirects to `/login`.

---

### 💵 Spending & Budgets Guardrails (`spending-guardrails.spec.ts`)
*   **Custom Category Creation**:
    *   Navigate to the Spending Tracker tab.
    *   Open "Manage Categories" and create a custom category (e.g., "Dining Out") with a selected emoji icon.
*   **Budget Limit Allocation**:
    *   Create a budget for the custom category (e.g., $100.00) and verify that a budget card is added to the list under the "Budgets" tab.
*   **Transaction Logging**:
    *   Log a new expense transaction breaching the warning threshold (95% of the budget amount, which is >=90% warning mark).
    *   Verify that the transaction appears under the "Transactions" tab.
*   **Background Budget Guardrails Integration**:
    *   Triggers the backend evaluator via Docker execution of the evaluation job.
    *   Navigates to the Todo list page.
    *   Polls/reloads the page to verify that a warning todo has been generated dynamically for the category.

---

### 🔄 Spending Recurring Transactions (`spending-recurring.spec.ts`)
*   **Recurring Rule Provisioning**:
    *   Create a recurring transaction rule (e.g., Monthly expense of $14.99 with rule description "Netflix Sub").
    *   Verify the recurring rule card is visible with correct details.
*   **Edit Existing Rules**:
    *   Edit the rule amount (e.g., change from $14.99 to $19.99).
    *   Verify the updated details are rendered.
*   **Recurring Engine Execution**:
    *   Triggers the background Python workflow `process_workspace_recurring_transactions` via Docker Compose.
    *   Verify that the transaction is generated under the Transactions list with the edited amount ($19.99).
*   **Rule Deactivation**:
    *   Deactivate the recurring rule.
    *   Verify that it is no longer listed in the active recurring rules view.

---

### 💱 Multi-Currency & FX Look-through Valuation (`investing-fx.spec.ts`)
*   **Multi-Currency Account Setup**:
    *   Create a GBP Brokerage account.
    *   Create a USD Brokerage account.
*   **Multi-Currency Holdings Logging**:
    *   Add a GBP holding (VWRD, 10 units @ 100 GBP avg cost).
    *   Add a USD holding (AAPL, 5 units @ 150 USD avg cost).
*   **Reporting Currency Dynamic Switch**:
    *   Patch user reporting currency to `USD` using the shared-session cookies.
    *   Reload the dashboard and verify the reporting currency indicator shows USD.
*   **Valuation & Triangulation**:
    *   Verify that the total portfolio value resolves to `$2,000.00` based on current FX rates:
        *   `10 * 100 GBP = 1000 GBP` (converted at `1.25` FX rate = `$1,250 USD`).
        *   `5 * 150 USD = $750 USD`.
        *   Total Portfolio Value: `$1250 + $750 = $2000 USD`.
*   **Direct & Look-through Analytics**:
    *   Verify that direct and look-through analytics cards display direct sum totals correctly.

---

### 🛡️ Workspace Role-Based Access Control (`rbac.spec.ts`)
*   **Viewer Role Read-Only Enforcement**:
    *   Register an Owner.
    *   Register a separate Viewer.
    *   Owner invites the Viewer to the workspace as a `viewer`.
    *   Viewer logs in, selects the shared workspace, and attempts to log a transaction.
    *   Verify the API rejects the mutation with `403 Forbidden`.
*   **Member Role CRUD Permission**:
    *   Register a Member.
    *   Verify the Member can successfully create, read, and delete todo items.
*   **Settings Access Enforcement**:
    *   Verify that the `viewer` is rejected with `403 Forbidden` when trying to update workspace finance settings.
    *   Verify that the `owner` is allowed to update workspace finance settings.

---

### 🛠️ Master Config & Display Settings (`finance-display-settings.spec.ts` & `runtime-header-master-config.spec.ts`)
*   **Workspace vs. User Overrides Formatting**:
    *   Verify the default is symbol-style USD formatting (`$`).
    *   Update workspace preferences to Code First (renders as `USD`).
    *   Verify the Dashboard portfolio values change to `USD`.
    *   Update user overrides to Symbol First (renders as `$`).
    *   Verify user override takes precedence over workspace settings on dashboard totals.
*   **Global Header Presence**:
    *   Verify notifications header and logout buttons are visible across all protected pages.
*   **Configuration CRUD**:
    *   Create, list, edit, and save financial accounts inside Settings.
    *   Edit and save default transaction category details.

---

### 📂 Bulk Imports & Data Exports (`imports-smoke.spec.ts` & `exports.spec.ts`)
*   **CSV Import Validation & Commit**:
    *   Generate a spending import CSV file.
    *   Upload, validate, and preview the import rows.
    *   Commit the imports batch and verify the upload status advances to `completed`.
*   **JSON Data Export Lifecycle**:
    *   Request JSON export containing `todo`, `spending`, and `investing` modules.
    *   Verify that it resolves to `ready` synchronously.
    *   Download the JSON file and assert it contains standard schema tags, workspace context, and module data.
*   **CSV Data Export Lifecycle**:
    *   Request CSV export.
    *   Verify the download endpoint delivers a ZIP archive file (validated by zip file signature headers `PK..`).

---

### 📝 Todo Smoke Flow (`todo-smoke.spec.ts`)
*   **Todo CRUD**:
    *   Create a todo task.
    *   Verify task heading is rendered on the board.
    *   Complete the todo, verifying text transitions to completed status.

---

## 2. Backend Integration & HTTP E2E Test Suite (`lifestack-api/app/tests/`)

These tests verify backend domain business logic, authorization rules, security headers, database integrity, and transactional boundaries.

### 📋 API Contracts & RFC 7807 Error Envelope (`integration/test_api_contracts.py` & `test_errors.py`)
*   **API Response Keys Consistency**:
    *   Assert key contracts for `/v1/auth/me` (`public_id`, `email`, `username`, `is_active`).
    *   Assert key contracts for spending categories lists (`items`, `total`, `limit`, `offset`).
    *   Assert key contracts for currencies, accounts, holdings, cash balances, and finance settings.
*   **Standardized Error Envelope (RFC 7807)**:
    *   Assert that all API errors return fields: `type`, `code`, `title`, `status`, `detail`, `hint`, `instance`.
    *   Assert validation errors (422) return a nested list of field-level errors.
    *   Assert unauthorized requests (401), missing tokens, and rate limits (429) deliver standard problem detail formats.

---

### 🔐 Security Hardening & Session Security (`test_security_hardening.py` & `test_auth_rbac_security.py`)
*   **Headers & Content Security Policy**:
    *   Verify response headers include a unique `X-Request-ID`.
    *   Verify HSTS `Strict-Transport-Security` headers are dynamically sent when `X-Forwarded-Proto` matches `https`.
    *   Verify `X-XSS-Protection` is enabled and set to block.
    *   Verify Content Security Policy (CSP) headers are active.
*   **Password Complexity Rules**:
    *   Registration rejects passwords without uppercase letters, numbers, or special characters.
    *   Password change validation rejects weak passwords.
*   **Registration Enumeration Protection**:
    *   Registering duplicate emails or usernames returns identical generic error messages to prevent account enumeration.
*   **Session Lifecycle & Refresh Token Rotation**:
    *   Verify refresh token rotation returns a new refresh token and access token on refresh.
    *   Verify grace period retry logic (allows refreshing with the old token within 5 seconds).
    *   Verify replay attack detection (reusing an old refresh token outside the grace period revokes the session family and clears cookie jars).
*   **Logout All**:
    *   Logging out of all sessions revokes all refresh tokens and sessions associated with the user across devices.
*   **Proxy & Client IP Detection**:
    *   Verify that `get_client_ip` correctly extracts the client IP using `X-Forwarded-For` only when it comes from a list of `TRUSTED_PROXIES`.

---

### 🏢 Platform & Workspace Architecture (`integration/test_platform.py` & `integration/test_workspace_resolution.py`)
*   **Registration Provisioning**:
    *   Registration creates user, workspace, workspace owner membership, and maps default notification categories atomically.
    *   If provisioning fails (e.g. database error), verify the entire registration rolls back leaving no orphaned rows.
*   **Workspace Context Selection & Resolution**:
    *   Users can have multiple workspaces. Switching workspaces updates session context.
    *   If a workspace membership is deleted, subsequent requests fall back to resolving from the user's remaining workspace membership.
    *   Workspace isolation ensures that requests targeting one workspace are completely isolated and cannot read or write data from other workspaces.
*   **Member Management Access Controls**:
    *   Owners can invite new members to workspaces.
    *   Members and viewers are blocked from inviting workspace members.
    *   Inviting users to deactivated workspaces is blocked.

---

### 🪙 Currencies, Exchange Rates, & Ingestion (`finance/` unit tests & `integration/test_finance.py`)
*   **FX Rate Service & Triangulation**:
    *   Assert that converting same-currency rates resolves to `1.0`.
    *   Assert direct rate conversion lookup works.
    *   Assert triangulation conversion (e.g., base currency to quote currency via USD base intermediary) resolves correctly.
    *   Assert correct error propagation when conversion components are missing.
*   **FX Rate Ingestion Job**:
    *   Fetch latest exchange rates from open exchange rates provider, parse, validate, and commit to the database.
    *   Verify ingestion rejects non-positive rates.
    *   Verify errors are caught, handled, and logged during network or parser failure.
*   **Transfers Validation**:
    *   Transfer operations verify that the amount debited from one account matches the amount credited to another when adjusted by active exchange rates.

---

### 💵 Spending & Category Operations (`integration/test_spending.py`)
*   **Default Categories**:
    *   Validates that 8 default system categories are seeded upon registration.
*   **Category Constraints**:
    *   Deleting system categories is prohibited.
    *   Deleting categories that contain logged transactions is prohibited.
    *   Deleting categories that are tied to active budgets is prohibited.
    *   Duplicate category name creation within the same workspace is rejected (case-insensitively).
*   **Budget Constraints**:
    *   Ensures that only one budget is created per category, per month. Attempting to create duplicate budgets returns a `409 Conflict` prompting the client to use `PATCH`.
*   **Transactions Summary**:
    *   Verifies that transactions summaries accurately compute expense totals, income totals, net totals, and breakdown metrics using full month data.

---

### 📂 Bulk Imports Lifecycle (`integration/test_imports.py`)
*   **Atomic Imports**:
    *   Importing a batch of rows is atomic. If a single row fails validation, the entire batch rolls back.
*   **Automatic Provisioning**:
    *   Importing records containing category names that do not exist in the workspace automatically provisions custom categories.
*   **Local Object Storage**:
    *   Validates generated file name keys and storage cleanups.
*   **Import Deletion & Rollback**:
    *   Deleting a completed import batch performs a clean cascading rollback, deleting all transactions, budgets, or holdings generated by that import batch.

---

### 📈 Investing & Look-through Analytics (`integration/test_investing.py`)
*   **Holdings Constraints**:
    *   Price submission rejects unrealistic unit prices or large batches.
    *   Duplicate holdings within the same account are blocked.
*   **Portfolio Valuations**:
    *   Computes direct and look-through asset exposures.
    *   Validates constituent weights for funds and look-through exposure overlap calculations.

---

### 🔔 Notifications & User Preferences (`integration/test_notifications.py` & `notifications/` tests)
*   Verify unread notification count queries.
*   Verify listing notification preferences.
*   Verify toggling of notification delivery options.
*   Verify that marking all notifications as read returns the count of updated records.

---

### 📋 Audit Logging Immutability (`test_audit_logging.py` & `integration/test_spending_audit.py`)
*   **PII & Secrets Redaction**:
    *   Sensitive details (passwords, tokens, API keys, account numbers) are recursively redacted before being written to the database.
*   **Contract Validation**:
    *   Verifies event contract schemas, actions (`create` requires before state to be null; `update` requires both states to be non-null).
*   **Immutability Enforcement**:
    *   Verify that any attempt to update or delete rows in the `audit_logs` database table raises database-level exceptions (enforced via DB triggers).

---

### ⏰ Scheduler & Guardrails Background Jobs (`test_scheduler.py`)
*   **Scheduler Gating**:
    *   Verify the scheduler is disabled in testing environments unless explicitly overridden.
*   **Budget Guardrails State Machine**:
    *   A. Spend under threshold: no todo created.
    *   B. Spend reaches warning threshold (>=90%): warning todo created, audit log written.
    *   C. Re-evaluation under same conditions: no duplicate todo or audit logs (idempotency).
    *   D. Spend reaches critical threshold (>=100%): warning todo updated to critical in place, audit log updated.
    *   E. Spend drops below threshold: todo is automatically resolved and marked as completed.
*   **Isolation**:
    *   Verify that guardrail evaluations are isolated. A breach in one workspace does not create tasks or leaks to other workspaces.
    *   Verify per-workspace failure isolation: if a workspace evaluation crashes, other workspaces are still evaluated successfully.
