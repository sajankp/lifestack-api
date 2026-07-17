# Spec-084: Account Resolution on Recurring Transactions

**Created:** 2026-07-17
**Status:** Approved — account required at creation (explicit or workspace default, 422
otherwise, matching spec-054); generation-time fallback for a deactivated linked account
goes to the workspace default spending account.
**Depends on:** spec-054 (mandatory account on spending transactions — this spec extends the
same invariant and reuses its resolver), spec-053 (calendar recurrence modes, unrelated but
same table)

---

## Problem

`RecurringTransaction` (`app/spending/models.py:209-267`) has no `account_id` column at all —
unlike `SpendingTransaction`, which has had one since spec-054. `RecurringTransactionCreate`
(`app/spending/schemas.py:289-307`) has no account input either, so there is nowhere for a
user to say which account a recurring rule should post against.

The generation job (`process_workspace_recurring_transactions`,
`app/application/workflows.py:935-1084`) builds each occurrence's `SpendingTransaction`
directly via SQLAlchemy (lines 1037-1046) without ever setting `account_id` — it bypasses
`TransactionService._resolve_create_account_id` (`app/spending/service.py:847-879`)
entirely. Every transaction spec-054 requires to resolve to an account (explicit, or
workspace default, or a 422) silently gets a `NULL` account when it comes from a recurring
rule instead of the manual-create or import paths. This is the same rupee-miscount problem
spec-054 fixed for manual creates, reopened for the recurring path: a NULL-account spend
counts in budgets/category analytics but is invisible in net worth and per-account
reconciliation.

`RecurringTransactionService` (`app/spending/service.py:2067-2076`) is a separate, smaller
class from `TransactionService` (`app/spending/service.py:674`) — it's built from
`recurring_repo` + `tx_repo` + `category_repo` only, with no `account_repo`/`setting_repo`,
via `get_spending_recurring_service` (`app/core/dependencies.py:289-294`). It has no path to
the resolver today.

## Solution

Give `RecurringTransaction` its own `account_id`, resolved through the **same resolver**
spec-054 introduced for manual transactions, at both creation time and generation time.

### Data model

Add to `RecurringTransaction` (`app/spending/models.py`):
```python
account_id: int | None = Field(default=None, index=True)
```
plus a composite FK constraint matching `SpendingTransaction`'s pattern (`__table_args__`):
```python
sa.ForeignKeyConstraint(
    ["account_id", "workspace_id"],
    ["accounts.id", "accounts.workspace_id"],
    name="fk_recurring_transactions_account_workspace",
),
```
Column is nullable at the DB level — existing rows get `NULL` (not backfilled; see
Retroactivity below). Alembic migration: `add_column` + FK, with a working `downgrade()`
that drops the FK then the column.

### API

- `RecurringTransactionCreate` (`schemas.py:289`): add `account_id: uuid.UUID | None = None`
  (public_id, same shape as `TransactionCreate.account_id`).
- `RecurringTransactionUpdate` (`schemas.py:310`): add `account_id: uuid.UUID | None = None`
  — if the field is present in the payload (including explicit `null`), re-resolve; if
  absent, leave the stored account unchanged. (Same "explicit vs. absent" semantics already
  used for the other optional update fields.)
- `RecurringTransactionResponse` (`schemas.py:322`) and `UpcomingTransactionItem`
  (`schemas.py:344`): add `account_id: uuid.UUID | None` (public_id) so the web UI can show
  and edit which account a rule/preview is tied to.

### Service — create/update (`app/spending/service.py`)

`RecurringTransactionService.create_recurring` (`service.py:2142-2168`) gets the same
treatment `TransactionService.create_transaction` already has (`service.py:1098-1099`):
```python
account_id = await self._resolve_create_account_id(workspace_id, payload.account_id)
```
i.e. explicit `account_id` → validated; omitted → workspace default spending account;
neither → `ValidationError` (422) telling the caller to provide one or set a default.

Concretely:
- Move `_resolve_create_account_id` and `_resolve_account_id` to a shared location both
  services can call (e.g. a module-level function or a small mixin in `service.py`) rather
  than duplicating the resolution logic on `RecurringTransactionService` — same behavior,
  one implementation.
- `RecurringTransactionService.__init__` (`service.py:2068-2076`) gains `account_repo:
  AccountRepository` and `setting_repo: FinanceSettingRepository` params.
- `get_spending_recurring_service` (`app/core/dependencies.py:289-294`) gains an
  `AccountRepository(session)` and `FinanceSettingRepository(session)` the same way
  `get_spending_transaction_service` already builds them (`app/core/dependencies.py:265-266`).

`update_recurring`: if `payload.account_id` is set in the request, resolve with the
non-creating `_resolve_account_id` (validate + must exist, no default fallback) and update
the stored value.

### Generation workflow (`app/application/workflows.py`)

Replace the direct `account_id`-less construction (lines 1037-1046) with:
1. Copy `recurrence.account_id` onto the generated `SpendingTransaction`.
2. Defense-in-depth re-check (accounts can be deactivated after being linked to a rule,
   same concern `_resolve_create_account_id` already handles for the default-account case):
   if `recurrence.account_id` is set, verify via `AccountRepository` (already imported in
   `workflows.py`) that the account still exists and `is_active`; if not, fall back to the
   workspace's current default spending account (via `FinanceSettingRepository`, already
   used elsewhere in this file — see `finance_setting_repo` at `workflows.py:314-381` for
   the existing pattern) using the same active-check.
3. If the linked account is gone/inactive and the workspace also has no active default
   (edge case — the workspace default itself was deactivated after being set), generate the
   transaction with `account_id = None` and log a warning
   (`recurring_generation_account_unresolved`, with `workspace_id`/`recurrence_id`) — **do
   not raise**, since this runs unattended in a batch job across all of a workspace's due
   rules and one bad recurrence must not abort the others. This mirrors the existing pattern
   in this function where advance-failures log and `break` rather than raise.

This is intentionally *not* a call into `SpendingTransactionService` — the workflow module
already reaches into repositories directly (see `finance_setting_repo` usage) rather than
instantiating the full request-scoped service, so the re-validation step reuses that same
direct-repository style rather than adding a service dependency to a background job.

### Web (`lifestack-web`) — mirror the manual-transaction form's spec-054 pattern

The manual transaction form already lives in `SpendingPage.tsx` and already enforces
"account required on create" client-side; the recurring form in the same file
(`recurringFormSchema` / `RecurringFormValues`, `SpendingPage.tsx:151`,
`handleSaveRecurring` at `SpendingPage.tsx:1200-1233`) needs the identical treatment:

- `RecurringTransaction`/`RecurringTransactionCreate`/`RecurringTransactionUpdate` types
  (`src/types/spending.ts:368-410`): add `account_id`.
- `recurringFormSchema`: add `accountId` field.
- Pre-fill on opening the create modal: replicate the `defaultSpendingAccountId` effect
  used for the manual form (`SpendingPage.tsx:739`, `761-766`) so a new recurring rule
  starts with the workspace default selected, not blank.
- Require it on create: replicate the manual form's submit guard
  (`!editingTransaction && !accountId` at `SpendingPage.tsx:1056`) as
  `!editingRecurring && !accountId` before calling `createRecurringMutation`
  (`handleSaveRecurring`, `SpendingPage.tsx:1218-1231` — add `account_id: values.accountId`
  to the `RecurringTransactionCreate` payload built there).
- Render the same account `<select>` used for manual transactions
  (`SpendingPage.tsx:2194`) and the same missing-account hint
  (`SpendingPage.tsx:2202`) inside the recurring modal.
- `update_recurring`: allow changing the account on an existing rule (optional field, no
  create-time 422 — a rule that already has an account can only be re-pointed, not cleared,
  consistent with "every recurring rule has a resolved account" once created).
- Upcoming/preview list (`UpcomingTransactionItem`): show the resolved account per item.

## Out of scope

- No change to how `account_id` behaves on ad-hoc transactions (spec-054 already covers
  that).
- No UI/API for bulk-editing account across many existing recurring rules.
- No change to import-sourced or voice-logged transactions.

## Retroactivity

Not retroactive, consistent with spec-054/spec-049 precedent:
- Existing `recurring_transactions` rows get `account_id = NULL` from the migration and keep
  generating `NULL`-account transactions (via the fallback-to-default-then-null path above)
  until the owner edits the rule to set an account.
- Historical `spending_transactions` rows already generated with `account_id = NULL` are
  untouched — no backfill.

## Testing plan

- Red: a test asserting a newly created recurring transaction with no `account_id` and no
  workspace default raises 422 (mirrors spec-054's transaction-create test); a test asserting
  the generation job sets `account_id` on the generated transaction from the recurring rule.
- Green: implement per above.
- Regression: existing recurring-generation tests must keep passing with `account_id`
  unset → `NULL` for pre-existing rows (no default configured in that test's fixture).
- Web: a test on the recurring form asserting create is blocked without an account selected
  (unless a default is pre-filled), mirroring the existing manual-transaction-form test for
  the same guard.
- Full backend suite + coverage gate (80%); web suite + coverage gate (70%) for the form/type
  changes.
