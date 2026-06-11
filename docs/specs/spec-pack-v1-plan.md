# Historical Record: Lifestack Spec Pack V1

**Status:** Archived - Implemented
**Original branch:** `planning-spec-pack-v1`
**Archived on:** 2026-06-11

This document is no longer an active roadmap or implementation tracker.

It records the original planning bundle that grouped the first backend foundation specs so implementation could move through the initial product surface with fewer design reversals. The included specs are now implemented or separately superseded by later specs, architecture docs, and product roadmap documents.

For current product direction, use:

- [Product Strategy and Roadmap](../product/PRODUCT_STRATEGY_AND_ROADMAP.md)
- [Platform Architecture and Build Plan](../ARCHITECTURE.md)
- Current individual specs under `docs/specs/`

## Included Specs

- [spec-001-api-versioning-rfc7807.md](./spec-001-api-versioning-rfc7807.md) - Implemented
- [spec-002-workspace-model-and-isolation.md](./spec-002-workspace-model-and-isolation.md) - Implemented
- [spec-003-spending-module.md](./spec-003-spending-module.md) - Implemented
- [spec-004-audit-logging.md](./spec-004-audit-logging.md) - Implemented
- [spec-005-scheduler.md](./spec-005-scheduler.md) - Implemented
- [spec-006-export-module.md](./spec-006-export-module.md) - Implemented
- [spec-007-dashboard-read-model.md](./spec-007-dashboard-read-model.md) - Implemented
- [spec-008-investing-mvp.md](./spec-008-investing-mvp.md) - Implemented
- [spec-009-scheduler-first-workflow-budget-guardrails.md](./spec-009-scheduler-first-workflow-budget-guardrails.md) - Implemented

## Original Purpose

The bundle existed to:

- review dependency chains once
- catch contradictory contracts before coding
- align backend and frontend implementation order
- reduce PR churn during the first implementation wave

That purpose is complete. New feature planning should not extend this file. Create or update the relevant individual spec, architecture doc, or product roadmap section instead.

## Dependency Decisions Preserved

The planning bundle established several durable architecture choices:

- Tenancy: domain data is workspace-scoped.
- API contract: business APIs are versioned under `/v1`.
- Error contract: application errors use RFC 7807 problem details.
- Identity: public APIs expose UUID `public_id` values instead of internal integer primary keys.
- Orchestration: cross-module side effects live in `app/application/`, not inside domain services.
- Scheduler: Stage 1 uses an in-process APScheduler leader with explicit guards.
- Audit: important business mutations emit append-only audit rows in the same transaction boundary.

These decisions now live in the implemented specs and architecture docs. If future work changes one of these rules, update the current source-of-truth document rather than reopening this bundle.

## Original Implementation Waves

The original sequencing was:

1. Wave A: audit logging and scheduler core plumbing.
2. Wave B: dashboard read model and investing MVP.
3. Wave C: first scheduled workflow for budget guardrails.
4. Wave D: exports.

The waves are complete. Later work moved into separate specs such as recurring transactions, notifications, weekly summaries, imports, investing FX/look-through, Gate 0 hardening, and product demo readiness.

## Archive Rule

Do not use this file to track pending work.

Pending or future work belongs in one of these places:

- a current individual feature spec when implementation is approved
- [Product Strategy and Roadmap](../product/PRODUCT_STRATEGY_AND_ROADMAP.md) for product sequencing
- audit documents for review findings and remediation history
- GitHub issues/PRs for execution details
