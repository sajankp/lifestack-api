# Spec Implementation Roadmap (013-018)

## Status
- Working plan (post-PR16/PR17 merge)
- Scope: backend-first sequencing and implementation slices

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

## Immediate Next Step
Start `spec-015-notifications.md` with a narrow V1 slice:
- notification data model + repository/service
- create/list/acknowledge API
- minimal in-app delivery workflow integration
