# Spec: Current Product Demo Readiness Roadmap

**Status:** Active - Partially Implemented
**Spec ID:** 029
**Date:** 2026-06-06

## 1. Purpose

This roadmap defines the active near-term product and engineering sequence for the current Lifestack branch.

The goal is to make the existing application credible, safe, and easy to demonstrate before adding new life domains such as health, documents, second brain, MCP, or a personal coach.

## 2. Product Position

Lifestack currently works best as a **finance-led personal operations command center**.

Primary current surfaces:

- Dashboard: operating briefing across money, tasks, alerts, and summaries.
- Spending: transactions, budgets, recurring rules, and guardrails.
- Investing: account-backed holdings, cash, FX conversion, and performance context.

Supporting surfaces:

- Imports and exports: realistic data movement, portability, and trust.
- Todos and notifications: action layer for follow-ups and system-generated work.
- Workspaces and RBAC: trust boundary and demo credibility.
- Master config: admin/settings surface, not a daily destination.

Experimental surfaces:

- Voice/capture: input layer over structured services, not the core product.

## 3. Product Non-Goals For This Roadmap

- Do not add health tracking.
- Do not add document intelligence or RAG.
- Do not add MCP tools.
- Do not expand the coach experience.
- Do not add more major product modules before Gate 0 is public-demo safe.

The current application already has enough surface area for a strong portfolio demo. The next product work is composition, correctness, and trust.

## 4. Implementation Status Snapshot

Last reviewed: 2026-06-10.

| Milestone | Status | Implemented | Open |
| --- | --- | --- | --- |
| Milestone 1 - Demo Safety Baseline | Done | Demo reset is feature-flagged, role-gated for owner/admin, limited to the active workspace, exposed only when backend status allows it, audited for denied and successful attempts, confirmation-gated by workspace name, service-owned outside the router, and covered by API/Web tests. | Keep README/spec seed-data descriptions aligned as the fixture evolves. |
| Milestone 2 - Workspace and Session Correctness | Partial | Workspace selection updates the active workspace token claim, persists refresh-token hash rotation, is covered for follow-up workspace-aware actions plus select-then-refresh, and Web now has a persisted active-workspace model used by destructive reset flows. | Centralize login, refresh, and workspace-select rotation into a shared helper when auth code is next refactored. |
| Milestone 3 - Investing Correctness | Partial | Investing summary and performance snapshots now use shared FX conversion helpers, persist reporting currency and FX rates used, and cover deterministic USD/GBP/EUR fixtures. | Add parity checks between summary and performance totals where both endpoints use the same valuation basis. |
| Milestone 4 - Documentation Reconciliation | Partial | This roadmap now identifies current surfaces, future-track non-goals, and the demo journey. README, ERD, Spec 014, Spec 028, and the audit index now reflect active workspace reset safety, demo fixture details, user finance settings, auth session rotation, and performance FX metadata. | Finish the broader historical spec-status sweep outside the Spec 029 release gate. |
| Milestone 5 - Maintainability Cleanup | Partial | Demo reset logic is extracted from the platform router into a dedicated service, and background lifecycle plus DB hardening work from Gate 0 reduced some operational risk. | Split large dependency/frontend modules and replace container-shell E2E helpers. |

## 5. Reviewer Demo Journey

The intended first five minutes of review:

1. Dashboard: show financial health, upcoming tasks, latest summary, and portfolio snapshot.
2. Spending: show budget guardrails and recurring transactions.
3. Imports: show review-before-commit behavior.
4. Investing: show account-backed holdings, cash, FX conversion, and performance context.
5. Workspace/admin: show RBAC, active workspace context, and safe demo reset.
6. Evidence layer: point to E2E, architecture docs, security checklist, and audit reports.

The demo should show insight before data entry. The user should quickly understand what changed, what needs attention, and what action to take next.

## 6. Milestone 1 - Demo Safety Baseline

Goal: destructive demo features are safe, scoped, and intentional.

Required work:

- Restrict demo reset to owner/admin roles.
- Add an explicit demo/reset feature flag.
- Ensure reset can target only the intended workspace.
- Emit audit events for reset attempts and outcomes.
- Hide or disable reset UI for users who cannot run it.
- Require a confirmation phrase that includes the workspace name.

Acceptance criteria:

- Viewer and member reset attempts return `403 Forbidden`.
- Reset is unavailable when the feature flag is disabled.
- Reset always displays and uses the active workspace.
- Reset seed data matches the spec and README.

## 7. Milestone 2 - Workspace and Session Correctness

Goal: workspace switching is safe, predictable, and session-compatible.

Required work:

- Persist refresh-token hash changes when workspace selection issues a new refresh token.
- Centralize login, refresh, and workspace-select session rotation semantics.
- Add a frontend active-workspace state model.
- Avoid inferring workspace targets from list order.
- Make active workspace visible enough for destructive/admin flows.

Acceptance criteria:

- Login, select another workspace, then refresh succeeds.
- Workspace-aware frontend actions use active workspace state.
- Multi-workspace users can identify which workspace they are acting in.

## 8. Milestone 3 - Investing Correctness

Goal: investing analytics are correct for multi-currency portfolios.

Required work:

- Reuse or extract FX conversion logic for performance snapshots.
- Convert holdings and cash into reporting currency before aggregate performance values are stored or returned.
- Expose reporting currency and FX rates used when conversion occurs.
- Add regression tests for USD, GBP, and EUR accounts with known rates.

Acceptance criteria:

- Summary and performance values agree for the same reporting currency.
- Snapshot responses do not mix native currency amounts under a single USD label.
- Multi-currency test fixtures produce deterministic totals.

## 9. Milestone 4 - Documentation Reconciliation

Goal: docs make the current state and roadmap obvious to reviewers.

Required work:

- Update README to describe the current product as a finance-led personal operations command center.
- Keep future health, document, second brain, MCP, and coach tracks clearly separated from implemented features.
- Update ERD for current auth session and finance settings fields.
- Update spec statuses to `Proposed`, `Approved`, `Implemented`, `Partially Implemented`, or `Superseded`.
- Align demo reset seed data between spec, implementation, README, and tests.
- Keep historical roadmaps marked as historical.

Acceptance criteria:

- A reviewer can tell what exists today, what is planned, and what is intentionally deferred.
- No roadmap implies that future high-trust domains are already implemented.
- Spec 028 and this roadmap agree on Gate 0 hardening priorities.

## 10. Milestone 5 - Maintainability Cleanup

Goal: reduce complexity in the areas that now carry the most product risk.

Required work:

- Extract demo reset logic from the platform router into a service.
- Split auth identity, workspace resolution, RBAC, and service-factory dependencies where practical.
- Split large frontend pages into feature hooks, forms, tables, and dialogs.
- Remove runtime dependency installation from the E2E web container path.
- Replace container-shell job triggering in E2E with a test-only helper or API.

Acceptance criteria:

- Security-critical auth/workspace code is smaller and easier to review.
- Reset behavior is service-tested outside the router.
- E2E setup is repeatable from a clean checkout.

## 11. Release Gate

Gate 0 is public-demo ready only when:

- Demo reset is safe and role-gated.
- Workspace switch plus refresh is covered by tests.
- Frontend active workspace state is explicit.
- Multi-currency investing performance is correct.
- README and specs distinguish current features from future tracks.
- The reviewer demo journey can be completed without manual database edits or hidden setup knowledge.

After this release gate, Track 1 mobile companion work can begin.
