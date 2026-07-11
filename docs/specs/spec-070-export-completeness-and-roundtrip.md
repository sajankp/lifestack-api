# Spec-070: Export Completeness & Import/Export Round-Trip

**Created:** 2026-07-10
**Status:** Implemented (api `fa01f25`/`15617f6`, web `a12e612`, 2026-07-10). Decisions: (1) health export is **surfaced in the UI**; (2) accounts are **auto-included when referenced** (INV-3); (3) transfers + accounts + settings form a **new `finance` module**.
**Scope:** multi-repo, user-facing — `lifestack-api` (export data + module contract) and `lifestack-web` (export UI). Delivered as two PRs, api merged first (per one-PR-per-repo rule). No e2e-blocking behavior change expected; add e2e coverage for the new export modules opportunistically.
**Depends on:** spec-041 (investing orders as source of truth), spec-049/050 (capital transfers + brokerage snapshots), spec-056/060/063 (CAS imports). Related domain doc: `docs/domain/cash-model-ledger-snapshots-reconciliation.md`.

---

## Problem

The export and import halves have drifted apart, and export no longer reflects the current data model.

1. **Export emits derived data, not the source of truth.** Since spec-041, `InvestingOrder` is authoritative for investing; `Holding` is *derived* — `order_service._recompute_holding()` rebuilds it by replaying orders (FIFO lots, corporate actions, realized gains). But `ExportService._write_json_export`/`_write_csv_export` emit only `Holding` + `CashBalance` and **never the orders**. An export therefore cannot reconstruct a portfolio: no order history, no `OrderLot`, no realized gain/loss, no cost-basis lineage.

2. **No round-trip.** Import supports `investing-orders` and `finance-transfers`; export produces neither. A user can import orders and transfers but cannot get them back out. `CapitalTransfer` — "a key bridge between spending and investing" — is entirely absent from export, so an export omits the movements that reconciliation and net worth depend on.

3. **Silent omissions of live features.** `RecurringTransaction`, `CategoryGroup`, and `RecurringTodoRule` are current, user-facing tables that export drops without notice. `Account` — referenced by every holding, transfer, and (soon) exported order — is not exported, so the artifact is not self-consistent for restore.

4. **UI/backend module mismatch.** Backend `SUPPORTED_MODULES` includes `health`, but the web `ExportModule` type and `MODULE_OPTIONS` only offer `todo | spending | investing`. Health export is reachable by API but invisible to users — orphaned.

5. **Dead import module.** `ImportModule.investing_holdings` is already marked *"Kept for backward-compat deserialization of historic rows only."* Holdings are derived from orders now; holdings *import* is conceptually dead but still enumerated.

The through-line: there is no shared notion of *what a module contains*, which is exactly how the orders gap went unnoticed.

## Solution

Four parts. The export additions (A) are the value; the rest remove the drift that caused them.

### Invariants (must hold)

- **INV-1 — Export the source of truth, label the derived.** Investing export includes `InvestingOrder` (+ `OrderLot`, `CorporateAction`) as authoritative. `Holding`/`CashBalance` remain in the export but are documented as *derived snapshots* (convenience, not restore-authoritative). No consumer should treat holdings as reconstructable independent of orders.
- **INV-2 — Round-trip parity.** Every entity that *import* can create is present in *export*. Concretely: orders and capital transfers, which import already supports, must appear in export. (Full re-import of the export JSON is a separate future spec — see Non-goals — but the data must not be lost on the way out.)
- **INV-3 — Self-consistent artifact.** When an export references an account (holdings, orders, transfers, spending transactions), the referenced `Account` rows are included so the artifact is internally resolvable.
- **INV-4 — One module vocabulary.** The set of export modules and what each contains is defined once (backend `SUPPORTED_MODULES` + a documented per-module content manifest) and the web UI is generated from / matched to it. Backend and web can never again disagree on which modules exist.
- **INV-5 — Workspace-scoped, streaming, sync-limited.** New entities follow the existing pattern exactly: `workspace_id` filter, `stream_scalars` progressive write, and inclusion in `_count_module_rows` so `SYNC_LIMIT_PER_MODULE` still guards payload size. No unbounded materialization.

### A. Export the current investing + finance model (lifestack-api)

Extend the module content so each module exports its authoritative entities:

| Module | Now exports | Add |
|---|---|---|
| `investing` | holdings, cash_balances | **orders** (`InvestingOrder`), **order_lots** (`OrderLot`), **corporate_actions** (`CorporateAction`) |
| `spending` | categories, transactions, budgets | **category_groups** (`CategoryGroup`), **recurring_transactions** (`RecurringTransaction`) |
| `todo` | todos | **recurring_rules** (`RecurringTodoRule`) |
| `finance` *(new module)* | — | **accounts** (`Account`), **capital_transfers** (`CapitalTransfer`), **finance_settings** (`WorkspaceFinanceSetting`, workspace currencies) |
| `health` | medications, medication_events, weight_entries | *(unchanged; see part C)* |

- Each new entity gets a `stream_scalars(... .where(workspace_id ==) .order_by(created_at/occurred_at asc))` section in both `_write_json_export` and `_write_csv_export` (new `<module>/<entity>.csv` arcname), and a count added to `_count_module_rows`.
- `SCHEMA_VERSION` bumps `1 → 2` (new keys, additive). Old artifacts remain readable; the version field lets any future importer branch.
- **Accounts are always included** whenever `finance`, `investing`, or `spending` is selected (INV-3), even if the user didn't tick `finance`, so no exported reference dangles. Documented in the manifest; deduplicated if multiple selected.

### B. One module contract (lifestack-api + lifestack-web)

- Define a single source-of-truth manifest in the backend: `EXPORT_MODULES: dict[str, list[str]]` (module → entity names) alongside `SUPPORTED_MODULES`, and surface it via a small `GET /exports/modules` metadata endpoint (or embed in the OpenAPI schema).
- Web `ExportModule` type + `MODULE_OPTIONS` are aligned to that set (adds `finance`, `health`). Long-term the UI can fetch the manifest; minimally, this spec makes the two lists match and adds the missing options.

### C. Surface or retire `health` export

Decision required (see Open questions). Default recommendation: **surface it** — add `health` to `MODULE_OPTIONS` (label "Health") so the existing backend capability is usable, rather than deleting working code. If the owner considers health export premature, remove `health` from backend `SUPPORTED_MODULES` instead. Not both.

### D. Retire the dead holdings import (lifestack-api) — CONFIRMED, no code change needed

- `ImportModule.investing_holdings` stays in the enum for historic-row deserialization (its comment already says so). Verified: `validate_upload` hard-rejects it with `ValidationError("investing-holdings imports are no longer supported")` (`app/imports/service.py:464-465`) before any file processing; it is absent from `TEMPLATE_HEADERS` and `REQUIRED_HEADERS`; and `lifestack-web`'s `ImportsPage.tsx` `MODULE_OPTIONS` never offers it as an upload choice (only present in the `ImportModule` TS type, with an explicit backward-compat comment, so historic `import_batches` rows still render). No live surface exists to remove. No migration — enum value retained for old rows.

## Now vs. Proposed

| Aspect | Now | Proposed |
|---|---|---|
| Investing export | holdings + cash only (derived) | + orders, lots, corporate actions (authoritative) |
| Capital transfers | not exported | exported under `finance` |
| Accounts | not exported | exported (auto-included when referenced) |
| Recurring txns / category groups / recurring todo rules | dropped | exported |
| Export modules | `todo, spending, investing, health` (health UI-orphaned) | `todo, spending, investing, finance, health`, backend↔web aligned |
| Schema version | 1 | 2 (additive) |
| Round-trip | import can create data export can't emit | parity (INV-2) |

## Non-goals

- **Re-importing the export JSON** (full backup/restore). This spec makes export *complete and lossless*; a JSON re-import path (ID remapping, conflict handling, ordering) is a separate, larger spec. Export today is a data-portability artifact, not a restore mechanism, and this spec keeps that framing while removing the data loss.
- Retroactive backfill of anything.
- Changing storage backends, TTL/cleanup, or the S3/local/db logic — untouched.

## Testing

- Backend RGR: for each new entity, a test asserting a seeded row appears in both JSON and CSV output and is counted toward the sync limit; a test that selecting `investing` includes orders and that accounts are auto-included; SCHEMA_VERSION=2 assertion. Extend `app/tests/integration/test_exports.py` + `app/tests/exports/test_service.py`.
- Web: `ExportsPage` test covering the new module options (finance, health) and that the module list matches the backend contract.
- Coverage gate unchanged (80% api).

## Resolved decisions

1. **Health export** — surfaced in the web UI (part C). Backend capability retained.
2. **Accounts** — auto-included whenever referenced by a selected module (INV-3), independent of whether `finance` is ticked.
3. **`finance` module** — distinct module owning accounts, capital transfers, and finance settings.

## Phasing

1. **PR-1 (api)**: `finance` module (accounts, transfers, settings) + investing orders/lots/corporate actions + spending/todo additions + SCHEMA_VERSION=2 + count updates + module manifest endpoint. Merge first.
2. **PR-2 (web)**: align `ExportModule`/`MODULE_OPTIONS` to the manifest; add finance (+ health per Q1) options; tests.
3. Retire dead holdings-import surface if any exists (small, can ride PR-1).
