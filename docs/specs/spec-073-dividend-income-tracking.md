# Spec-073: Dividend & Income Tracking

**Created:** 2026-07-10
**Status:** Implemented (api backend) — owner-approved and built 2026-07-10. **Rev. 5 (2026-07-11, approved-in-principle, api-first):** the bespoke bulk-upload path is retired in favour of the shared imports framework — `POST /investing/dividends/bulk` and its Cash-tab paste-CSV modal are removed; a new first-class `investing-dividends` import module (template → validate → preview → commit) replaces them, reusing the same `DividendService.bulk_import` idempotency engine. See §Rev. 5 below for the retirement, the two design decisions (commit-result surfacing; batch-rollback scoped out), and the sequencing (api now; web wiring deferred behind an in-flight Cash-tab pagination change). **This is now module 1 of the umbrella initiative in [spec-074](spec-074-consolidate-bulk-paste-imports.md)**, which applies the identical treatment to FX-rate and net-worth history imports; spec-074 owns the shared consolidation approach and sequencing, this spec owns the dividend event model. Original build note follows. Backend: migration `0047_investing_dividends`, `Dividend` model/service/router, reconciliation INV-2 integration; 10 integration tests, full suite green (649 passed, 83% coverage). Web UI (Cash tab entry + bulk upload) shipped in the same PR series (lifestack-web). Rev. 2 fixed the bulk-import identity (external_ref + amount-mismatch rejects instead of amount-in-key), corrected the deletion mechanism (no order-style replay; see rev. 4 note in INV-6), and tightened the data-model section (constraints, indexes, enum pattern). Rev. 3 resolved the open questions: both entry paths (UI + upload) in v1, detection-only migration helper, interest included as account-level income on brokerage accounts only. **Implementation note:** the holding_id/symbol both-or-neither CHECK proposed in rev. 3 was dropped — it rejected the legitimate case of a dividend on an already-exited symbol (no matching Holding row); see §A.
**Scope:** multi-repo, user-facing — `lifestack-api` (dividend event model, cash + reconciliation integration, import) and `lifestack-web` (entry + upload + display). Two PRs, api merged first.
**Depends on:** spec-040 (transfer-inclusive ledger & reconciliation), spec-048 (orders in reconciliation), spec-044 (FIFO — for per-holding attribution). **Feeds:** spec-071 (dividend as income flow, INV-6). Related domain doc: `docs/domain/cash-model-ledger-snapshots-reconciliation.md`.

---

## Problem

There is no first-class way to record investment income (dividends, interest, coupons). To make the cash appear in a brokerage account, the user currently records a **transfer from their wallet/bank into the brokerage account**. That is wrong in two compounding ways:

1. **It corrupts the cash ledger on both sides.** The wallet shows an outflow that never happened, and the brokerage inflow is attributed to a transfer of the user's own capital rather than to income the investment produced. Reconciliation "balances" only because the fiction is symmetric — the underlying accounts are both wrong.
2. **It silently sabotages return metrics.** A dividend is a *return*. Modeled as a contribution (transfer-in), it counts as capital the user *added*, which **understates XIRR and total return** (spec-071). The better the dividends, the worse the mismodeled return looks.

A dividend is economically distinct from a transfer: cash **enters** the brokerage account from **outside the user's own accounts**, attributable to a holding. The model must represent that directly.

## Goal

A first-class **income event** — primarily dividends, extensible to interest/coupons — that:
- credits brokerage cash with **no offsetting debit** from the user's other accounts,
- is attributable to a holding/symbol + account (and nullable when account-level, e.g. cash interest),
- participates correctly in **reconciliation** (part of the projected ledger),
- is recordable **manually and by bulk upload** (broker statements list them),
- and is consumed by **spec-071** as income (positive, non-contribution) — fixing the understated-return bug.

## Non-goals (this spec)

- Automatic dividend *fetching* from a provider / corporate-actions feed — future spec. This is user-entered / uploaded.
- Dividend *forecasting* or ex-date scheduling.
- Tax-lot / withholding-tax modeling beyond a simple optional `tax_withheld` field (full tax treatment is its own concern).
- DRIP (dividend reinvestment) auto-creating buy orders — v2; v1 records the cash, user places any reinvestment buy themselves.

## Solution

### Invariants (must hold)

- **INV-1 — Income is not a transfer.** A dividend credits brokerage cash and has **no** counterparty debit in any user account. It is a distinct event type (`trigger_type = 'dividend'`), never represented as a transfer. This is the structural correction of the current workaround.
- **INV-2 — Reconciliation-complete.** Dividend credits are included in the account's **projected ledger** (alongside transactions, transfers, orders) so `projected_balance` still matches the cash snapshot. A dividend that credits cash but is absent from the projection would manufacture a permanent discrepancy — explicitly tested against.
- **INV-3 — Attribution optional but currency-consistent.** A dividend may reference a holding/symbol (for per-holding income + yield) or be account-level (cash interest). Its currency must match the crediting account/cash currency; cross-currency income is recorded in the received currency (FX handled by the same historical-FX machinery as elsewhere, not re-invented here).
- **INV-4 — Return-metric contract.** Dividends surface to spec-071 as **realized income**, entering XIRR as a positive flow at pay date that is **excluded from contributions/invested capital** (spec-071 INV-6). This spec owns the event; 071 owns the math.
- **INV-5 — Idempotent import + reversible.** Bulk upload is idempotent: when a row carries an `external_ref` (broker statement line id), that is the identity — `(workspace, account, external_ref)` upserts cleanly, including corrected amounts on re-upload. Without an `external_ref`, the fallback identity is `(workspace, account, symbol, pay_date)`; a re-uploaded row matching that key with a **different amount is rejected** (reason `amount_mismatch` — resolve manually or supply an `external_ref`), never silently deduped or duplicated. Amount is **not** part of any identity key: two legitimate same-day, same-symbol dividends (e.g. two folios of one fund) require distinct `external_ref`s or manual entry. Each dividend is individually editable/deletable.
- **INV-6 — Deletion reverses the cash credit via the transfer pattern.** Dividends never touch holdings or FIFO lots, so there is **no order-style replay**. Create appends a snapshot credit (`prev + net`) with `trigger_ref = <dividend public_id>`; delete removes that linked snapshot row, and edit of a cash-affecting field removes it and appends a recomputed credit — **exactly the mechanism `CapitalTransferService` uses**, including its guard: when a *newer* cash snapshot exists on the account, the delete/edit is refused with a conflict (the linked row is no longer the head of the series, so removing it would corrupt later balances). *(Rev. 4 correction: rev. 2 described this as an appended compensating debit row; the implementation follows the established transfer-deletion pattern instead — same house mechanism, same conflict guard.)*

### A. Data model (lifestack-api)

New `investing_dividends` (income event) table, investing domain:

| Column | Type | Constraints / notes |
|---|---|---|
| id | int PK | |
| public_id | uuid | unique, indexed — API identity |
| workspace_id | FK → workspaces.id | NOT NULL, indexed |
| user_id | FK → users.id | NOT NULL |
| account_id | FK → accounts.id | NOT NULL — crediting account; must be a snapshot-managed brokerage account (service-layer validation; see Decisions §3) |
| holding_id | FK → holdings.id, nullable | opportunistic link to a currently-existing `Holding` row for `symbol`/`account`; NULL whenever no such holding exists (e.g. an already-exited position) or for account-level income — not a reliable attribution signal by itself |
| symbol | str(64), nullable | the actual user-facing attribution; set for a dividend, NULL for account-level income (e.g. cash interest). **No CHECK pairs it with `holding_id`** — a dividend on a fully-sold symbol correctly has `symbol` set and `holding_id` null (caught in implementation: an earlier draft's both-or-neither CHECK rejected exactly this case) |
| income_type | str(20) | CHECK IN (`dividend`, `interest`, `coupon`) — string + CHECK per house pattern, **not** a native PG enum (avoids enum-migration churn; see MEMORY-BACKEND named-enum trap) |
| gross_amount | Numeric(18,2) | CHECK `> 0` |
| tax_withheld | Numeric(18,2) | NOT NULL default 0, CHECK `>= 0` |
| net_amount | Numeric(18,2) | CHECK `net_amount = gross_amount - tax_withheld` and `> 0`; this is what hits cash |
| currency | str(10) | FK → currencies.code; must equal the crediting account's `default_currency_code` (INV-3, enforced at service layer — consistent with spec-050 one-currency-per-account) |
| pay_date | date | NOT NULL — the return/flow date |
| external_ref | str(128), nullable | broker statement line id; import identity when present (INV-5) |
| notes | str, nullable | |
| created_at / updated_at | timestamptz | |

Indexes / constraints beyond the above:
- Partial unique index `uq_dividend_external_ref` on `(workspace_id, account_id, external_ref) WHERE external_ref IS NOT NULL` — DB-level idempotency for ref-carrying imports.
- Index on `(workspace_id, account_id, pay_date)` — list/filter and reconciliation-projection query path.
- The fallback import identity `(workspace, account, symbol, pay_date)` is enforced at the application layer only (a DB constraint there would forbid legitimate same-day multi-folio dividends entered with distinct `external_ref`s).

Integration:
- **Cash**: create appends an `investing_cash_balances` credit row with `trigger_type='dividend'`, `trigger_ref=<dividend public_id>` (mirrors transfers/orders); delete removes the linked row and edit replaces it with a recomputed credit, both guarded against newer snapshots (INV-6). Resolution always via `get_by_trigger_ref_and_account`, never the unscoped lookup.
- **Reconciliation**: extend the projected-ledger query with a `+ dividend.net_amount` term (INV-2); `ReconciliationSummary` gains a `dividend_count` for transparency.
- **Migration**: single `op.create_table` with inline CHECK constraints; working `downgrade()` (drop table). No change to existing rows.

### B. API (lifestack-api)

- `POST /investing/dividends`, `GET /investing/dividends` (filter by account/symbol/date range, paginated), `PATCH`/`DELETE /investing/dividends/{id}` — same shape/conventions as the orders endpoints.
- ~~`POST /investing/dividends/bulk`~~ **(retired in rev. 5)** — bulk upload now runs through the shared imports framework as the `investing-dividends` module (`app/imports/`), not a bespoke endpoint. The idempotency contract (INV-5) is unchanged and still lives in `DividendService.bulk_import`, which the module's commit step calls (mirroring how `investing-orders` reuses `bulk_import_orders`). See §Rev. 5.
- Dividends included in the investing performance inputs consumed by spec-071.

### C. UI (lifestack-web)

- On the **Investing → Cash tab** (where transfers/cash live) or a new **Income** section: "Record dividend" modal (account, optional symbol, gross, tax, pay date, currency) and a dividends list with edit/delete, mirroring the orders UX.
- **Bulk upload** — **(rev. 5)** no longer a bespoke Cash-tab modal. The Cash-tab dividends section keeps only manual entry ("Record dividend") and shows a secondary **"Bulk import →"** link to `/imports`, where `investing-dividends` is a selectable module with a downloadable template, server-side validation, preview-before-commit, and per-row reject feedback — consistent with every other bulk format (orders, transfers, CAMS/Demat CAS). This deletes the client-side `parseCsv` and the paste-CSV `<textarea>` entirely.
- **Display**: per-holding income shown on the holding/trade-history view; a "Dividends / income" line in the Investing summary; once spec-071 lands, income folds into total-return and (fast-follow) a dividend-yield stat. Emerald/positive convention.
- **Migration nudge (optional):** a helper to find existing wallet→brokerage transfers the user tagged as dividends and offer to convert them to income events — reversing the current workaround. Gated, explicit, never automatic.

## Now vs. Proposed

| Aspect | Now | Proposed |
|---|---|---|
| Recording a dividend | fake wallet→brokerage transfer | first-class income event, no counterparty debit (INV-1) |
| Wallet/bank balance | wrong (phantom outflow) | correct (untouched by income) |
| Brokerage cash source | mislabeled "transfer" | labeled `dividend` (INV-1) |
| Reconciliation | balances via symmetric fiction | balances via real dividend credit in projection (INV-2) |
| Return metrics | dividend = contribution → understated return | dividend = income flow → correct XIRR/total return (INV-4, spec-071) |
| Per-holding income / yield | invisible | tracked and displayable |

## Testing & evidence

- INV-1: recording a dividend leaves all non-crediting accounts untouched; no transfer row created.
- INV-2: projected ledger includes dividend credits; reconciliation discrepancy stays 0 across record/edit/delete.
- INV-5: idempotent re-import (both identity modes); `amount_mismatch` reject on ref-less corrected rows; `external_ref` re-upload with new amount updates in place; same-day same-symbol rows with distinct refs both import.
- INV-6: delete removes the linked snapshot credit (no replay) and edit replaces it; both are refused with a conflict when a newer snapshot exists; reconciliation discrepancy stays 0 throughout.
- Attribution/currency: symbol-attributed vs account-level; currency-mismatch rejected (INV-3).
- Spec-071 seam (once both exist): dividend enters XIRR as positive non-contribution flow; total return rises vs the old transfer model.
- Coverage gate respected.

## Decisions (owner-resolved 2026-07-10)

1. **Home in the UI — DECIDED: Cash tab, with BOTH entry paths in v1** — a manual "Record dividend/income" modal *and* bulk upload. Promote to a dedicated Income tab only if volume warrants. **Revised in rev. 5:** manual entry stays on the Cash tab; *bulk* moves to the shared `/imports` page (the Cash tab links to it). The two-entry-paths intent is preserved — bulk simply lives where every other bulk format already lives, instead of a one-off modal.
2. **Migration helper — DECIDED: v1 ships read-only detection** of transfer-modeled dividends; conversion is a follow-up once the event model is proven.
3. **`income_type` breadth — DECIDED: interest included from v1** (single string+CHECK set: `dividend` | `interest` | `coupon`; UI leads with dividends). Clarifications that follow from the cash model:
   - **Interest is account-level**: `holding_id`/`symbol` are NULL — no stock attribution, tagged to the crediting account only.
   - **This table applies to brokerage (snapshot-managed) accounts only** — `account_id` must be a snapshot-managed investing account, validated at the service layer. Interest on a bank/wallet account is *not* recorded here: those are ledger-managed, and their interest is already representable as an ordinary income `spending_transaction`. Routing it through this table would violate the write matrix (income events credit snapshots; spending accounts have no snapshots).
   - **Tax-planning groundwork (deliberate):** `gross_amount`, `tax_withheld`, `income_type`, and `pay_date` are kept per-event precisely so a future tax spec can compute financial-year dividend/interest income and TDS credit without re-modeling — no aggregation-only shortcuts.

---

## Rev. 5 — bulk upload moves to the imports framework (2026-07-11)

**Why.** The rev. 2–3 bulk path was a bespoke Cash-tab modal: a paste-CSV `<textarea>` parsed *client-side* (naive `split(',')`, no quoted-field or template support) firing `POST /investing/dividends/bulk`, with a bespoke `{imported, updated, skipped, rejected}` result. Every *other* bulk format in the product (spending txns/budgets, orders, transfers, CAMS/Demat CAS) already runs through one mature framework — `app/imports/` + the `/imports` page — with a downloadable template, server-side validation, preview-before-commit, an import-history list, and structured per-row/field errors. The dividend path was a second, weaker bulk-import mental model. Rev. 5 deletes it and makes dividends a first-class module in the existing framework. This also buys dividends **preview-before-commit**, which matters more here than for orders because a committed row mutates cash snapshots (INV-6).

**Scope & sequencing (two PRs, api-first; UI deferred).**
- **api PR (now):** add the `investing-dividends` import module and **retire** the bespoke endpoint.
  - `ImportModule.investing_dividends = "investing-dividends"`.
  - `TEMPLATE_HEADERS` + template row; `_REQUIRED_COLUMNS` entry; `template_csv` branch.
  - `app/imports/investing_dividends_import.py`: `validate_dividend_row` (shape/enum/decimal/date checks, symbol-vs-account-level rules, currency-in-workspace) and `commit_dividends_chunk`, which builds a `DividendBulkImportRequest` from preview rows and calls the **existing** `DividendService.bulk_import` — INV-5 idempotency is reused verbatim, not reimplemented (mirrors `commit_investing_orders_chunk` → `bulk_import_orders`).
  - Wire the validate/commit dispatch in `ImportService`.
  - **Retire** `POST /investing/dividends/bulk` (router) + the `DividendBulkImport{Request,Result,RejectedRow}` HTTP wiring. `DividendService.bulk_import` and its row/result models **stay** — they are now the module's commit engine (internal, no longer HTTP-exposed). No capability gap: manual entry is untouched and bulk still works end-to-end through the framework.
- **web PR (later, after the in-flight Cash-tab pagination change lands):** add `investing-dividends` to `MODULE_OPTIONS` + preview columns in `ImportsPage.tsx`; delete the bulk modal + `parseCsv` from `DividendsSection.tsx`, leaving manual entry + a "Bulk import →" link to `/imports`. Deferred to avoid colliding with another agent editing the Cash tab.

**Decision 5a — commit-result surfacing.** The framework's `ImportCommitResponse` reports only `inserted_rows`. Dividend idempotency yields `updated / skipped / rejected(amount_mismatch)` too. **Decision:** persist the full `DividendBulkImport`-style breakdown into the batch's existing `extra_json` (the same generic advisory channel CAMS uses for skipped/suspected rows) and surface it in the commit response — no new columns, no framework-wide schema change. Rows that fail *validation* still surface as normal `ImportError` rows at validate time; the `rejected(amount_mismatch)` case is a *commit-time* outcome against live DB state (an existing row at a different amount) and rides in `extra_json`.

**Decision 5b — batch rollback scoped out of v1 (no migration).** Orders support batch rollback via `source_import_id` on the order row; `investing_dividends` has no such column, so batch-rollback would need a **schema migration**. But a dividend is already individually reversible via `DELETE /investing/dividends/{id}` (INV-6, snapshot-credit removal with the newer-snapshot conflict guard). **Decision:** v1 of the module does **not** implement order-style batch rollback; the import batch is still deletable as a record, and its dividends are undone individually through the existing delete path. Adding `source_import_id` + true batch-rollback is a fast-follow if volume warrants — explicitly out of scope here to avoid a migration for a reversible-by-other-means event.

**Testing (api).** Red-first: module validation rejects (bad enum/decimal/date, symbol on account-level income, currency not in workspace); commit reuses `bulk_import` so INV-5 modes (external_ref upsert incl. corrected amount, ref-less exact-dup skip, ref-less `amount_mismatch` reject, distinct-ref same-day rows) hold through the framework; `extra_json` carries the breakdown (5a); reconciliation discrepancy stays 0 across a committed batch (INV-2). A test asserts `POST /investing/dividends/bulk` is gone (404/405). Coverage gate (80) respected.
