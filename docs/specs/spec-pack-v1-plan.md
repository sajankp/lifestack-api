# Lifestack Spec Pack V1 (Planning Bundle)

**Status:** Implemented
**Branch:** `planning-spec-pack-v1`
**Purpose:** Provide one coherent planning bundle so implementation can run faster with fewer design reversals.

Implementation note (2026-06-11): this planning bundle is historical. Specs 001-009 have been implemented and are now reflected in the main backend architecture, tests, and README feature table.

## Included Specs
- [spec-001-api-versioning-rfc7807.md](./spec-001-api-versioning-rfc7807.md) (Implemented)
- [spec-002-workspace-model-and-isolation.md](./spec-002-workspace-model-and-isolation.md) (Implemented)
- [spec-003-spending-module.md](./spec-003-spending-module.md) (Implemented)
- [spec-004-audit-logging.md](./spec-004-audit-logging.md) (Implemented)
- [spec-005-scheduler.md](./spec-005-scheduler.md) (Implemented)
- [spec-006-export-module.md](./spec-006-export-module.md) (Implemented)
- [spec-007-dashboard-read-model.md](./spec-007-dashboard-read-model.md) (Implemented)
- [spec-008-investing-mvp.md](./spec-008-investing-mvp.md) (Implemented)
- [spec-009-scheduler-first-workflow-budget-guardrails.md](./spec-009-scheduler-first-workflow-budget-guardrails.md) (Implemented)

## Why One Planning Branch
- Review dependency chains once.
- Catch contradictory contracts before coding.
- Align BE/FE implementation order.
- Reduce PR churn during implementation phase.

## Dependency and Sequence Map
1. `001` + `002` are foundational and already approved.
2. `003` defines spending entities and APIs.
3. `004` defines mutation audit contract used by later features.
4. `005` defines scheduler runtime and deployment topology.
5. `008` (investing MVP) adds a second domain on the same architectural shape.
6. `009` defines first concrete scheduled workflow behavior.
7. `006` (export) depends on stable read models from todo/spending/investing and on audit hooks.
8. `007` (dashboard read model) depends on stable module query contracts.

## Consistency Checks Across Specs
- Tenancy consistency: all domain tables remain workspace-scoped (`002`, `003`, `006`, `007`, `008`, `009`).
- API consistency: all new APIs remain under `/v1` and use RFC 7807 for errors (`001`, all new specs).
- Identifier consistency: external APIs use `public_id` UUIDs; internal PKs stay integer (`003`, `006`, `008`).
- Orchestration consistency: cross-module side effects happen in `app/application/`, not module services (`ARCHITECTURE`, `005`, `009`).
- Scheduler consistency: single leader process via `SCHEDULER_ENABLED=true` (`005`, `009`).
- Audit consistency: business mutations that matter emit append-only audit rows in same transaction (`004`, `006`, `008`, `009`).

## Planned Implementation Waves
- Wave A: `004` + `005` core plumbing.
- Wave B: `007` dashboard read model (minimal summary) + `008` investing MVP.
- Wave C: `009` first scheduled workflow.
- Wave D: `006` exports.

## Planning Decisions Locked For V1
- Export generation remains stage-1 local artifact storage (no encryption-at-rest service in this slice).
- Dashboard ships with one aggregate endpoint (`GET /v1/dashboard/summary`) for initial FE simplicity.
- Investing MVP scope is holdings + cash balance + summary only; transaction ledger is deferred to V2.
- Delete strategy in V1 is hard-delete on domain tables, with historical trace preserved through audit logs.
- No platform/settings management endpoints are introduced in this planning bundle.

## Exit Criteria For Merging This Planning Bundle
- Specs `006-009` reviewed for contradictions with `001-005`.
- Ownership assigned for each implementation wave.
- Any deferred items explicitly moved to out-of-scope sections.
