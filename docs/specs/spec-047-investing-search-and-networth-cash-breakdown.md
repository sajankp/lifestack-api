# Spec-047: Investing Substring Search + Net-Worth Brokerage Cash Breakdown

**Created:** 2026-06-30
**Status:** Draft
**Depends on:** spec-008 (Investing MVP), spec-011 (transfers/FX), spec-007 (dashboard/net worth reads)

---

## Problem

Three small discoverability/visibility gaps on the investing and net-worth surfaces:

1. **Orders filter is exact-match only.** `GET /investing/orders?symbol=` matches `InvestingOrder.symbol == symbol.upper()` (`app/investing/repository.py`). Typing `NVD` returns nothing, and it can't match an instrument *name* — useless for mutual funds whose symbol is a numeric folio (e.g. `152981`) with the real name on the joined `Instrument`.
2. **Holdings list has no text filter at all** — only account/currency/type dropdowns (`InvestingPage.tsx`). Same MF problem: the only searchable text is the numeric symbol.
3. **Net worth shows investing cash only as one lumped number.** Spending accounts are itemized in their own table, but brokerage cash is a single `investing_cash_total`. There's no per-account breakdown, which is exactly what's needed to reconcile idle cash per brokerage account.

## Solution

### A. Orders substring search (backend + frontend)

- `InvestingOrderRepository.list_by_workspace` gains a `search: str | None` parameter. When set, LEFT JOIN `Instrument` on `instrument_id` and filter `or_(InvestingOrder.symbol ILIKE %q%, Instrument.name ILIKE %q%)`. The existing exact `symbol` param is retained for API back-compat.
- `InvestingOrderService.list_orders` and `GET /investing/orders` pass a new `search` query param through.
- Frontend: the existing "Filter by symbol" input sends `search` instead of `symbol`; placeholder becomes "Filter by symbol or name…".

### B. Holdings substring filter (frontend only)

- Add a search text box to the Holdings tab. Holdings are already fully loaded client-side; filter case-insensitively on `symbol` **or** the resolved instrument display name (the page already builds `instrumentBySymbol`). No backend change.

### C. Net-worth per-brokerage-account cash breakdown (backend + frontend)

- `GET /finance/net-worth` adds `investing_accounts: list[InvestingAccountBalance]` — one row per `(brokerage account, currency)` from `CashBalanceRepository.get_latest_per_account_currency`, with `balance` and `balance_in_reporting_currency` (converted via the same `_convert_to_reporting` + FX lookup as spending accounts, extended to investing currencies). `investing_cash_total` is unchanged (still the summary's converted total).
- Frontend: render a "Brokerage cash" table on the net-worth page parallel to the spending-accounts table.

## API / schema impact

- New `InvestingAccountBalance` schema (account_public_id, account_name, currency_code, balance, balance_in_reporting_currency) and `NetWorthResponse.investing_accounts`.
- `GET /investing/orders` gains optional `search`; response shape unchanged.
- No migration.

## Out of scope

- Reconciling the seed-data cash imbalances themselves (missing Shahma/Paasa orders, over-stated GROWW deposits) — a data task this view is meant to *surface*, handled separately via UI edits.
- Instrument-name matching on the holdings tab beyond the already-resolved display name; account-listing/grouping changes deferred earlier.

## Test plan

- Orders `search`: substring on symbol and on instrument name both return the order; non-matching returns empty; exact `symbol` still works.
- Net worth: `investing_accounts` lists each brokerage account's cash with converted value; sums are consistent with `investing_cash_total`.
- Holdings filter (FE): typing part of a symbol or MF name narrows the list.
