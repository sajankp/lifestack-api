# Historical Record: Gate 0 Demo Readiness

**Status:** Archived - Implemented
**Spec ID:** 029
**Created:** 2026-06-06
**Archived:** 2026-06-11

This document is no longer an active roadmap or backlog tracker.

Spec 029 captured the short-lived execution plan for making the current Lifestack product credible, safe, and easy to demonstrate before adding larger life domains such as health, documents, second brain, MCP, or a personal coach. That Gate 0 demo-readiness work has landed across the API, Web, and E2E repositories.

Future product sequencing should live in [Product Strategy and Roadmap](../product/PRODUCT_STRATEGY_AND_ROADMAP.md). Specs should describe approved implementation contracts, not carry open-ended pending-item lists.

## Product Position Preserved

The Gate 0 product position remains useful:

Lifestack works best today as a **finance-led personal operations command center**.

Primary current surfaces:

- Dashboard: operating briefing across money, tasks, alerts, and summaries.
- Spending: transactions, budgets, recurring rules, and guardrails.
- Investing: account-backed holdings, cash, FX conversion, and performance context.

Supporting surfaces:

- Imports and exports: data movement, portability, and trust.
- Todos and notifications: action layer for follow-ups and system-generated work.
- Workspaces and RBAC: trust boundary and demo credibility.
- Master config: admin/settings surface, not a daily destination.

Experimental surface:

- Voice/capture: input layer over structured services, not the core product.

## Closed Outcomes

Gate 0 demo readiness closed the following outcomes:

- Demo reset is feature-flagged, role-gated for owner/admin, limited to the active workspace, audited, confirmation-gated by workspace name, service-owned outside the router, and covered by API/Web tests.
- Workspace selection updates the active workspace token claim and persists refresh-token hash rotation.
- Frontend active-workspace state is explicit enough for destructive reset flows.
- Auth/session hardening blocks inactive users on existing access tokens, clears current browser cookies after password change, rejects malformed Bearer headers, and keeps refresh grace retries from overwriting the first rotated refresh token.
- Voice WebSocket frame, cumulative byte, duration, and text-size limits are implemented.
- Provider errors for voice/capture are sanitized, and frontend failure UX is visible and recoverable.
- Investing summary and performance snapshots use shared FX conversion helpers and persist reporting currency plus FX rates used.
- Demo reset logic is extracted from the platform router into a dedicated service.
- Auth/session dependency wiring is extracted from `core/dependencies.py` into `auth/dependencies.py`.
- Local/test-only E2E workflow hooks replace container-shell job triggers.
- The E2E web image no longer installs dependencies at runtime.
- README, ERD, Spec 014, Spec 028, the V1 spec-pack archive, and the historical spec-status sweep distinguish implemented, partial, planned, and archived behavior.

## Reviewer Demo Journey

The intended first five minutes of review remain:

1. Dashboard: show financial health, upcoming tasks, latest summary, and portfolio snapshot.
2. Spending: show budget guardrails and recurring transactions.
3. Imports: show review-before-commit behavior.
4. Investing: show account-backed holdings, cash, FX conversion, and performance context.
5. Workspace/admin: show RBAC, active workspace context, and safe demo reset.
6. Evidence layer: point to E2E, architecture docs, security checklist, and audit reports.

The demo should show insight before data entry. The user should quickly understand what changed, what needs attention, and what action to take next.

## Non-Goals Preserved

The following remained intentionally outside Gate 0:

- health tracking
- document intelligence or RAG
- MCP tools
- expanded coach experience
- new major product modules

Those belong in the product roadmap until an implementation slice is explicitly selected.

## Post-Gate 0 Backlog Placement

Do not add new pending items to this spec.

Use these homes instead:

- Product sequencing: [Product Strategy and Roadmap](../product/PRODUCT_STRATEGY_AND_ROADMAP.md)
- Architecture and module-boundary decisions: [Architecture](../ARCHITECTURE.md) or a focused new spec
- Review/remediation history: root `audit/` documents
- Execution: GitHub issues or PR descriptions

Examples of post-Gate 0 work that should stay outside this spec:

- frontend page decomposition
- backend dependency/module decomposition beyond the completed auth/session split
- per-user/workspace WebSocket rate limits if voice becomes a primary workflow
- broader valuation-assumption UX
- production observability, backup/restore, and runbooks
- mobile companion, health, documents, MCP, second brain, and coach tracks
