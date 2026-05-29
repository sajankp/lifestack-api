# Spec 024 - Phase 1 Runtime API Integration Contract (Investing-First)

**Status:** Proposed

## 1) Problem
Phase 1 feature specs exist, but the app runtime still depends on cross-module API contracts that are not explicitly captured as a single "must-work-together" integration spec.
This causes drift between backend readiness and frontend/e2e runtime expectations, especially for Investing where summary, performance, FX/currency, transfers, and account selection are tightly coupled.

## 2) Goal
Define the minimum backend API integration contract required for the Lifestack app to run reliably in Phase 1, with Investing called out as a first-class runtime surface.

## 3) Scope
### In Scope (Phase 1)
- Contract-level integration requirements across:
  - auth/session
  - workspace-scoped finance settings
  - spending/investing/todo/dashboard/notifications/summaries
  - imports needed for onboarding data
- Explicit required endpoint set and response guarantees for Investing runtime UX.
- Phase-1 required integration tests and e2e handshake points.

### Out of Scope
- New product features beyond approved specs.
- Broker integrations, real-time tick feeds, advanced accounting, or reconciliation engines.
- V2 ledger redesign concerns.

## 4) Runtime Integration Contract

### 4.1 Session + Workspace Context (Required)
- `POST /v1/auth/login`
- `POST /v1/auth/logout`
- `GET /v1/auth/me`
- Refresh/session validity flow must keep SPA authenticated without token-in-local-storage usage.
- All domain endpoints must enforce workspace isolation from active session context.

### 4.2 Finance Baseline (Required for Investing + Spending UX)
- `GET /v1/finance/settings`
- `PATCH /v1/finance/settings`
- `GET /v1/finance/currencies`
- `GET /v1/finance/accounts`
- `POST /v1/finance/accounts`
- `PATCH /v1/finance/accounts/{public_id}`
- `GET /v1/finance/fx-rates`
- `GET /v1/finance/transfers`
- `GET /v1/finance/transfers/{public_id}`
- `POST /v1/finance/transfers`

Contract rules:
- `reporting_currency_code` must be nullable and stable.
- account types required for runtime: `bank`, `wallet`, `card`, `gift_card`, `brokerage`.
- transfer responses must include enough metadata for UI history and FX/fee display.

### 4.3 Investing Runtime (Required)
- `GET /v1/investing/holdings`
- `POST /v1/investing/holdings`
- `PATCH /v1/investing/holdings/{public_id}`
- `DELETE /v1/investing/holdings/{public_id}`
- `GET /v1/investing/cash-balances`
- `POST /v1/investing/cash-balances`
- `PATCH /v1/investing/cash-balances/{public_id}`
- `DELETE /v1/investing/cash-balances/{public_id}`
- `GET /v1/investing/summary`
- `GET /v1/investing/performance/summary`
- `GET /v1/investing/performance/history`

Contract rules:
- Decimal values serialize as strings.
- Summary must include valuation status semantics from Spec 011.
- Multi-currency portfolios must degrade gracefully (`N/A`/status metadata) when conversion is unavailable.
- Performance endpoints must not break summary-page rendering when historical snapshots are absent.

### 4.4 Dashboard + Operational Surfaces (Required)
- `GET /v1/dashboard/summary`
- `GET /v1/notifications`
- `GET /v1/notifications/unread-count`
- `POST /v1/notifications/mark-all-read`
- `PATCH /v1/notifications/{id}/read`
- `GET /v1/summaries/weekly/latest`

Contract rules:
- Dashboard summary must be live backend-derived data (not frontend-computed placeholders except explicitly derived display metrics).
- Unread notification count must be available for global header usage.

### 4.5 Spending + Todo Runtime Dependencies (Required)
- Spending transactions must support optional `account_id` and include `wallet_name`/`labels` compatibility fields.
- Recurring spending and recurring todo endpoints must be stable for scheduler-backed UI flows.

### 4.6 Import Contract (Required for onboarding)
- `POST /v1/imports` (multipart form: `module` + `file`)
- `POST /v1/imports/{batch_public_id}/commit`
- `GET /v1/imports/{batch_public_id}`
- `GET /v1/imports/{batch_public_id}/errors`

Contract rules:
- fail-all-on-error semantics for Phase 1 commit.
- validation errors must be RFC7807-compatible and field-structured.

## 5) Non-Functional Requirements
- All endpoints above must be tenant-safe and audited where mutations occur.
- Standardized error shapes (RFC7807) across all modules.
- No cross-module hidden side effects outside explicit workflows.
- Reasonable pagination defaults + limits on list endpoints used by UI.

## 6) Acceptance Criteria
1. App can cold-start from login to dashboard with no missing required endpoint.
2. Investing page can:
   - list/create/update/delete holdings and cash balances,
   - render summary and performance sections without runtime errors,
   - handle multi-currency states predictably.
3. Spending page can link wallet/account and display transfer history via finance APIs.
4. Global notification badge works from unread-count endpoint on all protected pages.
5. CSV onboarding flow works end-to-end with validate -> commit path and clear errors.

## 7) Test Plan (Phase 1 Gate)
### Backend
- Integration suite covering each required endpoint family above.
- Contract tests for decimal serialization, nullable fields, and enum stability.
- Workspace isolation tests on all list/get/mutation paths.

### Frontend
- Runtime integration tests for:
  - dashboard bootstrap
  - investing summary/performance render path
  - spending wallet/account + transfer listing
  - global notification badge

### E2E
- Smoke scenario:
  - login -> dashboard -> spending -> investing -> notifications -> imports
  - verifies API handshake and core page render readiness.

## 8) Rollout / Ownership
1. Lock this contract in docs (this spec).
2. Map each required endpoint to implementation/test status in CI checklist.
3. Keep this as the release-readiness reference for Phase 1 runtime stability.
