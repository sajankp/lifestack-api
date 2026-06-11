# Feature Spec: Dashboard Read Model
**Status:** Implemented
**Spec ID:** 007

Implementation note (2026-06-11): `GET /v1/dashboard/summary` is implemented through `app/dashboard` and `DashboardSummaryWorkflow`, with integration tests covering authenticated shape, empty-state defaults, workspace isolation, and cross-module summary behavior.

## 1. Overview
Dashboard is a read model that aggregates key workspace state across modules without duplicating domain business rules.

## 2. Goals
- Provide one low-latency endpoint for FE dashboard boot.
- Aggregate todo, spending, and investing summaries.
- Keep dashboard strictly read-only.

## 3. Out of Scope
- Write/mutation endpoints in dashboard module.
- Long-term analytics warehouse.
- Personalized ML ranking.

## 4. API Surface
- `GET /v1/dashboard/summary`

Response sections:
- `todos`: open count, overdue count, next due items, active guardrail todo count.
- `spending`: month spent, month budget, top overspent categories.
- `investing`: portfolio value, `daily_change` (always `null` in V1), holdings count.
- `system`: generated_at timestamp.

## 5. Data and Query Contract
- Dashboard service composes module services in `app/dashboard/service.py`.
- **SQL Aggregation Mandate:** Services MUST NOT fetch full lists of ORM objects into memory for aggregation. Counts and sums must be pushed down to the database level (e.g., via `func.count()`, `func.sum()`) to guarantee stable performance.
- All queries are workspace-scoped.
- Missing module data returns empty-safe sections rather than hard failure.
- V1 caching policy: no server-side caching; responses are computed from live reads.

## 6. Error Handling
- Returns RFC 7807 for auth/tenancy failures.
- Partial module failures are logged; response should remain best-effort for non-critical sections in Stage 1.
- Partial failure response contract:
  - failed section returns `{ "status": "unavailable" }`
  - successful sections still return normal payloads
  - top-level response remains `200` for partial non-critical degradation

## 7. Performance Targets
- P50 under 150ms for typical personal workspace.
- P95 under 400ms with moderate data volume.
- CI should enforce a coarse integration threshold (`<500ms`) for baseline regressions; tighter P50/P95 is validated in staging load checks.

## 8. Test Plan
- Integration test for authenticated summary response shape.
- Workspace isolation test with two users.
- Contract test for empty workspace defaults.

## 9. Acceptance Criteria
- Endpoint returns stable typed response for FE.
- No cross-workspace leakage.
- No business-rule duplication in dashboard layer.
- Response shape documented and versioned.

## 10. Observability Hooks
- Emit structured log events for dashboard summary generation with section timing.
- Emit counters for full-success vs partial-degraded responses.
- Emit trace spans for each module summary fetch.
