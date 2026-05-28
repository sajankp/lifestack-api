# Spec Implementation Roadmap (013-018)

## Status
- Phase 1 baseline completed on `main` for Specs 013 and 015-018.
- Spec 014 (investment performance) remains deferred/not shipped in this roadmap closeout.
- Phase 1.1 recurring todos (Spec 019) implemented
- This document is now a historical sequencing record plus follow-up reference

## Goal
Implement approved/proposed specs in an order that minimizes rework, preserves architecture boundaries, and keeps each PR reviewable.

## Priority Order
1. `spec-015-notifications.md`
2. `spec-016-weekly-summary.md`
3. `spec-017-spending-analytics.md`
4. `spec-018-quick-capture.md`
5. `spec-013-recurring-transactions.md`
6. `spec-014-investment-performance.md`

## Why This Order
- Notifications is foundational for downstream delivery workflows.
- Weekly summary depends on notification delivery channels.
- Spending analytics can reuse export/reporting patterns and feed summaries.
- Quick capture benefits from existing notifications + analytics contexts.
- Recurring transactions can be implemented safely after core event surfaces stabilize.
- Investment performance is broader and should come after the above baseline.

## Execution Strategy
- One spec per feature branch and PR.
- For each spec:
  - finalize scope cut (V1 slice only)
  - write/refresh failing tests first
  - implement minimal passing behavior
  - run focused suite + smoke regression
  - open PR with explicit “out-of-scope” notes

## Definition of Done Per Spec
- Endpoints/models/migrations match spec acceptance criteria.
- Workflow/services follow `docs/ARCHITECTURE.md` and `PATTERNS.md`.
- Audit logging covered for all mutation paths.
- At least one integration test for happy path and one for isolation/negative path.
- PR threads resolved with explicit rationale when suggestions are deferred.

## Delivery Outcome
- `spec-015-notifications.md`: delivered with in-app inbox + unread + preference flows.
- `spec-016-weekly-summary.md`: delivered with persisted summaries and latest-summary dashboard exposure.
- `spec-017-spending-analytics.md`: delivered with trends + breakdown + budget-performance + savings-rate endpoints.
- `spec-018-quick-capture.md`: delivered with rule-based capture routing and todo/spending dispatch.
- `spec-013-recurring-transactions.md`: delivered with scheduler generation, catch-up controls, and upcoming preview.
- `spec-014-investment-performance.md`: deferred from this sequence.
- `spec-019-recurring-todos.md`: delivered with recurring rule CRUD and scheduler-driven todo generation.

## Recommended Next Use
- Keep this file as a completed roadmap snapshot.
- Track new work in follow-up specs/roadmaps rather than re-opening this sequence.
