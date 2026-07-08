# Spec-050: One Account, One Currency

**Created:** 2026-07-02
**Status:** Implemented (api#98, merged 2026-07-05)
**Depends on:** spec-048 (unified cash view), spec-049 (transfer outflow snapshot), the cash
model documented in `docs/domain/cash-model-ledger-snapshots-reconciliation.md`

---

## Problem

Two related data-quality gaps surfaced from a real production incident (a manually-entered
ICICI cash balance meant to backfill pre-tracking history):

1. **Nothing enforces that a cash balance, order, or transfer's currency matches the
   account it's on.** `default_currency_code` is set at account creation but never
   checked again anywhere. This is exactly the class of bug that caused the IND Money
   transfer incident (fixed ad-hoc via `seed_data/scripts/fix_transfer_currency_mislabels.py`)
   — nothing in the schema prevents it from recurring.
2. **`InvestingSummaryService.get_summary`'s `cash_total` sums every cash-balance row in
   the workspace with no account-type filter.** Cash balances are legitimately added to
   *any* account type for reconciliation purposes (`GET /finance/accounts/{id}/reconciliation`'s
   `snapshot_balance` — see `test_reconciliation_happy_path`, which adds one to a `"bank"`
   account). But that same row also gets summed into `investing_cash_total`/Net Worth's
   investing side, double-counting any non-brokerage account that has both ledger activity
   (`spending_total`) and a cash-balance snapshot.

## Solution

### A. One account, one currency (enforced going forward, not retroactive)

`default_currency_code`, set at account creation, becomes the account's fixed currency for
every cash-affecting entry on it. Validation added at each entry point that accepts a
currency independent of the account:

- **Cash balances** (`InvestingCashBalanceService.create_cash_balance` /
  `update_cash_balance`): `currency` must equal the account's `default_currency_code`.
  Applies to *every* account type (reconciliation snapshots on bank/wallet accounts
  included) — the invariant is account-wide, not brokerage-specific.
- **Orders** (`InvestingOrderService.place_order`, via `_validate_brokerage_account`):
  `currency` must equal the account's `default_currency_code`. (Orders are already
  brokerage-only; `InvestingOrderUpdate` has no `currency` field, so only create needs
  the check.)
- **Transfers** (`CapitalTransferService.create_transfer` / `update_transfer`):
  `from_currency_code` must equal `from_account.default_currency_code`, and
  `to_currency_code` must equal `to_account.default_currency_code`, independently.

This removes the ability for a single account to hold cash/holdings in more than one
currency (a real prior capability — see the pre-existing multi-currency test, restructured
to use two single-currency accounts instead of one dual-currency account) in exchange for
eliminating an entire class of currency-mislabeling bugs at the source, rather than
catching them after the fact via a one-off correction script.

Not retroactive: existing rows created before this validation are untouched.

### B. Investing/Net-Worth aggregation excludes non-brokerage cash

`InvestingSummaryService.get_summary`'s `cash_total` (and therefore `investing_cash_total`
in Net Worth) now filters cash-balance rows to brokerage accounts only. A cash balance on
a bank/wallet account continues to work exactly as before for reconciliation's
`snapshot_balance` — it just no longer leaks into the investing side, fixing the
double-count where such an account's balance was counted twice (once via
`spending_total`'s ledger, once via `cash_total`'s unfiltered sum).

Considered and rejected: blocking cash-balance creation for non-brokerage accounts
entirely. Reconciliation's core mechanism *requires* being able to add a snapshot to any
account type — blocking it would silently break every reconciliation test and the feature
itself, not just prevent the mistake that prompted this spec.

### C. `GET /investing/cash-balances` gains a server-side `account_id` filter

Separate but discovered while cleaning up the incident that prompted this spec: the Cash
tab's balances table always fetched a fixed `limit=200, offset=0` page (ordered by `as_of`
desc), with no pagination and no server-side account filter — the frontend's account
dropdown only filtered whatever was already in that page. A workspace with enough
order-triggered snapshots (each order writes one) can easily exceed 200 rows, silently
pushing an intentionally old-dated entry (exactly the pre-tracking-backfill use case) past
page 1 with no way to reach it — it existed but was unreachable in the UI, including for
deletion. Added an optional `account_id` query param, threaded through
`CashBalanceService.list_cash_balances` → `CashBalanceRepository.get_all`, so requesting a
specific account returns that account's full history regardless of workspace-wide row
count. The frontend's Cash tab account filter now passes this server-side instead of only
filtering client-side.

## Backend impact (`lifestack-api`)

- `app/investing/service.py`:
  - `InvestingCashBalanceService.create_cash_balance` / `update_cash_balance`: currency-match validation.
  - `InvestingCashBalanceService.list_cash_balances`: optional `account_id` filter.
  - `InvestingOrderService._validate_brokerage_account`: optional `currency` param, validated when provided.
  - `InvestingSummaryService.get_summary`: `cash_total` filtered to brokerage accounts (new `account_repo` dependency).
- `app/investing/repository.py`: `CashBalanceRepository.get_all` gains an optional `account_id` filter.
- `app/investing/router.py`: `GET /investing/cash-balances` gains an optional `account_id` query param.
- `app/core/dependencies.py`: `get_investing_summary_service` wires in `AccountRepository`.
- `app/finance/service.py`:
  - `CapitalTransferService.create_transfer` / `update_transfer`: currency-match validation on both sides.
- `lifestack-web`: `investingService.getCashBalances` takes an optional `accountId`;
  `InvestingPage`'s Cash tab passes the selected account filter through server-side
  (`queryKey`/`queryFn` now include `cashAccountFilter`).
- `app/tests/integration/test_investing.py`: `test_investing_multi_currency_summary`
  restructured to use two single-currency brokerage accounts (one USD, one GBP) instead of
  one account holding both, preserving the test's actual intent (exercising FX-converted
  summary aggregation across a multi-currency *workspace*). Five other tests
  (Indian-stock/mutual-fund price refresh, holding-symbol-rename x3, FIFO cost basis)
  similarly moved their INR orders off the shared USD-default `account_map["brokerage"]`
  onto a dedicated INR brokerage account.

## API / schema impact

`POST/PATCH /investing/cash-balances`, `POST /investing/orders`, and
`POST/PATCH /finance/transfers` now reject (422 `ValidationError`) a currency that doesn't
match the relevant account's `default_currency_code`. `GET /investing/cash-balances` gains
an optional `account_id` query param. No schema/migration change.

## Out of scope

- Making `default_currency_code` immutable via `PATCH /finance/accounts` (out of scope;
  not requested — an account's currency can still be changed after creation, and existing
  rows in the old currency aren't revalidated).
- Retroactively fixing or flagging existing currency-mismatched rows.
- Any change to reconciliation's mechanism itself (cash-balance-as-ground-truth remains
  intact for all account types).
