# Spec-073: Dividend & Income Tracking

**Created:** 2026-07-10
**Status:** Implemented (api backend) — owner-approved and built 2026-07-10. Backend: migration `0047_investing_dividends`, `Dividend` model/service/router, reconciliation INV-2 integration; 10 integration tests, full suite green (649 passed, 83% coverage). Web UI (Cash tab entry + bulk upload) shipped in the same PR series (lifestack-web). Rev. 2 fixed the bulk-import identity (external_ref + amount-mismatch rejects instead of amount-in-key), corrected the deletion mechanism (no order-style replay; see rev. 4 note in INV-6), and tightened the data-model section (constraints, indexes, enum pattern). Rev. 3 resolved the open questions: both entry paths (UI + upload) in v1, detection-only migration helper, interest included as account-level income on brokerage accounts only. **Implementation note:** the holding_id/symbol both-or-neither CHECK proposed in rev. 3 was dropped — it rejected the legitimate case of a dividend on an already-exited symbol (no matching Holding row); see §A.
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
- `POST /investing/dividends/bulk` — CSV/JSON import (`symbol,account,pay_date,gross,tax,currency,external_ref?`), returns `{imported, updated, skipped, rejected:[{row,reason}]}` (INV-5): `external_ref` rows upsert; ref-less rows insert, no-op on exact re-upload, and reject with `amount_mismatch` when the fallback key matches at a different amount.
- Dividends included in the investing performance inputs consumed by spec-071.

### C. UI (lifestack-web)

- On the **Investing → Cash tab** (where transfers/cash live) or a new **Income** section: "Record dividend" modal (account, optional symbol, gross, tax, pay date, currency) and a dividends list with edit/delete, mirroring the orders UX.
- **Bulk upload** with a downloadable template + inline reject feedback (same pattern as spec-072 imports).
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

1. **Home in the UI — DECIDED: Cash tab, with BOTH entry paths in v1** — a manual "Record dividend/income" modal *and* bulk upload. Promote to a dedicated Income tab only if volume warrants.
2. **Migration helper — DECIDED: v1 ships read-only detection** of transfer-modeled dividends; conversion is a follow-up once the event model is proven.
3. **`income_type` breadth — DECIDED: interest included from v1** (single string+CHECK set: `dividend` | `interest` | `coupon`; UI leads with dividends). Clarifications that follow from the cash model:
   - **Interest is account-level**: `holding_id`/`symbol` are NULL — no stock attribution, tagged to the crediting account only.
   - **This table applies to brokerage (snapshot-managed) accounts only** — `account_id` must be a snapshot-managed investing account, validated at the service layer. Interest on a bank/wallet account is *not* recorded here: those are ledger-managed, and their interest is already representable as an ordinary income `spending_transaction`. Routing it through this table would violate the write matrix (income events credit snapshots; spending accounts have no snapshots).
   - **Tax-planning groundwork (deliberate):** `gross_amount`, `tax_withheld`, `income_type`, and `pay_date` are kept per-event precisely so a future tax spec can compute financial-year dividend/interest income and TDS credit without re-modeling — no aggregation-only shortcuts.
