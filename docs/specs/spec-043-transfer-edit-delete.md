# Spec 043 — Transfer Edit & Delete

**Status:** Implemented (api `47cb123`, web `48aa4ca`, merged 2026-06-29)

## Problem

Capital transfers imported via CSV cannot currently be edited or deleted. When a transfer is
committed to the wrong account (e.g., a USD brokerage transfer routed to an INR brokerage), the
only remediation path is a direct database operation — there is no API endpoint or UI for
correction.

Additionally, when a transfer targets an investing account, `create_transfer` writes an
`investing_cash_balances` snapshot with `trigger_ref = transfer.public_id`. Any edit or delete
must keep that snapshot consistent.

## Goals

1. `DELETE /v1/finance/transfers/{id}` — remove a transfer and its linked cash balance snapshot.
2. `PATCH /v1/finance/transfers/{id}` — update editable fields; rebuild the linked cash balance
   snapshot when amount, account, or currency changes.
3. Edit & delete actions in the Transfers UI (list view and/or detail drawer).

## Non-Goals

- Automatic cascade-recalculation of **subsequent** order-derived cash balance snapshots. The
  user is responsible for deleting the most-recently-created snapshot first (see Constraints).
- Editing the `from_module` / `to_module` direction (spending→investing vs spending→spending).
  Changing direction would require a full delete + recreate; the UI can guide the user to do that.

## Constraints

### Cash balance cascade

`investing_cash_balances` is append-only cumulative. Order commits create snapshots that
include the running balance at that moment. If a transfer snapshot is deleted/edited and later
order snapshots exist for the same `(account_id, currency)`, those order snapshots are now stale.

**Safe-delete rule (enforced by the API):** Before deleting (or editing the to_account / to_currency
/ net_amount_received of) a transfer, the backend checks whether any `CashBalance` row for the
same `(account_id, currency)` was `created_at` **after** the transfer's linked snapshot. If any
exist, the API returns `409 Conflict` with a message explaining that newer balance snapshots must
be removed first (i.e., the user must delete the most recent orders' import before deleting the
transfer).

This is an intentional conservative design — it keeps the balance chain valid without requiring
a full recompute pass.

## API Changes

### `DELETE /v1/finance/transfers/{transfer_id}`

**Happy path:**
1. Load transfer by `public_id` for workspace.
2. Look up the linked `CashBalance` where `trigger_ref = transfer.public_id`.
3. If a linked `CashBalance` exists, check for newer snapshots:
   - Query `CashBalance` where `account_id = transfer.to_account_id`,
     `currency = transfer.to_currency_code`, `created_at > linked_snapshot.created_at`.
   - If any exist → return `409 Conflict`.
4. Delete the linked `CashBalance` (if any).
5. Delete the `CapitalTransfer`.
6. Return `204 No Content`.

**Error cases:**
- `404` — transfer not found in workspace.
- `409` — newer cash balance snapshots exist (message tells user what to clear first).

### `PATCH /v1/finance/transfers/{transfer_id}`

Editable fields (all optional in request body):
```
from_account_id   (UUID public_id)
to_account_id     (UUID public_id)
from_currency_code
to_currency_code
gross_amount
fx_rate_used
fx_fee_amount
platform_fee_amount
tax_amount
net_amount_received
occurred_at
notes
```

**Happy path:**
1. Load transfer.
2. Validate updated field values (same arithmetic consistency check as create).
3. Determine if balance-affecting fields changed:
   `to_account_id | to_currency_code | net_amount_received` — call this "balance-affecting change".
4. If balance-affecting change AND a linked `CashBalance` exists, run the same newer-snapshot
   check as DELETE → `409 Conflict` if blocked.
5. Apply field updates to `CapitalTransfer`.
6. Rebuild the linked `CashBalance`:
   - Find the snapshot with `trigger_ref = transfer.public_id`.
   - If to_account or to_currency changed: delete the old snapshot, create a new one for the new
     account/currency (re-reading prev balance for the new account/currency first).
   - If only `net_amount_received` changed: update the existing snapshot's balance in-place
     (prev_balance + new_net_amount — old_net_amount).
   - If no balance-affecting change: leave cash balance snapshot untouched.
7. Return updated `CapitalTransferResponse`.

**New schema — `CapitalTransferUpdate`:**
```python
class CapitalTransferUpdate(BaseModel):
    from_account_id: uuid.UUID | None = None
    to_account_id: uuid.UUID | None = None
    from_currency_code: str | None = None
    to_currency_code: str | None = None
    gross_amount: Decimal | None = None
    fx_rate_used: Decimal | None = None
    fx_fee_amount: Decimal | None = None
    platform_fee_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    net_amount_received: Decimal | None = None
    occurred_at: datetime | None = None
    notes: str | None = None
```

## Repository Changes

**`CapitalTransferRepository`:**
- `update(transfer)` — session.flush on a mutated ORM object (already works via SQLAlchemy).
- `delete(transfer)` — `await session.delete(transfer)`.

**`CashBalanceRepository` (investing/repository.py):**
- `get_by_trigger_ref(workspace_id, trigger_ref: UUID) → CashBalance | None`
- `get_newer_than(workspace_id, account_id, currency, created_after: datetime) → list[CashBalance]`
  (used for the 409 safety check).

## Frontend Changes

**Transfers list page** (`TransfersPage.tsx` or equivalent):
- Each row gets an action menu (three-dot or hover) with **Edit** and **Delete**.

**Edit dialog:**
- Pre-fills all editable fields.
- Re-uses the existing transfer form component (or extracts a shared `TransferFormFields`
  component from the create form).
- On submit: `PATCH /v1/finance/transfers/{id}`.
- Shows `409` errors as a contextual banner with the full API message so the user knows exactly
  which account/currency chain they need to clear first before retrying.

**Delete confirmation dialog:**
- Shows transfer summary (date, from→to, amount).
- On confirm: `DELETE /v1/finance/transfers/{id}`.
- On `409`: replace the confirmation with an error state showing the API message verbatim, e.g.
  "IND Money Shahma (USD) has 2 newer balance snapshots. Delete those order imports first, then
  retry." — never leave the user with a silent failure or a generic "something went wrong".

**TanStack Query:**
- Invalidate `['transfers']` and `['cashBalances']` after both operations.

## Testing

**Backend (integration):**
1. Delete happy path — transfer + cash balance removed.
2. Delete blocked by newer snapshot → 409.
3. Delete transfer with no cash balance (spending→spending) — succeeds.
4. Patch non-balance fields — cash balance unchanged.
5. Patch `net_amount_received` — cash balance updated in-place.
6. Patch `to_account_id` — old snapshot removed, new one created for new account.
7. Patch blocked by newer snapshot → 409.

**Frontend (Playwright / mock):**
1. Edit dialog pre-fills correctly.
2. Delete shows confirmation and removes row from list.
3. 409 error banner appears with correct message.

## Migration

No schema changes required. `trigger_ref` and `trigger_type` already exist on
`investing_cash_balances`.

## Rollout

Single PR per repo: `feat/transfer-edit-delete`.
Order: backend first (so frontend can integrate against real endpoints), then frontend.
