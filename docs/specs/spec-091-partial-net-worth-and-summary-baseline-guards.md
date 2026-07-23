# Spec 091 — Partial net-worth totals on missing FX + weekly-summary zero-baseline guards

**Status:** Implemented
**Issues:** #182, #183
**Repos:** lifestack-api (this spec), lifestack-web (rendering follow-up, one PR per repo)

## Problem

1. (#182) When one spending-account currency lacks an FX rate to the reporting
   currency, `GET /finance/net-worth` returns `total_net_worth: null` and
   `spending_total: null` (`valuation_status: "partial"`). The frontend renders
   "—" for the headline while Investing Cash and Holdings on the same page show
   real values. One missing rate blanks the whole workspace headline.
2. (#183.2) **REVISED — see note below.** A zero-value snapshot boundary
   (start or end) produces a real `week_change` with `week_change_pct: null`
   (division by zero is already guarded server-side). The frontend renders
   `null` as `toNumber(null) → 0 → "(0.00%)"` (`WeeklySummariesPage.tsx:290,
   384`) — a fabricated-looking percentage next to a real amount. This is a
   **frontend null-handling bug, not a backend one.**
   > **Note (this spec, pre-implementation):** a backend zero-baseline guard
   > (treat a zero boundary as `"unavailable"`) was already tried in commit
   > `b0f568e` and **deliberately reverted** in `e2397cc` — a zero boundary is
   > also the legitimate shape of a first-ever tracked snapshot or a genuine
   > full liquidation, and hiding the whole section as "unavailable" throws
   > away a real `week_change` to avoid an undefined percentage. **Do not
   > re-implement that guard.** The only remaining defect is the frontend
   > `(0.00%)` render for a `null` percentage — fixed in the web PR only, no
   > api change for this item.
3. (#183.3) Staleness: `data_stale` (spec-085) and `import_data_reverted`
   (spec-086) are already computed and exposed — the web app never renders
   them. Frontend-only; no api change.
4. (#183.1) "Coverage: 0 / Status: partial" beside populated charts: the
   value-weighted look-through coverage number is correct; the label omits that
   it measures *look-through decomposition*, not the rendered holdings charts.
   Frontend labeling fix; no api change.

## Change (api)

### A. Partial net-worth exposure (additive, backward-compatible)

`_compute_net_worth` already accumulates the convertible sum in
`spending_total` and then discards it when `spending_convertible` is false.
New response fields on `NetWorthResponse`:

- `spending_total_partial: Decimal | None` — sum of convertible spending
  balances; populated **only** when `valuation_status == "partial"`.
- `total_net_worth_partial: Decimal | None` — `spending_total_partial +
  investing_total`; populated only when status is `"partial"` and
  `investing_total` is not null.
- `excluded_currencies: list[ExcludedCurrency]` — `{currency_code,
  total_balance}` per currency lacking a conversion, `total_balance` being the
  native-currency sum of the affected spending balances (so the client can say
  "excludes €X"). Empty list otherwise.

Existing fields keep their exact semantics (`total_net_worth`/`spending_total`
stay null when partial — no client sees a partial number where it expects a
complete one). Snapshot persistence is untouched: **no NetWorthSnapshot row is
written from a partial valuation** (unchanged behavior; partial totals are a
display affordance, not an audit record).

Partition argument (per cash-model rules): every spending balance lands in
exactly one of {`spending_total_partial` (convertible), `excluded_currencies`
(unconvertible)}; investing values are already internally converted by the
summary service and appear only in `investing_total`. No unit counted twice.

### B. ~~Zero-baseline guard~~ — dropped, no api change

No backend change for #183 item 2 (see note above) — settled behavior from
`e2397cc` is correct and stays as-is: zero-boundary weeks remain `"complete"`
with a real `week_change` and `week_change_pct: null`. The fix is web-only
(section below).

### C. Retroactivity

Not retroactive (house policy). Stored weekly summaries generated before this
change keep their recorded sections; the guard applies on (re)generation only.
Regenerating an affected week via the existing regenerate endpoint adopts the
new behavior. No stored rows are mutated.

## Change (web — separate PR, after api merges)

1. Net Worth page: when `total_net_worth` is null and `total_net_worth_partial`
   present, render the partial with an "excludes {currency} {amount}" note (per
   currency from `excluded_currencies`); same for Spending Cash via
   `spending_total_partial`. Banner names the missing currencies instead of
   "one or more currencies".
2. Weekly Summaries page: render `data_stale` as a "data changed since
   generation — regenerate?" pill (`summary-stale-indicator` test-id, arming
   the dormant e2e check). Stop rendering a `null` `week_change_pct` as
   "(0.00%)" — omit the percentage (or show "N/A") when it's undefined, in
   both the investing and net-worth cards. This is the actual fix for #183
   item 2; no api change accompanies it.
3. Investing Analytics Controls: label coverage as look-through scope
   ("Look-through coverage: N% of portfolio value").

## Out of scope

- Backfilling/mutating stored weekly summaries or net-worth snapshots.
- Persisting partial valuations as snapshots.
- Reconciliation, ledger, FIFO — untouched.
- The api-side semantics of `snapshot_coverage`/`analysis_status` (correct as
  computed; labeling is the web fix).

## Tests (Red first)

api:
- Net worth with one unconvertible spending currency → status `"partial"`,
  `spending_total_partial` = convertible sum, `excluded_currencies` carries the
  native sum, `total_net_worth` stays null, no snapshot persisted.
- All-convertible workspace → new fields null/empty, `total_net_worth` intact
  (regression).

web: partial rendering with excludes note; stale pill from `data_stale`;
no "(0.00%)" for null pct (investing + net-worth cards); coverage relabel.
