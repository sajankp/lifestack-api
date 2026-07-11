# Spec-072: Historical Data Ingestion (user-provided FX rates + net-worth backfill)

**Created:** 2026-07-10
**Status:** Implemented (api backend) — owner-approved and built 2026-07-11. Migration `0048_historical_data_ingestion`: nullable `workspace_id` on `fx_rates` (system rows unaffected, `get_latest_rate`/`get_latest_rates_for_pairs` now explicitly filter `workspace_id IS NULL` to keep user rows out of live valuation per INV-3); `source` + nullable components on `net_worth_snapshots` with a live-completeness CHECK. Endpoints: `POST/GET/DELETE /finance/fx/history[...]`, `POST /finance/net-worth/history/import`, `GET/DELETE /finance/net-worth/history/user-points[...]`. 8 integration tests (idempotency, future-date reject, system-beats-user precedence, INV-2 boundary reject, component-sum validation, delete-reopens-gap), full suite green. **Web UI implemented** (lifestack-web, same commit series): `HistoricalDataPanel` (Net Worth page hero action) for CSV-paste import + management list/delete of both FX and net-worth backfill rows; `NetWorthHistoryChart` rewritten so the stacked component areas break into disjoint runs around any component-less point (no zero-fill misrender) while the total line and dot markers still span every point, with user-provided points rendered as a dashed amber ring + tooltip. Rev. 2 resolved the FX-store question (extend `fx_rates`, workspace-scoped user rows), fixed the net-worth schema contradiction (component columns are now nullable), closed the daily-job/user-row collision gap (backfill dates must precede live history), and documented currency behavior for user points.
**Scope:** multi-repo, user-facing — `lifestack-api` (storage + ingestion + reconstruction) and `lifestack-web` (upload/entry UI + provenance on charts). Two PRs, api merged first.
**Depends on:** spec-065 (net-worth snapshots — the table this backfills). **Unblocks:** spec-071 cross-currency XIRR aggregate (historical FX per flow).
**Related domain doc:** `docs/domain/cash-model-ledger-snapshots-reconciliation.md`.

---

## Problem

The system can only ever know the *future*. Two hard walls today:

1. **No historical FX.** FX rates are only fetched/known for recent dates. Any past-dated cross-currency computation (a 3-year-old USD buy valued in INR, or the spec-071 XIRR aggregate) has no rate to use.
2. **Net-worth history is non-retroactive.** Per spec-065 INV-3, `net_worth_snapshots` only accumulate from ship date forward — imported holdings genuinely can't be reconstructed from nothing. So the net-worth graph starts empty and fills slowly, and a user with years of real history sees a stub.

But the user often *has* this data — a spreadsheet of past FX, or simply knows what their portfolio was worth on past dates. The honest fix is to let them **supply history they already have**, clearly marked as their own input, so dashboards get depth immediately instead of waiting months.

## Goal

Let the user contribute two kinds of historical data, each **provenanced as user-provided** and never conflated with live-computed figures:

- **A. Historical FX rates** — `(base, quote, rate, as_of_date)` — unblocking past-dated conversions and the spec-071 aggregate.
- **B. Net-worth backfill points (Tier A)** — a user-stated total (optionally split into holdings/investing-cash/spending-cash) for a past date, materialized into `net_worth_snapshots` so the graph reaches back.

**Tier B (per-symbol historical holding quantities → replay-valued)** is explicitly **deferred** (see Non-goals) — Tier A delivers the "rich dashboard now" win at a fraction of the cost.

## Non-goals (this spec)

- **Per-symbol historical holdings ledger (Tier B).** Hand-entering past quantities per symbol per date and replay-valuing them (with corporate actions, splits, per-day prices) is a separate, much larger data-modeling effort. Deferred until there's demonstrated need; this spec's net-worth points cover the dashboard goal.
- Automatic historical FX *fetching* from a provider — that's a data-feed spec, not user upload. (User upload works offline and needs no vendor.)
- Overwriting or "correcting" computed live snapshots. User points only fill dates that have **no** live snapshot (INV-2).

## Solution

### Invariants (must hold)

- **INV-1 — Provenance is explicit and durable.** Every user-supplied row carries `source = 'user_provided'` (vs `'system'`/`'live'`). It is queryable, rendered distinctly on charts (e.g. dashed/annotated segment), and exportable with its provenance intact. No surface ever presents user-provided history as computed truth.
- **INV-2 — Live wins; user fills gaps only, strictly in the past.** A user point is accepted only for dates **strictly before the workspace's earliest live snapshot** (or before today, when no live rows exist yet); anything later is rejected per-row with reason `date_not_backfill`. This makes "live wins" structural rather than procedural: the daily snapshot job can never collide with a user row on the `(workspace, snapshot_date)` unique constraint, and no interleaving of provenance mid-series is possible. When live history later can't reach back (it never will — spec-065 INV-3), the boundary stays fixed. This preserves spec-065 INV-1 (display cash is always live).
- **INV-3 — User FX is scoped and lowest-priority.** A user-provided historical rate is used **only** when no system rate exists for that (base, quote, date). System/fetched rates always take precedence. User FX never affects *today's* live valuation (which uses current rates) — only past-dated reconstruction and the spec-071 aggregate.
- **INV-4 — Ingestion is idempotent and validated.** Upload is upsert-keyed on (workspace, base, quote, date) for FX and (workspace, date) for net-worth points. Rows are validated (positive rates, sane date bounds, currency codes exist, total = sum of components if components given) and a per-row reject list is returned — a bad row never silently corrupts the set, and re-uploading the same file is a no-op.
- **INV-5 — Reversible.** Every user-provided row is individually deletable and the whole user-provided set is clearable, restoring the "history starts at ship date" state. Deletion of a user point that shadowed a gap simply reopens the gap.

### A. Historical FX (lifestack-api)

**Schema — extend the existing `fx_rates` table (decided; no parallel store).** `fx_rates` already carries `rate`, `as_of`, and a `source` column with a unique constraint over `(base, quote, as_of, source)` — a second FX table would mean two resolver paths forever. User history becomes rows in the same table:

| Change | Detail |
|---|---|
| `workspace_id` | new **nullable** FK → `workspaces.id`, indexed. `NULL` = system/global row (all existing rows, unchanged via server default NULL); set = user-provided, visible only to that workspace |
| `source` | user rows use `source = 'user_provided'` (existing column; INV-1 provenance) |
| `as_of` | user rows store the date at midnight UTC (`as_of` is a datetime; historical resolution matches on `DATE(as_of)`) |
| new partial unique index | `uq_fx_rate_user_row` on `(workspace_id, base_currency_code, quote_currency_code, as_of) WHERE workspace_id IS NOT NULL` — the upsert key for user rows (INV-4). Needed because the existing constraint treats NULL `workspace_id` per-row; system rows keep `uq_fx_rate_pair_asof_source` unchanged |

Migration: `ALTER TABLE fx_rates ADD COLUMN workspace_id ...` (nullable, no backfill) + the partial index; working `downgrade()` drops both. No existing row changes.

- **Ingestion**: `POST /finance/fx/history/import` accepting CSV (`base,quote,date,rate`) and/or a JSON array; returns `{ imported, skipped, rejected: [{row, reason}] }`. Manual single-row entry via the same endpoint (array of one). Validation per INV-4: rate > 0, known currency codes, date not in the future.
- **Consumption**: the historical FX resolver gains a precedence tier — **system rate for that date → user rate for that date → none** (INV-3). Used by past-dated valuation and spec-071's aggregate. Never used for present-day live figures, which keep using current system rates only.

### B. Net-worth backfill points (lifestack-api)

**Schema — `net_worth_snapshots` migration (two changes, one revision).** Reuse the spec-065 table; today it cannot store a bare-total user point because the component columns are NOT NULL:

| Change | Detail |
|---|---|
| `source` | new `String(20)` NOT NULL, `server_default 'live'` (existing rows become `'live'` for free), CHECK IN (`live`, `user_provided`) — string + CHECK per house pattern, not a native enum |
| `holdings_value`, `investing_cash`, `spending_cash` | become **nullable**. Currently `NOT NULL`, which contradicts an optional component split. Live rows always populate all three (the snapshot-job writer is unchanged and this is asserted); user rows may carry only `total_net_worth`. When components are given, CHECK/service validation enforces `total = holdings + investing_cash + spending_cash` |
| `fx_rates_used` | user rows store `{}` — a user point has no FX context and is **fixed in its stated `reporting_currency`** (see currency note below) |
| unique constraint | unchanged — `(workspace_id, snapshot_date)` remains the upsert key (INV-4) |

Downgrade: re-`NOT NULL` requires no NULLs — `downgrade()` deletes `source='user_provided'` rows first (acceptable: reversibility of user data is INV-5 by design), then restores constraints and drops `source`.

- **Ingestion**: `POST /finance/net-worth/history/import` — CSV/JSON of `{ date, total_net_worth, holdings_value?, investing_cash?, spending_cash?, reporting_currency }`. Upsert-keyed (workspace, date); rows dated on/after the earliest live snapshot are rejected with `date_not_backfill` (INV-2); returns the same `{imported, skipped, rejected}` shape. Components all-or-none per row.
- **Read**: `GET /finance/net-worth/history` gains `source` per row so the chart can distinguish. No new read endpoint.
- **Currency behavior (explicit):** live rows can be re-derived under a changed reporting currency via `fx_rates_used`; user rows cannot. A user point whose `reporting_currency` differs from the workspace's current reporting currency renders as a **gap** (not converted, not hidden from the management list) unless a user-provided historical FX rate for that date exists, in which case it converts via the section-A resolver and is flagged as converted in the tooltip.

### C. UI (lifestack-web)

- On the **Net Worth page**: an "Add historical data" affordance → a small modal/panel with (i) CSV upload with a downloadable template + inline reject feedback, and (ii) a manual "add a point" form (date + amount, optional component split). A companion "Historical FX" uploader (same pattern) reachable from settings or the same panel.
- **Chart provenance**: user-provided segments/points rendered distinctly (dashed line, lighter fill, or a legend chip "user-provided") with a tooltip note; live segments unchanged. Rows with NULL components appear on the **total line only** and are excluded from the stacked-area component view (no zero-fill — a fabricated all-zero component day would misrender the stack). Follows the dataviz house style, theme- and currency-preference-aware.
- A management view to list/delete user-provided points and FX rows (INV-5).

## Now vs. Proposed

| Aspect | Now | Proposed |
|---|---|---|
| Historical FX | none (recent-only) | user-uploadable `(base,quote,date,rate)`, lowest-priority fallback (INV-3) |
| Net-worth graph depth | starts at ship date, fills forward | user can backfill past points → immediate depth (INV-2) |
| Cross-currency past valuation / XIRR aggregate | impossible (no rate) | possible where user supplied the rate |
| Provenance | n/a | every user row tagged, chart-distinct, deletable (INV-1/5) |

## Testing & evidence

- FX resolver precedence tests: system > user > none per date; user FX never used for present-day live figures; user rows invisible to other workspaces.
- FX migration tests: existing global rows untouched; partial unique index enforces the user-row upsert key.
- Ingestion tests: idempotent re-upload; per-row rejects (bad rate/date/currency, future date, `date_not_backfill`); components all-or-none and sum validation.
- INV-2 test: user point dated on/after the earliest live snapshot is rejected; earlier dates fill; deleting reopens the gap; daily job runs cleanly with user rows present (no constraint collision possible).
- Snapshot-job assertion: live rows always write all three components (guards the nullable-columns migration against writer regression).
- Currency test: user point in a non-current reporting currency renders as a gap; converts (flagged) when a user FX rate for that date exists.
- Web: reject-feedback render, provenance styling on chart, total-line-only for component-less points, delete flow.
- Coverage gate respected.

## Decisions (resolved rev. 2, 2026-07-10)

1. **User FX scope — DECIDED: workspace-scoped**, as nullable `workspace_id` on the existing `fx_rates` table (NULL = system/global). User assumptions, not shared truth; one table, one resolver.
2. **Component split — DECIDED: optional (all-or-none per row).** Requires making the three component columns nullable (they are NOT NULL today — see §B); a bare total draws the line, components additionally enable the stacked view for those dates.
3. **Tier B (per-symbol historical holdings) — DECIDED: stays deferred.** Revisit only if Tier A proves insufficient.

## Phasing note

The two halves are independently shippable within the api PR sequence: **FX first** (small, mechanical, and the hard dependency for spec-071's aggregate), net-worth backfill second. If scope pressure hits, the net-worth half can split into its own follow-up PR without touching the FX design.
