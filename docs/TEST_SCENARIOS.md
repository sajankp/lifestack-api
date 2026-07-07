# LifeStack Test Scenarios

Last verified: 2026-06-28

This document is the current test coverage and missing-test backlog for the
LifeStack workspace. It is based on the live test files in:

- `lifestack-e2e/e2e/`
- `lifestack-api/app/tests/`
- `lifestack-web/src/**/*.test.ts(x)`

It intentionally replaces the older scenario inventory that had gone stale.

## Test Layers

| Layer | Location | Runner | Purpose |
|---|---|---|---|
| Full-stack E2E | `lifestack-e2e/e2e/` | Playwright | Browser journeys against the Dockerized web/API/Postgres/Redis stack. |
| Backend integration and unit | `lifestack-api/app/tests/` | pytest | API contracts, domain workflows, auth/RBAC, DB integrity, jobs, and service units. |
| Frontend unit/component | `lifestack-web/src/**/*.test.ts(x)` | Vitest | React pages/components, service clients, auth store, CSRF/refresh behavior, and UI lifecycle controls. |

## Current Test Commands

### Full-Stack E2E

From `lifestack-e2e/`:

```bash
npm run stack:up
npm run test:smoke
npm run test:full
npm run stack:down
```

Single-command stack orchestration is also available:

```bash
npm run test:smoke:stack
npm run test:full:stack
```

The E2E stack uses:

- Web: `http://localhost:5174`
- API: `http://localhost:8001`
- Postgres: `localhost:5433`
- Redis: `localhost:6381`

The E2E harness enables local-only authenticated HTTP hooks for background
workflows. E2E specs should use those hooks instead of shelling into containers.
The weekly-summary hook returns the generated summary id and week range so tests
can assert the exact workflow result instead of treating the hook as fire-and-forget.

### Backend

From `lifestack-api/`:

```bash
pytest
ruff check .
ruff format --check .
```

### Frontend

From `lifestack-web/`:

```bash
npm run test
npm run test:coverage
npm run lint
npm run build
npm run security:audit
```

## Current Full-Stack E2E Coverage

The Playwright suite currently has 38 tests across 18 spec files.

| Spec | Current coverage |
|---|---|
| `app-shell-responsive.spec.ts` | Tablet-width app shell behavior: mobile drawer navigation, header notifications, profile menu, and logout. |
| `auth.spec.ts` | Register, login, dashboard access, logout, and logged-out protected-route redirect. |
| `capture.spec.ts` | Verify viewer role is blocked from connecting, member receives provider unavailable alert when API key is missing, and mock WebSocket session handles user text, agent transcript, tool executions, and client errors correctly. |
| `keyboard-accessibility.spec.ts` | Verify keyboard-only sidebar navigation into Todo, Todo creation via Enter, Todo completion via Space on the row control, and Spending category modal creation by keyboard. |
| `todo-smoke.spec.ts` | Create and complete a todo. |
| `transfer-flow.spec.ts` | Create same-currency and cross-currency account transfers through the UI, verify account/module/gross/net/FX metadata in transfer history, and verify invalid transfer arithmetic is rejected by the API. |
| `imports-smoke.spec.ts` | Upload a generated spending CSV, validate preview rows, commit the import, verify completed status, roll back a completed spending import from the UI, and verify generated transactions are removed. |
| `exports.spec.ts` | Create/download JSON export, create/download CSV ZIP export, and create/download/delete an export through the UI while verifying deleted artifacts no longer download. |
| `guided-empty-states.spec.ts` | Verify first-run dashboard, todo, spending, investing, imports, exports, notifications, summaries, and settings empty states plus primary start controls. |
| `spending-guardrails.spec.ts` | Create category, create budget, log threshold-breaching transaction, trigger guardrail hook over HTTP, verify warning todo, create a wallet/account, log spending against it, and verify transaction-row account context. |
| `spending-recurring.spec.ts` | Create/edit recurring spending rule, trigger recurring generation hook over HTTP, verify generated transaction, and deactivate rule. |
| `investing-fx.spec.ts` | Create GBP/USD brokerage accounts, add holdings, verify unconverted multi-currency state before reporting currency setup, switch reporting currency, verify displayed FX-rate metadata, valuation, and look-through analytics. |
| `investing-orders.spec.ts` | Place a buy order and verify holding creation, place a second buy and verify cost basis, place a sell and verify realized gain/loss, reject a buy on insufficient cash, delete an order and recompute the holding, show per-holding trade history, and verify a transfer-triggered brokerage cash balance entry. ⚠️ Needs review against spec-044 (FIFO lots replaced weighted avg_cost) and spec-048 (Orders now lives under the unified Cash tab). |
| `finance-display-settings.spec.ts` | Verify workspace display settings and user override precedence on dashboard totals. |
| `notifications-summaries.spec.ts` | Trigger a weekly summary through the E2E hook, assert the generated summary id/week response, verify the generated unread notification and header badge, mark notifications read, verify the Weekly Summaries page renders the generated summary, and verify notifications/summaries stay isolated when switching between personal and shared workspaces. |
| `runtime-header-master-config.spec.ts` | Verify global header controls and Master Configuration account/category edit actions. |
| `rbac.spec.ts` | Verify viewer mutation rejection, member todo CRUD, viewer finance-settings rejection, and owner finance-settings update. |
| `workspace-isolation.spec.ts` | Verify an invited user can switch between personal and shared workspaces, sees workspace-specific Todo, spending transaction, and investing holding data in the UI, and cannot API-fetch/mutate Todo, spending, investing, import, or export records from a non-active workspace. |

### Smoke Tier

The smoke tier is selected with `@smoke` and currently includes:

- Auth happy path.
- Todo create/complete.
- Spending import validate/commit.
- Spending budget guardrail todo generation.
- JSON export request/download.

### Full Tier

The full tier runs all Playwright specs, including smoke plus:

- CSV ZIP export.
- Tablet responsive app shell navigation, profile menu, notifications, and logout.
- Keyboard-only sidebar navigation and Todo creation/completion.
- Same-currency and cross-currency transfer creation plus invalid transfer arithmetic rejection.
- Export create/download/delete lifecycle through the UI.
- Guided first-run empty states and primary start controls across core modules.
- Completed spending import rollback from the UI.
- Voice agent connection block for viewer, missing API key error alerts, and mock WebSocket session interactions (transcripts, tool execution status, client errors).
- Account-linked spending transaction creation and row context.
- Spending recurring generation.
- Investing unconverted multi-currency state, FX conversion, displayed FX rate metadata, visible original currency mix, and look-through.
- Finance display settings.
- Generated weekly summary hook response, notification, unread badge, mark-all-read behavior, and weekly summary rendering.
- Runtime header and Master Configuration edits.
- Workspace RBAC checks.
- Workspace switch isolation for Todo, spending, and investing UI, and API lookup/mutation boundaries for Todo, spending, investing, imports, and exports.

## Current Backend Coverage

### API Contracts and Error Format

Files:

- `integration/test_api_contracts.py`
- `test_errors.py`
- `integration/test_api.py`

Coverage:

- Auth, spending category/recurring, investing selector, and finance settings response contracts.
- RFC 7807-style error envelopes for validation, auth, duplicate registration, rate limits, and unhandled exceptions.
- CSRF enforcement for cookie-authenticated mutations.
- Basic health, docs, OpenAPI, registration/login/logout, and protected route checks.

### Auth, Session, Security, and Config

Files:

- `test_auth_rbac_security.py`
- `test_security_hardening.py`
- `test_config.py`
- `test_middleware.py`
- `test_main.py`

Coverage:

- Password complexity for registration and password changes.
- Password change session revocation and cookie clearing.
- Logout-all session invalidation.
- Refresh token rotation, retry grace, and replay detection.
- Inactive user and inactive workspace access blocking.
- Malformed bearer token rejection.
- Trusted-proxy behavior for forwarded protocol handling.
- Request ID, HSTS, CSP, and XSS protection headers.
- Production config fail-closed checks.
- Multipart upload size limiting and response-state safety.

### Platform, Workspace, and RBAC

Files:

- `integration/test_platform.py`
- `integration/test_workspace_resolution.py`
- `platform/test_service.py`
- `test_platform_router.py`

Coverage:

- Registration provisioning and rollback.
- Workspace listing with real name and role.
- Workspace member invitation and role restrictions.
- Workspace selection and refresh persistence.
- Non-member workspace selection rejection.
- Inactive workspace and inactive user membership restrictions.
- Demo reset targeting the active workspace.
- Default workspace claim normalization.

### Todo

Files:

- `routers/test_todo_router.py`
- `routers/test_todo_audit.py`
- `todo/test_service.py`
- `application/test_recurring_todo_workflow.py`

Coverage:

- Workspace-scoped todo CRUD.
- Todo service unit behavior.
- Todo audit logging.
- Recurring todo rule CRUD and recurring todo generation.

### Spending, Budgets, Recurring, and Guardrails

Files:

- `integration/test_spending.py`
- `routers/test_spending_router.py`
- `integration/test_spending_audit.py`
- `application/test_budget_guardrails.py`
- `application/test_recurring_workflow.py`
- `test_scheduler.py`

Coverage:

- Default category seeding.
- Workspace isolation for categories, accounts, and transactions.
- System category deletion protection.
- Category deletion blocked when transactions, budgets, or recurring rules reference it.
- Case-insensitive duplicate category rejection.
- Budget uniqueness and patch update behavior.
- Month summary totals.
- Manual transaction source metadata.
- Spending audit logging.
- Recurring transaction generation, catch-up, idempotency, end-date deactivation, and workspace isolation.
- Budget guardrail state machine, idempotency, threshold transitions, cross-workspace isolation, and per-workspace failure isolation.

### Finance, Currency, FX, Accounts, and Transfers

Files:

- `finance/test_fx_rate_service.py`
- `finance/test_fx_ingestion.py`
- `integration/test_finance.py`

Coverage:

- Same-currency, direct, and triangulated FX rate resolution.
- Missing FX component errors.
- FX upsert validation and currency validation caching.
- FX ingestion success and provider failure cases.
- Non-positive rate rejection.
- Finance account CRUD and validation.
- Workspace isolation for accounts.
- Finance user override currency validation.
- Deletion restrictions for accounts in use.
- Transfer arithmetic validation and same-currency FX enforcement.
- Tenant-safe account FKs for spending transactions and capital transfers.

### Investing

Files:

- `integration/test_investing.py`
- `test_investing_schema.py`
- `investing/tests/test_order_service.py`

Coverage:

- Investing account, holding, summary, cash balance, and audit flows.
- Workspace isolation.
- Unrealistic price and large batch rejection.
- Duplicate holding conflicts.
- Multi-currency summary conversion.
- Performance summary conversion.
- Look-through exposure and overlap analytics.
- Constituent weight validation and workspace isolation.
- Portfolio snapshot latest-query index and FX-rate validation.
- Buy/sell order placement: brokerage-account validation, automatic cash-balance debit/credit, insufficient-cash and oversell rejection, FIFO lot-based cost basis and realized gain/loss (spec-044), fee capitalization into book value (spec-046), holding recompute on order delete, and bulk order import.

### Imports

Files:

- `integration/test_imports.py`
- `application/test_cleanup_jobs.py`

Coverage:

- Oversized multipart rejection before parsing.
- Fail-all import behavior when one row is invalid.
- Spending transaction and spending budget imports.
- UTF-8 BOM CSV handling.
- Template download attachment behavior.
- Generated storage object keys instead of user-controlled filenames.
- Spendee CSV wallet/label parsing.
- Auto-created category reporting.
- Import delete lifecycle for validated imports.
- Completed import rollback for spending transactions, spending budgets, and investing holdings.
- Import workspace isolation (get, commit, delete, and list API actions).
- Import preview cleanup job.

### Exports

Files:

- `integration/test_exports.py`
- `exports/test_repository.py`
- `exports/test_service.py`

Coverage:

- Export create/get/download/delete lifecycle.
- Pending export delete conflict.
- Local backend lifecycle.
- Export workspace isolation (get, download, and delete API actions).
- Cleanup workflow.
- Repository lifecycle.
- Service validation for invalid modules, empty module lists, pending conflicts, limits, JSON generation, local backend, and S3 backend.

### Notifications, Summaries, Dashboard, Capture, and Cross-Module Flows

Files:

- `integration/test_notifications.py`
- `notifications/test_notifications.py`
- `integration/test_summaries.py`
- `summaries/test_weekly_summary.py`
- `dashboard/test_dashboard_service.py`
- `routers/test_dashboard.py`
- `capture/test_agent.py`
- `capture/test_capture_router.py`
- `integration/test_capture.py`
- `integration/test_cross_module_flows.py`

Coverage:

- Notification listing, preferences, unread counts, read/dismiss behavior, pagination, filtering, auth requirements, and workspace isolation.
- Weekly summary service, endpoints, job, pagination, date ranges, latest/get behavior, auth requirements, and workspace isolation.
- Dashboard empty/data summaries and workspace isolation.
- Capture route registration, OpenAPI tagging, WebSocket auth, role restriction, and HTTP upgrade auth behavior.
- Agent tool execution for creating todos, logging spending, logging cash balances, error handling, frame/session/text limits, session expiration, and sanitized provider errors.
- Cross-module checks across notifications, capture, summaries, analytics, and investing performance.

### Jobs and Cleanup

Files:

- `test_scheduler.py`
- `application/test_cleanup_jobs.py`
- `application/test_workflows.py`

Coverage:

- Scheduler gating and registration.
- Non-idempotent scheduler jobs blocked by default.
- Session cleanup and session limit eviction.
- Import preview cleanup.
- Registration workflow creates user/workspace/membership/default data atomically.

### Audit Logging

Files:

- `test_audit_logging.py`
- `integration/test_spending_audit.py`
- `routers/test_todo_audit.py`

Coverage:

- Sensitive detail redaction.
- Audit event contract validation.
- Create/update action shape rules.
- Transactional commit and rollback behavior.
- Database-level audit log immutability.
- Spending and todo audit logging.

## Current Frontend Unit and Component Coverage

| Area | Files | Current coverage |
|---|---|---|
| App shell | `App.test.tsx` | Mobile navigation opens/closes without losing the active route, and the header workspace selector posts the selected workspace and updates active workspace state. |
| API client and auth | `services/api.test.ts`, `services/auth.test.ts`, `store/authStore.test.ts` | Credentials, CSRF header mirroring, refresh retry, concurrent 401 coalescing, unauthorized callbacks, auth service calls, and auth store state transitions. |
| Imports | `pages/ImportsPage.test.tsx`, `services/imports.test.ts` | Page rendering, file-size validation, non-CSV rejection, successful upload, import delete/rollback action, and import service endpoint calls. |
| Exports | `pages/ExportsPage.test.tsx`, `services/exports.test.ts` | Export creation, lifecycle controls, ready export deletion, invalid date fallback, and service endpoint calls. |
| Dashboard | `pages/DashboardPage.test.tsx` | Budget remaining display with and without a current-month budget. |
| Investing | `pages/InvestingPage.test.tsx`, `services/investing.test.ts` | Multi-currency unconverted summary state, account/holding submission, look-through analytics tab, and investing service endpoint calls. |
| Master Configuration | `pages/MasterConfigPage.test.tsx` | Active-workspace demo reset targeting and disabled reset reason rendering. |
| Spending | `services/spending.test.ts` | Category, transaction, budget, trend, and recurring service endpoint calls. |
| Voice/capture UI | `components/VoiceAgentFailureAlert.test.tsx` | Failed voice session recovery actions. |

## Known Gaps and Recommended Test Cases

These are not a claim that the product is broken. They are the highest-value
missing tests based on the current suite shape.

### P0: Release Confidence Gaps

1. Accessibility and keyboard breadth
   - Extend current keyboard-only coverage beyond sidebar, Todo, and a Spending modal submit into date pickers, file upload, and remaining critical submit buttons.
   - Add an accessibility scan for dashboard, spending, investing, imports, exports, and settings.

### P1: Product-Quality Gaps

2. Finance valuation metadata breadth
   - Current E2E covers visible reporting currency, original currency mix, unconverted state, conversion availability, and displayed FX rate values.
   - Add tests for FX date/source only after those fields are exposed in the relevant UI surfaces.

3. Notifications and summaries workspace isolation breadth
   - Current E2E verifies unread counts, mark-all-read behavior, and weekly summary lists while switching between personal and shared workspaces.
   - Extend this further to include notification pagination/filter state and latest-summary dashboard cards across workspaces.

### P2: Maintainability and Future-Readiness Gaps

6. Frontend decomposition regression tests
   - As large pages are split, add component tests for shared page header, filter bar, modal shells, empty states, and amount/currency display helpers.
   - Keep service tests focused on request contracts and page tests focused on user-visible behavior.

7. Export history/listing tests if a backend listing endpoint is added
   - API: list exports by workspace, status, and created date.
   - Web: show historical exports with download/delete controls.
   - E2E: verify one workspace cannot see another workspace's export history.

8. Future source metadata convention tests
   - When synced, extracted, assistant-created, health, or document-backed records exist, verify they expose consistent source metadata.
   - Verify the UI shows provenance without exposing internal storage keys.

9. Production runbook verification
   - Add scriptable checks for backup/restore drill documentation, security monitoring hooks, and deployment readiness.
   - Keep these separate from normal fast CI unless they become automated pre-release gates.

## Suggested Gate Matrix

| Gate | Required checks |
|---|---|
| Local feature branch | Relevant API pytest subset, relevant Vitest subset, lint/format for touched repo. |
| PR | Full API pytest, web test/coverage/lint/build, E2E smoke stack, dependency audit gates. |
| Pre-release | Full E2E stack, accessibility/responsive checks, workspace isolation breadth, import/export lifecycle, and finance/account smoke. |
| Nightly | Full E2E, broader browser matrix if needed, cleanup jobs, long-running import/export cases, and production-hardening script checks. |

## Documentation Maintenance Rule

Update this file whenever any of these change:

- A new E2E spec is added, removed, or reclassified as smoke/full.
- Background workflow triggering changes.
- A backend test module is added for a new product area.
- A frontend page/service/component gains meaningful test coverage.
- A recommended missing scenario is implemented and should move into current coverage.
