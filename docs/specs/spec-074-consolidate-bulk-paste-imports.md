# Spec-074: Consolidate bespoke bulk-paste flows into the imports framework

**Created:** 2026-07-11
**Status:** Draft — owner-approved in principle (2026-07-11, "one umbrella spec now"); awaiting review of this document before implementation.
**Scope:** multi-repo, user-facing — `lifestack-api` (three new import modules; retire three bespoke bulk endpoints) and `lifestack-web` (route bulk entry through `/imports`; delete three client-side CSV parsers). **One api PR + one web PR** (per-module commits within each); api merges first. The web changes only wire modules into the existing `/imports` UX and delete dead paste code, so they need no dedicated frontend spec. *Coordination note:* another change is touching the Cash tab (pagination) — rebase the `DividendsSection.tsx` edit onto it to avoid a collision.
**Supersedes/absorbs:** the dividend piece of **spec-073 rev. 5** (dividends is module 1 of this initiative — see §Modules). spec-073 remains the source of truth for the dividend *event model*; this spec owns the *consolidation approach* shared across all three.
**Related:** spec-065 (net-worth history), spec-072 (historical data), the imports framework (`app/imports/`).

---

## Problem

Lifestack has **one** mature bulk-import framework (`app/imports/` + the `/imports` page) used by seven formats (spending txns/budgets, constituents, orders, transfers, CAMS/Demat CAS). It gives every format: a downloadable template, **server-side** per-row validation, preview-before-commit, an import-history list with recovery/rollback, and structured per-row/field errors.

Alongside it, **three** features grew their own **bespoke bulk-paste flows** that reimplement a weaker version of the same thing:

| Bespoke flow | UI | Endpoint (to retire) | Reuse target (stays, becomes commit engine) |
|---|---|---|---|
| Dividends | `DividendsSection.tsx` paste modal | `POST /investing/dividends/bulk` | `DividendService.bulk_import` |
| FX rate history | `HistoricalDataPanel.tsx` "FX" tab | `POST /finance/fx/history/import` | `import_historical_rates` |
| Net-worth history | `HistoricalDataPanel.tsx` "Net worth" tab | `POST /finance/net-worth/history/import` | `import_net_worth_history` |

Each shares the same defects: a client-side `parseCsv`/`parseCsvRows` (naive `split(',')`, no quoted-field handling, **no server validation** — "let the validation handle stuff" is exactly what's missing), a paste `<textarea>` instead of a file+template, a bespoke `{imported, skipped/updated, rejected}` result blob, and no preview, history, or consistent error surface. Three second-class import mental models shadowing the one good one.

## Goal

Retire the three bespoke endpoints and their paste UIs. Add three first-class import modules to the existing framework, each **reusing its existing per-row idempotency service method as the commit engine** (mirroring how `investing-orders` reuses `bulk_import_orders`). Unify all bulk entry under `/imports`; keep genuinely-quick single-value manual entry where it earns its place.

## Non-goals

- No change to the underlying data models, idempotency semantics, or validation *rules* of the three features — only the *entry surface* moves. (Dividend `external_ref` identity, FX natural-key upsert, net-worth backfill-only rule all carry over verbatim.)
- No new bulk formats beyond these three.
- No order-style batch rollback for the new modules (see Decision 3).

## Solution

### Invariants

- **INV-1 — Reuse, don't reimplement.** Each module's commit step builds the existing request object from validated preview rows and calls the existing service method. The idempotency/validation *rules* live in one place still; the framework only replaces the transport + UX.
- **INV-2 — Server-side validation replaces client parsing.** Every rule the old client `parseCsv` skipped (date parse, currency-in-workspace, decimal/`>0`, all-or-none component sets, backfill-window) is enforced in the module's `validate_*_row` at validate time, surfaced as normal per-row `ImportError`s.
- **INV-3 — No capability gap, no data-model change.** Retirement and module addition land in the same api PR; manual entry is untouched; no migration.
- **INV-4 — Cash/reconciliation safety unchanged.** The dividend module still credits snapshots via `bulk_import` (spec-073 INV-2/INV-6); FX and net-worth touch neither snapshots nor the ledger.

### Modules

Each module = an `ImportModule` enum member + `TEMPLATE_HEADERS` + `REQUIRED_HEADERS` + `template_csv` branch + a `app/imports/<name>_import.py` with `validate_*_row` (static per-row checks producing a preview payload) and `commit_*_chunk` (thin adapter → existing service method), wired into `ImportService.validate_batch_file` / `commit_batch`.

**1. `investing-dividends`** — retires `POST /investing/dividends/bulk`; commit → `DividendService.bulk_import`.
- Columns: `account, symbol, income_type, gross, tax, currency, pay_date, external_ref, notes`. Required: `account, gross, currency, pay_date`.
- Validation: account exists & is a snapshot-managed brokerage account; currency in workspace & equal to account currency (INV-3, spec-073); `income_type ∈ {dividend,interest,coupon}`; symbol only for dividend/coupon; `tax < gross`; decimals/date parse.
- Idempotency: `external_ref` upsert; ref-less exact-dup skip; ref-less `amount_mismatch` reject (spec-073 INV-5) — all unchanged, executed at commit against live DB.
- **Full design: spec-073 rev. 5.** This spec tracks it as module 1; the api work may already be partly built on `feat/dividend-import-module`.

**2. `finance-fx-rates`** — retires `POST /finance/fx/history/import`; commit → `import_historical_rates`.
- Columns: `base_currency_code, quote_currency_code, rate, as_of_date`. All required.
- Validation: both currencies active in workspace; `rate > 0`; `as_of_date` not in the future; date parse.
- Idempotency: natural key `(workspace, base, quote, as_of_date)` — exact-rate re-upload skips; changed rate upserts. No `external_ref`.

**3. `finance-net-worth-history`** — retires `POST /finance/net-worth/history/import`; commit → `import_net_worth_history`.
- Columns: `date, reporting_currency, total_net_worth, holdings_value, investing_cash, spending_cash`. Required: `date, reporting_currency, total_net_worth` (the three components are all-or-none, optional).
- Validation: `date` strictly before the workspace's earliest **live** snapshot (backfill-only, spec-065 — "live wins" is structural); components all-given-or-all-omitted; when given, `total_net_worth == holdings + investing_cash + spending_cash`; decimals/date parse.
- Idempotency: natural key `(workspace, date)` for `source='user_provided'` rows — exact match skips; changed values overwrite the user point.

### Decisions

1. **Commit-result surfacing (shared).** The framework's `ImportCommitResponse` reports only `inserted_rows`, but these modules also yield `updated/skipped/rejected` (e.g. dividend `amount_mismatch`, FX exact-skip, net-worth `date_not_backfill`). **Decision:** persist the per-module breakdown into the batch's existing `extra_json` (the generic advisory channel CAMS already uses) and surface it via a single additive optional field on the import detail/commit responses — **no new columns, one small schema field reused by all three**. Validation-time failures still surface as normal `ImportError` rows; the soft outcomes (skip/reject/upsert against live DB state) are commit-time and ride in `extra_json`.
2. **Manual single-entry retained where it earns its place.** Bulk moves to `/imports` for all three. Keep: the dividend "Record" modal (spec-073); a single-FX-rate add (the quick "unblock XIRR" affordance). Net-worth single-point manual add: keep only if the current panel already offers it usefully — a web-PR judgement, not a blocker here.
3. **Batch rollback scoped out (no migrations).** Order-style batch rollback needs a `source_import_id` per row → a migration for each of the three tables. All three are already **individually reversible**: dividends via `DELETE /investing/dividends/{id}` (spec-073 INV-6, snapshot-safe); FX via `DELETE` on the user rate; net-worth user points via overwrite/delete. **Decision:** the modules do **not** implement batch rollback in v1 (the import batch remains deletable as a *record*; its data is undone through the existing per-row paths). Adding true batch rollback is a fast-follow if volume warrants — explicitly out of scope to avoid three migrations for reversible-by-other-means data.
4. **Reuse-not-reimplement is mandatory (INV-1).** No copy of the idempotency logic into the import module — the commit adapter constructs the existing request DTO and calls the existing method, exactly like `commit_investing_orders_chunk → bulk_import_orders`.

## API changes (lifestack-api)

- **Add** `ImportModule.{investing_dividends, finance_fx_rates, finance_net_worth_history}` (string values `investing-dividends`, `finance-fx-rates`, `finance-net-worth-history`).
- **Add** template headers, required-column sets, `template_csv` branches, `validate_*_row` + `commit_*_chunk`, and dispatch wiring for each. `ImportService` gains access to the dividend / fx / net-worth services (dependency wiring in `get_import_service`).
- **Add** `<module>` breakdown to `extra_json` on commit + one optional response field (Decision 1).
- **Retire** the three bespoke endpoints (`/investing/dividends/bulk`, `/finance/fx/history/import`, `/finance/net-worth/history/import`) and their router/request/result HTTP wiring. The underlying service methods (`bulk_import`, `import_historical_rates`, `import_net_worth_history`) and their row/result DTOs **stay** as internal commit engines.

## UI changes (lifestack-web) — one web PR, not deferred

- `ImportsPage.tsx`: add the three modules to `MODULE_OPTIONS` + their preview columns.
- `DividendsSection.tsx`: delete the bulk modal + `parseCsv`; keep "Record dividend"; add "Bulk import →" link to `/imports`.
- `HistoricalDataPanel.tsx`: delete both paste `<textarea>` tabs + `parseCsvRows`; keep any single-value manual add; link to `/imports`.
- Delete the now-unused `importFxHistory` / `importNetWorthHistory` / `bulkImportDividends` service calls and their types.

## Sequencing

**One umbrella spec (this); one api PR + one web PR.** The api PR adds all three modules + retires all three endpoints, with **per-module TDD commits** (the shared `service.py` dispatch/required-headers edits make one PR cleaner than three). Order: dividends → fx → net-worth. The web PR wires all three into `/imports` and deletes the paste code. api merges before web. Branches: `feat/consolidate-bulk-paste-imports` in each repo.

## Testing & evidence (api)

- Red-first per module: `validate_*_row` rejects the documented bad inputs (bad currency/decimal/date; dividend symbol-on-interest, tax≥gross; FX future-date, rate≤0; net-worth non-backfill date, component-sum mismatch, partial components) as `ImportError`s at validate.
- Commit reuses the existing service method, so each feature's idempotency modes still hold through the framework (dividend INV-5 all modes incl. `external_ref` corrected-amount; FX exact-skip vs changed-upsert; net-worth exact-skip vs overwrite).
- `extra_json` carries the breakdown; commit/detail responses surface it (Decision 1).
- Dividend module: reconciliation discrepancy stays 0 across a committed batch (spec-073 INV-2).
- Each retired endpoint returns 404/405 (a test per endpoint asserts removal); existing tests that hit the old endpoints are migrated to the framework path.
- Coverage gate (api 80) respected; existing suites stay green with no assertion weakening.
