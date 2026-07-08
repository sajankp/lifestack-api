# Spec Index

Last updated: 2026-07-08

This index lists the current spec set and its roadmap status. The product roadmap is the living home
for future sequencing; individual specs are implementation contracts or historical records.

Specs are immutable historical records — they are not edited, merged, deleted, or compressed after the
fact. This index is grouped by era below to make the 65-spec history navigable without touching the
specs themselves.

## Status Legend

- **Implemented:** shipped at the scope described in the spec.
- **Archived:** retained for historical context; do not use as active backlog.
- **Proposed/current:** available as an active implementation candidate or currently being implemented.
- **Deferred:** parked in the roadmap until explicitly selected for implementation.
- **Draft:** spec text exists but implementation status is not recorded as complete; verify against code before relying on it.

## Foundation / V1 pack (specs 001–039)

Platform baseline, first finance/investing/workflow features, and the original voice-capture surface.

| Spec | Status | Purpose |
|---|---|---|
| [Spec Pack V1 Plan](./spec-pack-v1-plan.md) | Archived - implemented | Historical record for the original V1 spec pack and dependency decisions. |
| [Spec 001: API Versioning and RFC 7807 Error Responses](./spec-001-api-versioning-rfc7807.md) | Implemented | API versioning, error response shape, and baseline list conventions. |
| [Spec 002: Workspace Model and Isolation](./spec-002-workspace-model-and-isolation.md) | Implemented | Workspace ownership, request context, and isolation expectations. |
| [Spec 003: Spending Module](./spec-003-spending-module.md) | Implemented | Spending categories, transactions, budgets, and integration scenarios. |
| [Spec 004: Audit Logging](./spec-004-audit-logging.md) | Implemented | Audit event model, write boundaries, and observability hooks. |
| [Spec 005: Scheduler and Background Jobs](./spec-005-scheduler.md) | Implemented | Scheduler foundation and background job architecture. |
| [Spec 006: Export Module](./spec-006-export-module.md) | Implemented | Export job lifecycle and download behavior. |
| [Spec 007: Dashboard Read Model](./spec-007-dashboard-read-model.md) | Implemented | Dashboard aggregation contract. |
| [Spec 008: Investing Module MVP](./spec-008-investing-mvp.md) | Implemented | Initial investing accounts, holdings, and portfolio surface. |
| [Spec 009: First Scheduler Workflow - Budget Guardrails](./spec-009-scheduler-first-workflow-budget-guardrails.md) | Implemented | Budget guardrail scheduled workflow. |
| [Spec 010: FastTodo Reference Audit](./spec-010-fasttodo-reference-audit.md) | Implemented | Todo reference patterns adopted and rejected. |
| [Spec 011: Investing Currency Governance, FX Valuation, and Cross-Module Transfer Ledger](./spec-011-investing-currency-fx-and-transfers.md) | Implemented (V1.1) | Currency references, FX valuation, and transfer ledger direction. |
| [Spec 012: Look-Through Exposure and Overlap Analytics](./spec-012-lookthrough-overlap-analytics.md) | Implemented (V1.2) | ETF/MF look-through exposure, overlap analytics, and constituent snapshot model. |
| [Spec 013: Recurring Transactions and Subscriptions](./spec-013-recurring-transactions.md) | Implemented | Recurring transaction rules and scheduler generation. |
| [Spec 014: Investment Performance and Returns](./spec-014-investment-performance.md) | Implemented (Performance V1) | Holding prices, portfolio snapshots, and performance endpoints. |
| [Spec 015: Notifications and Delivery Channel](./spec-015-notifications.md) | Implemented (Phase 1) | In-app notifications, preferences, and dispatch service. |
| [Spec 016: Weekly Summary Workflow](./spec-016-weekly-summary.md) | Implemented | Weekly summary data model, scheduler job, and dashboard integration. |
| [Spec 017: Spending Analytics and Trends](./spec-017-spending-analytics.md) | Implemented (Trends V1) | Spending trend analytics and category breakdown direction. |
| [Spec 018: Quick Capture API](./spec-018-quick-capture.md) | Archived - deferred to roadmap | Historical quick-capture proposal; superseded by the roadmap's capture consolidation item (see `PRODUCT_STRATEGY_AND_ROADMAP.md` §4 Immediate Focus). |
| [Spec 019: Recurring Todos](./spec-019-recurring-todos.md) | Implemented | Recurring todo CRUD, scheduler generation, and UI exposure. |
| [Spec 020: Bulk Import via CSV Templates](./spec-020-bulk-import-csv.md) | Implemented (CSV V1) | CSV validate-preview-commit import workflow. |
| [Spec 021: Voice Agent with Function Calling](./spec-021-voice-agent-function-calling.md) | Implemented (Phase 1) | WebSocket voice/tool-calling capture surface and future WebRTC/MCP direction. |
| [Spec 022: Workspace Currency and Display Governance](./spec-022-workspace-currency-and-display-governance.md) | Implemented (V1) | Workspace reporting currency and display rules. |
| [Spec 023: Spending Wallet Ledger and Transfers](./spec-023-spending-wallet-ledger-and-transfers.md) | Implemented (Account/Transfer V1) | Wallet/account model, transfers, and future ledger depth. |
| [Spec 024: Phase 1 Runtime API Integration Contract](./spec-024-phase1-runtime-api-integration-contract.md) | Implemented | Runtime contract for session, workspace, finance, investing, dashboard, and imports. |
| [Spec 027: Investing Account Identity Migration](./spec-027-investing-account-migration.md) | Implemented | Investing account identity migration and frontend/API alignment. |
| [Spec 030: CLI Management Commands](./spec-030-cli-management-commands.md) | Proposed/current | CLI runner for background jobs and production-safe E2E route gating checks. |
| [Spec 031: Automated Price Updates and Investing UI Enhancements](./spec-031-automated-price-updates-and-ui.md) | Implemented | On-demand price refresh, current valuation fields, and investing holdings UI enhancements. |
| [Spec 032: Automated Constituent Ingestion](./spec-032-automated-constituent-ingestion.md) | Archived - retired | Historical Yahoo ingestion implementation; automated ingestion was removed in favor of CSV constituent import. |
| [Spec 033: Hybrid Instrument Catalog with Tenant Overrides](./spec-033-hybrid-instrument-catalog.md) | Deferred | Future global public instrument/company catalog with workspace-scoped overrides. |
| [Spec 034: Investing Constituent CSV Import](./spec-034-constituent-csv-import.md) | Implemented | Workspace-facing constituent CSV import for ETF/MF look-through data. |
| [Spec 035: Platform Market Data Curation](./spec-035-platform-market-data-curation.md) | Deferred | Future platform-admin curation for shared/global constituents, instrument prices, provenance, and rollback. |
| [Spec 036: Password Reset](./spec-036-password-reset.md) | Implemented | Email-based password reset request and confirmation workflow. |
| [Spec 037: Remote Database Backups](./spec-037-remote-database-backups.md) | Implemented | Daily encrypted PostgreSQL SQL backups to Cloudflare R2 or OCI Object Storage with guarded restore tooling. |
| [Spec 038: Canonical Portfolio Performance](./spec-038-canonical-portfolio-performance.md) | Implemented | Shared holdings valuation, invested value, gain/loss, daily change, and separate investment cash across Investing and Dashboard. |
| [Spec 039: Google ADK Evaluation & Voice-First Multi-Agent Migration Guide](./spec-039-adk-evaluation-and-migration-guide.md) | Reference | Phase 2 voice/agent migration evaluation; not an implementation contract. |

## Gate 0 — Foundation Stabilization

Security, RBAC, demo-readiness, and documentation-honesty hardening before new high-trust domains. Status: complete (see roadmap §6).

| Spec | Status | Purpose |
|---|---|---|
| [Spec 025: API and Database Security Remediation](./spec-025-audit-remediation.md) | Implemented | RBAC, auth, path traversal, WebSocket, CORS, and database remediation. |
| [Spec 026: Gate 0 Foundation Hardening](./spec-026-gate0-foundation.md) | Implemented | Gate 0 security, reliability, UX, lifecycle, and finance correctness hardening. |
| [Spec 028: Gate 0 Foundation Remediation](./spec-028-gate0-foundation-remediation.md) | Implemented | Import rollback, FX transparency, demo reset, docs refresh, and workspace readiness. |
| [Spec 029: Gate 0 Demo Readiness](./spec-029-current-product-demo-readiness-roadmap.md) | Archived - implemented | Historical Gate 0 closure record. |

## Finance correctness wave (specs 040–050)

Transfer-inclusive reconciliation, FIFO cost basis, and account-currency invariants — the cash-correctness campaign the roadmap now marks V1-complete (see roadmap §4, "Finance depth: V1 complete").

| Spec | Status | Purpose |
|---|---|---|
| [Spec 040: Transfer-Inclusive Ledger and Account Reconciliation](./spec-040-transfer-inclusive-ledger-and-reconciliation.md) | Draft | Transfer-inclusive ledger and reconciliation model; verify implementation status against code. |
| [Spec 041: Transaction-Based Investing Orders](./spec-041-investing-orders.md) | Implemented (api#72) | Investing orders modeled as transactions. |
| [Spec 042: Net Worth / Balance Sheet Page](./spec-042-net-worth-page.md) | Implemented (api#83) | Net worth / balance sheet aggregation and page. |
| [Spec 043: Transfer Edit & Delete](./spec-043-transfer-edit-delete.md) | Implemented | Transfer edit and delete workflow. |
| [Spec 044: FIFO Lot-Based Cost Basis](./spec-044-fifo-cost-basis.md) | Draft | FIFO lot-based cost basis model; verify implementation status against code. |
| [Spec 045: Rename Symbol on Order-Derived Holdings](./spec-045-order-holding-symbol-rename.md) | Implemented | Symbol rename propagation for order-derived holdings. |
| [Spec 046: Investing Cost-Basis Accuracy](./spec-046-investing-cost-basis-accuracy.md) | Draft | Fee capitalization and book-value precision; verify implementation status against code. |
| [Spec 047: Investing Substring Search + Net-Worth Brokerage Cash Breakdown](./spec-047-investing-search-and-networth-cash-breakdown.md) | Draft | Investing search and net-worth cash breakdown; verify implementation status against code. |
| [Spec 048: Unified Account-Centric Cash View](./spec-048-unified-cash-view.md) | Implemented (api#95) | Unified account-centric cash view. |
| [Spec 049: Transfer Brokerage-Outflow Cash Snapshot](./spec-049-transfer-brokerage-outflow-snapshot.md) | Implemented (api#97) | Brokerage-outflow cash snapshot on transfer. |
| [Spec 050: One Account, One Currency](./spec-050-account-currency-invariant.md) | Implemented (api#98) | Account-currency invariant. |

## 2026-06→07 feature wave (specs 051–065)

Corporate actions, web push, CAS PDF ingestion (CAMS/NSDL/CDSL), NSE bhavcopy pricing, dashboard insights, voice usability, and the category/budget/net-worth-history slice — the most recently merged work.

| Spec | Status | Purpose |
|---|---|---|
| [Spec 051: Corporate Actions (Splits, Reverse Splits, Bonus Issues)](./spec-051-corporate-actions-splits.md) | Implemented (api#102) | Corporate actions as first-class replayed events, golden-tested. |
| [Spec 052: Web Push Notification Delivery](./spec-052-web-push-notifications.md) | Implemented (api#108) | Web push subscription management and delivery job. |
| [Spec 053: Calendar Recurrence Modes](./spec-053-calendar-recurrence-modes.md) | Implemented (api#109) | Month-end and nth-weekday recurrence modes. |
| [Spec 054: Mandatory Account on Spending Transactions](./spec-054-mandatory-transaction-account.md) | Implemented (api#110) | Mandatory account association for spending transactions. |
| [Spec 055: Capture Agent Workspace Awareness](./spec-055-capture-agent-workspace-awareness.md) | Implemented (api#112) | Workspace-scoped capture/voice agent context. |
| [Spec 056: CAMS CAS PDF Import](./spec-056-cams-cas-pdf-import.md) | Implemented (api#105) | CAMS Consolidated Account Statement PDF import. |
| [Spec 057: NSE Bhavcopy Price Feed](./spec-057-nse-bhavcopy-price-feed.md) | Implemented (api#106) | NSE bhavcopy daily price feed ingestion. |
| [Spec 058: Dashboard Insights (Phase 1)](./spec-058-dashboard-insights.md) | Implemented (api#107) | Dashboard insights job — overdue, guardrail, and valuation cues. |
| [Spec 059: Voice Agent Usability](./spec-059-voice-agent-usability.md) | Implemented | Fuzzy spending-account matching, read-only investing, barge-in. |
| [Spec 060: Demat CAS PDF Import — Holdings Verification](./spec-060-demat-cas-holdings-verification.md) | Implemented (api#123, web#82) | NSDL Demat CAS holdings verification import. |
| [Spec 061: Voice Agent — Backdated Spending Transactions](./spec-061-voice-transaction-date.md) | Implemented (api#126) | Backdated transaction support via voice agent. |
| [Spec 062: Deletable System Categories & Category Merge](./spec-062-category-delete-and-merge.md) | Implemented (api#131, web#86) | Category delete and merge. |
| [Spec 063: CDSL Demat CAS Support (Holdings Verification)](./spec-063-cdsl-demat-cas-support.md) | Implemented | CDSL Demat CAS holdings verification import. |
| [Spec 064: Recurring Date-Ranged Budgets & Category Groups](./spec-064-category-group-budgets.md) | Implemented (api#131, web#86) | Category groups and recurring date-ranged budgets. |
| [Spec 065: Net Worth Over Time](./spec-065-net-worth-over-time.md) | Implemented (api#132, web#87) | Live cash + daily net-worth history and graph. |

## Roadmap Alignment

- Active and proposed implementation candidates are tracked in the product roadmap before they are selected for implementation.
- Completed specs should not accumulate new backlog items; future work belongs in the roadmap, GitHub issues/PRs, or a new focused spec.
- Deferred specs remain reference material until the roadmap explicitly promotes them into implementation work.
- The finance correctness wave (040–050) and the 2026-06→07 feature wave (051–065) are both closed; per the roadmap's 2026-07-08 revision, new finance specs require explicit justification against briefing/health/capture priorities (see `../product/PRODUCT_STRATEGY_AND_ROADMAP.md`).
