# Feature Spec: Spending Module
**Status:** Approved
**Spec ID:** 003

## 1. Overview
The Spending module is the financial domain for Lifestack. It should let an authenticated user record income and expenses, organize them into workspace-scoped categories, and define monthly category budgets.

This spec is intentionally aligned to the current architecture:
- the module follows the standard `models` -> `schemas` -> `repository` -> `service` -> `router` shape
- all business data is scoped by `workspace_id`
- external APIs use `public_id` UUIDs rather than internal integer PKs
- cross-module reactions belong in `app/application/`, not inside spending services
- missing resources and validation failures return RFC 7807 problem details

Current codebase note:
- `spending` is planned in the architecture but is not implemented yet
- registration currently provisions a default workspace only; it does not yet seed spending categories

## 2. Goals
- Track income and expense transactions for the active workspace.
- Organize transactions using workspace-scoped categories.
- Set one budget per category per month per workspace.
- Provide a clean base for dashboard read models and future workflows such as overspending alerts.

## 3. Out of Scope for This Slice
- recurring transactions
- analytics/materialized summaries
- category sharing across workspaces
- multi-currency support
- grouped budgets spanning multiple categories
- AI-generated categorization
- notification delivery for overspending

Those can be layered on later without changing the core workspace-scoped model.

## 4. Architecture Alignment

### 4.1 Module Boundary
The spending module should own only spending-domain logic:
- categories
- transactions
- budgets

It must not:
- resolve authentication directly
- derive workspace state from raw request objects
- call todo or investing services directly for orchestration

If spending events need downstream actions, wire them through an application workflow.

### 4.2 Workspace Contract
- Every spending table must carry `workspace_id` as a required foreign key to `workspaces.id`.
- Repositories must scope every read and mutation by `workspace_id`.
- `user_id` on transactions is creator/audit metadata only; it is not the access-control boundary.
- Stage 1 request handling must resolve the active workspace through the shared dependency pattern already used by `todo`.

### 4.3 Identifier Contract
- Internal tables use integer primary keys.
- API routes and response payloads expose `public_id` UUIDs.
- Route params in this module should use `{public_id}` semantics even if the path segment is named generically.

## 5. Requirements

### 5.1 Data Model

#### Category
- `id`: internal PK
- `public_id`: external UUID
- `workspace_id`: tenant FK
- `name`: category label, unique per workspace, case-insensitive by policy
- `is_system`: `true` for default categories provisioned for that workspace
- `color`: optional presentation hint
- `icon`: optional presentation hint
- `created_at`, `updated_at`

Constraints:
- unique `(workspace_id, normalized_name)`
- categories are never shared across workspaces
- avoid a global `workspace_id = null` category pool because it weakens the strict-isolation model documented in architecture

#### Transaction
- `id`: internal PK
- `public_id`: external UUID
- `workspace_id`: tenant FK
- `user_id`: creator FK
- `amount`: `NUMERIC`/`Decimal`
- `occurred_at`: timezone-aware timestamp for when the transaction happened
- `description`: optional notes
- `category_id`: FK to a category in the same workspace
- `type`: enum with `income` and `expense`
- `created_at`, `updated_at`

Constraints:
- transaction amount should be positive; direction comes from `type`
- transaction/category linkage must be validated within the same workspace

#### Budget
- `id`: internal PK
- `public_id`: external UUID
- `workspace_id`: tenant FK
- `category_id`: FK to a category in the same workspace
- `amount`: monthly target stored as `NUMERIC`/`Decimal`
- `month_start`: date representing the first day of the covered month
- `created_at`, `updated_at`

Constraints:
- unique `(workspace_id, category_id, month_start)`

Phase 1 budget rule:
- each budget applies to exactly one category for one month
- grouped budgets such as "monthly discretionary spend across several categories" are a future extension, not part of this initial module contract
- include/exclude category rules should not be added until the team defines overlap and double-counting behavior explicitly

### 5.2 API Surface

#### Categories
- `GET /v1/spending/categories`
- `POST /v1/spending/categories`
- `PATCH /v1/spending/categories/{public_id}`
- `DELETE /v1/spending/categories/{public_id}`

Rules:
- list only categories for the active workspace
- delete is allowed only for custom categories
- system categories may be patched for cosmetic/user-label fields (`name`, `color`, `icon`) but cannot be deleted
- deleting a category that is referenced by transactions should fail with a documented problem response unless a reassignment flow is explicitly added
- category delete conflicts use RFC 7807 with conflict status and a module-specific type (for example `https://lifestack.app/errors/category-in-use`)

#### Transactions
- `GET /v1/spending/transactions`
- `POST /v1/spending/transactions`
- `GET /v1/spending/transactions/{public_id}`
- `PATCH /v1/spending/transactions/{public_id}`
- `DELETE /v1/spending/transactions/{public_id}`

Supported filters for list:
- category
- date range
- type

#### Budgets
- `GET /v1/spending/budgets`
- `POST /v1/spending/budgets`
- `PATCH /v1/spending/budgets/{public_id}`

Rules:
- `POST /v1/spending/budgets` creates a new budget; it must reject the request with an RFC 7807 conflict response if a budget for the same `(workspace_id, category_id, month_start)` already exists.
- `PATCH /v1/spending/budgets/{public_id}` updates the `amount` of an existing budget.
- Clients are expected to check whether a budget exists before deciding to create or update. This is the explicit split model, not upsert.

## 6. Implementation Guidance

### 6.1 Currency Handling
Use `Decimal` persisted as PostgreSQL `NUMERIC(12, 2)`.

Rationale:
- 2 decimal places covers standard currency precision for a personal finance tracker.
- `NUMERIC(12, 2)` supports amounts up to 9,999,999,999.99 which is sufficient.
- Pydantic schemas must validate that submitted amounts have at most 2 decimal places.
- Pydantic schemas must reject zero and negative values (`amount > 0`).
- Arithmetic (e.g. budget vs actual comparisons) should use `Decimal` throughout to avoid floating-point errors.

### 6.2 Provisioning Default Categories
Default spending categories are seeded **during user registration**, at the same time the default workspace is created.

The registration workflow must:
1. Create the `users` row.
2. Create the default `workspaces` row.
3. Create the `workspace_memberships` row.
4. Insert a fixed set of system categories (`is_system = true`) scoped to that workspace.

This is an atomic operation — all four steps should succeed or the registration fails.

Default system categories (illustrative, finalize before implementation):
- Food & Dining
- Transport
- Housing
- Health
- Entertainment
- Shopping
- Income
- Other

Do not use globally shared categories with `workspace_id = null`. Every category row must belong to exactly one workspace.

Implementation note:
- This provisioning must be orchestrated by an application workflow (`app/application/`), not by auth/router logic directly.
- Registration remains the entrypoint, but cross-module seeding belongs in workflow orchestration boundaries.

### 6.3 Cross-Module Workflows
Budget checks, alerts, and todo follow-ups are not part of the spending service layer itself.

If overspending should create a todo or dashboard signal, define that in `app/application/` after the core spending CRUD contract is stable.

## 7. Acceptance Criteria
- Add spending module files for `models`, `schemas`, `repository`, `service`, and `router` following the same layering used by `todo`.
- Ensure every spending repository method scopes by `workspace_id`.
- Expose `public_id` in responses and use workspace-scoped lookups for all single-resource routes.
- Reject cross-workspace category references when creating or updating transactions and budgets.
- Return RFC 7807 responses for not-found, validation, and forbidden business-rule cases.
- Keep transaction ownership above the repository layer; repositories should `flush()` rather than `commit()`.
- Document and test the chosen default-category provisioning behavior.

## 8. Observability Hooks
- Emit structured log events for category/transaction/budget create-update-delete actions with `workspace_id` and `public_id`.
- Add module metrics counters for transaction and budget mutation outcomes.
- Ensure trace spans include spending service operations for list/create/update/delete paths.

## 9. Required Integration Scenarios

### 8.1 Workspace Isolation for Categories
- setup: two users with distinct workspaces and categories of the same name
- actor: user A
- request: list categories and fetch category detail
- expected DB effect: none
- expected API response: only workspace A categories are returned
- isolation assertion: user A cannot fetch user B's category by `public_id`

### 8.2 Cross-Workspace Category Rejection
- setup: user A and user B each have a workspace; user B owns category X
- actor: user A
- request: create transaction using category X `public_id`
- expected DB effect: no transaction is inserted
- expected API response: RFC 7807 problem response
- isolation assertion: foreign category references are rejected even if the UUID exists

### 8.3 Budget Uniqueness
- setup: one workspace, one category, existing budget for a month
- actor: authenticated workspace member
- request: create or upsert another budget for the same category and month
- expected DB effect: exactly one row remains for that uniqueness boundary
- expected API response: matches documented create-vs-upsert semantics
- transaction assertion: uniqueness is enforced by schema and service behavior together

### 8.4 Category Provisioning
- setup: newly registered user with a default workspace
- actor: authenticated user
- request: first relevant spending flow based on the chosen provisioning design
- expected DB effect: default categories exist for that workspace only
- expected API response: categories are available without leaking from other workspaces
- isolation assertion: no shared global category records are relied on

## 10. Required E2E Scenarios
- Register, log in, create a category, create a transaction, and see it listed in the same workspace.
- Attempt to open or mutate another user's spending resource and verify failure.
- Create a monthly budget and confirm the UI/API shows the stored month/category combination consistently.
- Verify problem-detail responses are surfaced cleanly for missing category or transaction URLs.

## 11. Settled Decisions

| # | Decision | Resolution |
|---|----------|------------|
| 1 | Budget write model | Split `POST` (create) + `PATCH` (update). `POST` rejects duplicates with RFC 7807 conflict. |
| 2 | Default category provisioning | Seeded atomically during registration alongside workspace creation. |
| 3 | Money field precision | `NUMERIC(12, 2)` — 2 decimal places, validated in Pydantic schemas. |
