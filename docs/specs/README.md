# Spec Index

Last updated: 2026-06-20

This index lists the current spec set and its roadmap status. The product roadmap is the living home
for future sequencing; individual specs are implementation contracts or historical records.

## Status Legend

- **Implemented:** shipped at the scope described in the spec.
- **Archived:** retained for historical context; do not use as active backlog.
- **Proposed/current:** available as an active implementation candidate or currently being implemented.
- **Deferred:** parked in the roadmap until explicitly selected for implementation.

## Current Spec Set

| Spec | Status | Purpose |
|---|---|---|
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
| [Spec 018: Quick Capture API](./spec-018-quick-capture.md) | Archived - deferred to roadmap | Historical quick-capture proposal; future capture sequencing belongs in the roadmap. |
| [Spec 019: Recurring Todos](./spec-019-recurring-todos.md) | Implemented | Recurring todo CRUD, scheduler generation, and UI exposure. |
| [Spec 020: Bulk Import via CSV Templates](./spec-020-bulk-import-csv.md) | Implemented (CSV V1) | CSV validate-preview-commit import workflow. |
| [Spec 021: Voice Agent with Function Calling](./spec-021-voice-agent-function-calling.md) | Implemented (Phase 1) | WebSocket voice/tool-calling capture surface and future WebRTC/MCP direction. |
| [Spec 022: Workspace Currency and Display Governance](./spec-022-workspace-currency-and-display-governance.md) | Implemented (V1) | Workspace reporting currency and display rules. |
| [Spec 023: Spending Wallet Ledger and Transfers](./spec-023-spending-wallet-ledger-and-transfers.md) | Implemented (Account/Transfer V1) | Wallet/account model, transfers, and future ledger depth. |
| [Spec 024: Phase 1 Runtime API Integration Contract](./spec-024-phase1-runtime-api-integration-contract.md) | Implemented | Runtime contract for session, workspace, finance, investing, dashboard, and imports. |
| [Spec 025: API and Database Security Remediation](./spec-025-audit-remediation.md) | Implemented | RBAC, auth, path traversal, WebSocket, CORS, and database remediation. |
| [Spec 026: Gate 0 Foundation Hardening](./spec-026-gate0-foundation.md) | Implemented | Gate 0 security, reliability, UX, lifecycle, and finance correctness hardening. |
| [Spec 027: Investing Account Identity Migration](./spec-027-investing-account-migration.md) | Implemented | Investing account identity migration and frontend/API alignment. |
| [Spec 028: Gate 0 Foundation Remediation](./spec-028-gate0-foundation-remediation.md) | Implemented | Import rollback, FX transparency, demo reset, docs refresh, and workspace readiness. |
| [Spec 029: Gate 0 Demo Readiness](./spec-029-current-product-demo-readiness-roadmap.md) | Archived - implemented | Historical Gate 0 closure record. |
| [Spec 030: CLI Management Commands](./spec-030-cli-management-commands.md) | Proposed/current | CLI runner for background jobs and production-safe E2E route gating checks. |
| [Spec 031: Automated Price Updates and Investing UI Enhancements](./spec-031-automated-price-updates-and-ui.md) | Implemented | On-demand price refresh, current valuation fields, and investing holdings UI enhancements. |
| [Spec 032: Automated Constituent Ingestion](./spec-032-automated-constituent-ingestion.md) | Archived - retired | Historical Yahoo ingestion implementation; automated ingestion was removed in favor of CSV constituent import. |
| [Spec 033: Hybrid Instrument Catalog with Tenant Overrides](./spec-033-hybrid-instrument-catalog.md) | Deferred | Future global public instrument/company catalog with workspace-scoped overrides. |
| [Spec 034: Investing Constituent CSV Import](./spec-034-constituent-csv-import.md) | Implemented | Workspace-facing constituent CSV import for ETF/MF look-through data. |
| [Spec 035: Platform Market Data Curation](./spec-035-platform-market-data-curation.md) | Deferred | Future platform-admin curation for shared/global constituents, instrument prices, provenance, and rollback. |
| [Spec 036: Password Reset](./spec-036-password-reset.md) | Implemented | Email-based password reset request and confirmation workflow. |
| [Spec 037: Remote Database Backups](./spec-037-remote-database-backups.md) | Implemented | Daily encrypted PostgreSQL SQL backups to Cloudflare R2 or OCI Object Storage with guarded restore tooling. |

## Historical Bundles

| Document | Status | Purpose |
|---|---|---|
| [Lifestack Spec Pack V1](./spec-pack-v1-plan.md) | Archived - implemented | Historical record for the original V1 spec pack and dependency decisions. |

## Roadmap Alignment

- Active and proposed implementation candidates are tracked in the product roadmap before they are selected for implementation.
- Completed specs should not accumulate new backlog items; future work belongs in the roadmap, GitHub issues/PRs, or a new focused spec.
- Deferred specs remain reference material until the roadmap explicitly promotes them into implementation work.
