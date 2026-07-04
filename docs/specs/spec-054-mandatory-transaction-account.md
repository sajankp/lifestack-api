# Spec-054: Mandatory Account on Spending Transactions

**Created:** 2026-07-04
**Status:** Approved (implementation) — pre-approved by owner 2026-07-04 (design discussed and accepted; see AGENT-TASKS.md Task 12)
**Depends on:** spec-050 (account invariants precedent for forward-only enforcement)

---

## Problem

`SpendingTransaction.account_id` is nullable end to end — DB column
(`app/spending/models.py:60`), create schema (`TransactionCreate.account_id: uuid.UUID |
None`, `app/spending/schemas.py:51`), the web transaction form, and the voice-capture tool
(`log_spending_transaction(account_name=None)`).

An account-less transaction is not harmlessly incomplete — it is **inconsistently
counted**:

- It **does** count in budgets, category analytics, and spending summaries (workspace-scoped
  queries).
- It does **not** count in net worth: the spending side is assembled per account
  (the `raw_balances` loop in `GET /finance/net-worth`, `app/finance/router.py` ~413–425,
  iterates account balances and sums them), so a NULL-account spend is invisible there.
- It does **not** count in any account's projected balance, so reconciliation
  (`projected = income − expense + …` per account) never sees it either.

The same rupee is therefore an expense in one report and nonexistent in another — a
structural violation of "every unit of cash is counted exactly once" that the cash model is
built on. The voice agent makes the hole bigger: its tool declaration marks `account_name`
optional and the system prompt only asks for it "whenever the user names an account", so
voice-logged spends default to account-less.

## Solution

Make the account required on **new** spending transactions at every entry point, with a
workspace **default spending account** so the common case stays one utterance / one tap.
Enforcement is forward-only (spec-050 precedent): existing NULL-account rows are neither
backfilled nor blocked.

### Default spending account

New column on `workspace_finance_settings` (already one row per workspace, already the home
of `reporting_currency_code`):

- `default_spending_account_id: int | None` — FK → accounts, composite
  `(default_spending_account_id, workspace_id)` → `accounts(id, workspace_id)` like every
  account reference; nullable (workspaces need no default until they want one).
- Exposed through the existing finance-settings GET/PATCH endpoints and settings UI as an
  account picker (active, non-brokerage accounts). Deactivating an account that is the
  default clears the default (and the UI warns).

### Enforcement (service layer, `TransactionService.create`)

Resolution order for a create without an explicit account:

1. `account_id` provided → validate (exists, active, workspace-scoped) and use it.
2. Not provided → workspace `default_spending_account_id` if set.
3. Neither → `ValidationError` with an actionable detail ("Provide account_id or set a
   default spending account in Finance Settings").

`TransactionCreate.account_id` stays `uuid.UUID | None` in the schema — requiredness lives
in the service so the default-account fallback works for every caller (UI, voice tool,
imports). Updates are unchanged: editing a historical NULL-account row does not force an
account (that would make old rows un-editable), but *setting* one is how users repair
history one row at a time.

### Web UI (`lifestack-web`)

The transaction form's account select becomes required — pre-selected with the workspace
default (or last-used account if no default), submit blocked when empty with an inline
message linking to settings. The quick-entry path inherits the default silently.

Historical NULL-account rows are surfaced through the **existing account filter/sort, not a
special-cased toggle**: "No account" appears as a first-class option in the account filter
(backed by a server-side `account_id=null` on the list endpoint), and sorting/grouping by
account collects NULL rows under a visible "No account" group (sorted last). A count badge
on that option shows how many remain. Opening such a row and assigning an account is the
repair path (the update endpoint already allows setting an account on a historical row);
the group shrinks toward zero as the user repairs history.

**The "No account" option is transitional by design (owner decision, 2026-07-04):** once
the workspace's unassigned count reaches zero it is redundant — creates can no longer
produce NULL rows — and should be removed in a later cleanup pass. That same moment is the
trigger for the deferred DB hardening (see Out of scope: making `account_id` non-nullable);
the two make a natural single future chore.

### Imports

The spending import already maps an optional `account_name` column
(`app/imports/service.py` header aliases). Post-spec, a committed import must not produce
NULL-account rows. Per-row resolution order: row's `account_name` match → import-level
target account (optional picker in the preview step) → workspace default → row-level
**preview error** (blocks commit for that row, consistent with existing preview-error UX).
The import-level target account should **reuse the `ImportBatch.extra_json`
`target_account_id` mechanism introduced by spec-056** for CAMS imports (there it is
required; here it stays optional because per-row `account_name` and the workspace default
also resolve) — no second mechanism. No change to already-imported data.

### Voice capture (`app/capture/`)

This spec only defines the policy the tool inherits: `log_spending_transaction` calls
`TransactionService.create`, so steps 2–3 above apply to it automatically once implemented.
The tool-declaration and prompt changes (making the agent workspace-aware, naming the
account it used) are spec-055's scope.

## Backend impact (`lifestack-api`)

- `app/finance/models.py`: `default_spending_account_id` on `WorkspaceFinanceSettings`
  (+ composite FK); finance-settings schemas/router PATCH support.
- `app/spending/service.py`: create-path resolution + validation above.
- `app/spending/router.py` / `repository.py`: `account_id=null` filter on the list
  endpoint + unassigned count.
- `app/imports/service.py`: per-row account resolution + preview error; preview schema
  gains the optional import-level target account.
- `alembic/versions/`: next free number — one nullable column + composite FK, clean
  downgrade. No data migration.
- Docs: this is spending-ledger policy, not snapshot/order math — no cash-model §6 entry
  required unless implementation touches reconciliation code paths (it should not).

## Out of scope

- **Backfilling existing NULL-account transactions** — forward-only (house rule). The
  unassigned filter is the repair surface; a bulk-assign tool can be a later spec if manual
  repair proves painful.
- **Making `account_id` non-nullable in the DB** — impossible without backfill; revisit
  only if/when the unassigned count reaches zero organically. Bundle it with removing the
  transitional "No account" filter option (same trigger, one future chore).
- **Requiring accounts on transfers** — `capital_transfers` already requires both sides.
- **Agent/prompt changes** — spec-055.

## Golden test scenarios (required before merge)

1. **Resolution order** — create with explicit account → used; without account but with
   workspace default → default assigned; with neither → 422 with the actionable message;
   inactive/foreign-workspace account → rejected.
2. **Default management** — PATCH sets/clears the default; deactivating the default
   account clears it.
3. **Forward-only** — a pre-existing NULL-account row remains readable, editable
   (non-account fields), and repairable (setting an account sticks); the "No account"
   filter value returns exactly the NULL rows, and account sorting groups them last.
4. **Imports** — fixture with per-row account names, missing names + import-level target,
   missing names + workspace default, and missing names + nothing (preview error blocks
   commit).
5. **Web** — form blocks submit without account; pre-selects default; "No account" option
   in the account filter lists NULL rows and assigning an account from such a row removes
   it from the group; vitest for all three.
