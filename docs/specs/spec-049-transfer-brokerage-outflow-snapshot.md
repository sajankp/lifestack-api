# Spec-049: Transfer Brokerage-Outflow Cash Snapshot

**Created:** 2026-07-01
**Status:** Implemented (api#97, merged 2026-07-05)
**Depends on:** spec-011 (transfers/FX), the cash model documented in
`docs/domain/cash-model-ledger-snapshots-reconciliation.md`

---

## Problem

`CapitalTransferService.create_transfer` only auto-writes a new
`investing_cash_balances` snapshot for the **to**-side of a transfer, and only
when `to_module == "investing"` (`app/finance/service.py`). There is no
matching branch for `from_module == "investing"`.

Symptom: transferring cash **out of** a brokerage account (e.g. Groww →
ICICI) correctly creates the ledger `CapitalTransfer` row, but the
brokerage's cash snapshot is never decremented — the brokerage's cash
balance (shown on the Investing Cash tab and Net Worth page) doesn't move,
even though real money left the account.

## Solution

Add a symmetric from-side branch to `create_transfer`: when
`from_module == "investing"`, write a new cash-balance snapshot for
`from_account_id`/`from_currency_code`, decrementing by `gross_amount` (the
amount that actually left the source account — already the basis used
elsewhere, e.g. the reconciliation `transfer_out` sum).

### The complication: one transfer can now produce two snapshots

An **investing → investing** transfer (e.g. moving USD cash between two
brokerage accounts) triggers both the existing to-side branch and the new
from-side branch, producing two `CashBalance` rows that would share the same
`trigger_ref = transfer.public_id`. `delete_transfer` and `update_transfer`
look up the linked snapshot via `get_by_trigger_ref` (`scalar_one_or_none`),
which breaks (`MultipleResultsFound`) once two rows can share a `trigger_ref`.

Fix: add `CashBalanceRepository.get_by_trigger_ref_and_account(workspace_id,
trigger_ref, account_id)`, scoped by account, and use it everywhere a linked
snapshot needs to be resolved. `create_transfer`, `delete_transfer`, and
`update_transfer` each independently guard/rebuild the **to**-side and
**from**-side snapshot; whichever side has no linked snapshot (because that
side's module wasn't `"investing"` at creation time, or the transfer predates
this fix) is a no-op, matching the existing to-side-only pattern's behavior
exactly. `CapitalTransferUpdate` doesn't allow changing `from_module`/
`to_module`, so a side's managed/unmanaged status can't change after creation
— only account/currency/amount edits within a side.

The "newer snapshot exists" conflict guard (`ConflictError`, "delete those
order imports first") applies independently per side in `delete_transfer` and
`update_transfer` — both sides are checked before either is deleted/rebuilt,
so a conflict on one side doesn't leave the other side already mutated.

## Backend impact (`lifestack-api`)

- `app/investing/repository.py`: new `CashBalanceRepository.get_by_trigger_ref_and_account`.
- `app/finance/service.py` (`CapitalTransferService`):
  - `create_transfer`: new from-side snapshot branch.
  - `delete_transfer`: resolves + guards + deletes both sides' linked snapshots.
  - `update_transfer`: computes `from_balance_affecting` alongside the existing
    `to_balance_affecting`; guards and rebuilds both sides independently.
  - New private helper `_check_no_newer_snapshot(workspace_id, linked)` shared
    by both sides in both `delete_transfer` and `update_transfer` (previously
    duplicated inline per-call-site).

## API / schema impact

None. No new endpoints, no schema/migration change — `CapitalTransferResponse`
is unchanged. Existing (pre-fix) transfers with `from_module == "investing"`
are simply left as-is (no linked from-side snapshot exists for them, so
delete/update treat that side as unmanaged, same as any other unmanaged side)
— this fix is not retroactive. Backfilling historical outflow snapshots for
existing transfers is out of scope; a one-time reconciliation for the current
data was handled separately (`seed_data/scripts/fix_transfer_currency_mislabels.py`
and manual entry, not through this code change).

## Out of scope

- Retroactively creating from-side snapshots for transfers created before
  this fix.
- Validating sufficient funds before allowing a brokerage outflow transfer
  (transfers have never validated sufficient balance, unlike buy orders;
  not changed here).
- Reconciliation formula changes (already covered by spec-048).
