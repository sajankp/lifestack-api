# Spec-062: Deletable System Categories & Category Merge

**Created:** 2026-07-07
**Status:** Implemented (backend) — web (MasterConfigPage delete/merge UI) pending
**Depends on:** none (spending-ledger policy only; no snapshot/order math)
**Sequencing (owner plan, 2026-07-07):** lands **before** spec-064 (recurring budgets & category
groups), so this spec's merge/delete logic is written once, without `category_group_id`
awareness; after spec-064, the only interaction is its own group-delete guard.

---

## Problem

Spending categories are **per-workspace rows**, not a shared global set: every workspace
gets its own copy of the eight defaults, seeded at registration by
`CategoryService.provision_default_categories` with `is_system=True`
(`app/spending/service.py:283`). Despite the name, `is_system` is not an
immutable-global marker — editing a system category (name, color, icon) is **already
allowed** end to end (`update_category` has no `is_system` guard,
`app/spending/service.py:199`; the web edit button is enabled for all rows,
`MasterConfigPage.tsx:703`).

The **only** behavior `is_system` gates is deletion:

- `delete_category` raises `ForbiddenError("System categories cannot be deleted")`
  (`app/spending/service.py:258`), **above** the real guard — `has_usage()`, which already
  refuses to delete any category referenced by transactions, budgets, or recurring rules
  (`app/spending/service.py:260`).
- The web delete button is `disabled={category.is_system}` (`MasterConfigPage.tsx:722`).

Two gaps follow:

1. **The system-delete block is redundant paternalism.** No runtime path depends on a
   specific default category *existing* — transactions carry a NOT-NULL `category_id` FK
   (so there is no "uncategorized fallback" that needs "Other" to survive), and the only
   name references are a display-only "Uncategorized" label in summaries
   (`app/summaries/service.py:321`) and demo-reset seed data. A workspace that never uses
   "Entertainment" cannot remove it, for no data-integrity reason.

2. **There is no way to consolidate categories.** Because delete correctly refuses any
   *in-use* category, a workspace that accumulated near-duplicates ("Food", "Food &
   Dining", "Dining") is stuck — every one has transactions, so none can be deleted. The
   missing primitive is **merge**: re-point everything from one or more source categories
   onto a target, then remove the sources.

Delete and merge are two halves of the same capability — delete for empty categories,
merge for in-use ones — so they ship together.

## Solution

### 1. Allow deleting any unused category (drop the system special-case)

Remove the `if category.is_system: raise ForbiddenError` block in `delete_category`. The
`has_usage()` guard immediately below already enforces the only rule that matters: a
category in use by transactions, budgets, or recurring rules cannot be deleted (system or
custom). `is_system` survives purely as a **display badge** ("System"/"Custom") and as the
anchor for a possible future "restore defaults" action.

Web: `MasterConfigPage.tsx` delete button becomes `disabled={deleteCategoryMutation.isPending}`;
drop the "System categories cannot be deleted" title text.

### 2. Merge N source categories into one target

A single, transactional bulk re-point across the three referencing tables, then delete the
sources.

**API:** `POST /spending/categories/{target_public_id}/merge`
with body `{ "source_public_ids": [uuid, ...] }`.

**Validation (all before any write):**
- Target and every source exist and are workspace-scoped.
- `target_public_id` is **not** in `source_public_ids`.
- `source_public_ids` is non-empty and de-duplicated.

**Re-point, in one DB transaction (all-or-nothing):**
- `SpendingTransaction.category_id`: source → target (bulk `UPDATE`).
- `RecurringTransaction.category_id`: source → target (bulk `UPDATE`).
- `SpendingBudget.category_id`: source → target — **with conflict handling** (below).
- Delete the source `SpendingCategory` rows.

**Budget conflict — sum (owner decision, 2026-07-07):** budgets carry a unique constraint
`uq_budget_workspace_category_month` on `(workspace_id, category_id, month_start)`
(`app/spending/models.py:140`). When a source budget and the target budget cover the same
`month_start`, a naive re-point violates it. Resolution: **sum the amounts into the target's
budget for that month** and delete the source budget. Source-budget months with no target
counterpart re-point normally. Summing matches merge semantics ("these were always one
category, so combine their budgets"). Transactions and recurring rules have no analogous
constraint — they re-point unconditionally.

**Audit:** one merge event per merge (module `spending`, a `merge` action on the target
category) recording source public_ids, target public_id, and counts moved
(transactions / recurring / budgets, budgets-summed vs budgets-repointed). **Not** one
audit row per moved transaction.

**Concurrency / correctness:** the whole merge runs in a single transaction so a partial
merge cannot strand rows or leave a half-deleted source. Bulk `UPDATE ... WHERE
category_id IN (sources)` rather than per-row loads.

### Capture auto-categorizer — soft dependency on "Other" (accepted degradation)

The voice/AI capture tool resolves categories **by name at runtime**: it lists the
workspace's current categories, matches the model's `category_name` (normalized), and falls
back to a category literally named "Other" (`app/capture/tools.py:529-536`). If neither
matches, it returns `category_matched: false` with "No suitable spending category found" —
a graceful error, not a crash.

Deleting or merging away the "Other" category therefore removes capture's fallback. **This
spec accepts that as graceful degradation and does not special-case "Other":** the failure
mode is an actionable message the agent already surfaces (spec-055 prompt tells it to say so
and offer a real category), and special-casing one English string would reintroduce the
paternalism this spec removes. Documented here so it is a known, chosen tradeoff.
*(Override point: if we'd rather protect the fallback, the minimal alternative is to block
delete/merge of the workspace's current "Other" row — but that is explicitly not the
recommendation.)*

## Backend impact (`lifestack-api`)

- `app/spending/service.py`: remove the `is_system` block in `delete_category`; add
  `merge_categories(workspace_id, target_public_id, source_public_ids, actor_id,
  audit_logger)` with the validation, transactional re-point, budget-sum, and audit above.
- `app/spending/repository.py`: bulk re-point helpers (`reassign_category` per table) and a
  same-month budget lookup for the sum path.
- `app/spending/router.py` + `schemas.py`: the merge endpoint and its request schema.
- No model/migration change — no new columns; merge is pure data movement over existing
  tables. (`is_system` column stays.)
- Docs: spending-ledger policy only — no cash-model §6 entry (no snapshot/order/reconciliation
  path touched).

## Web impact (`lifestack-web`)

- `MasterConfigPage.tsx`: enable delete for system categories (guard on mutation-pending
  only). Add a **Merge** action — pick one or more source categories and a target, confirm
  dialog stating "N categories and their transactions/budgets will be merged into <target>;
  overlapping budgets are summed; this cannot be undone", then call the merge endpoint.
- `services/spending.ts` + `types/spending.ts`: merge call + types; invalidate category,
  transaction, budget, and analytics query keys on success.

## Out of scope

- **Undo / merge history restore** — merge is destructive by design; the confirm dialog is
  the guard. Audit records what moved.
- **Auto-suggesting duplicate categories to merge** — a later nicety; this spec ships the
  manual primitive.
- **Protecting the "Other" capture fallback** — see the degradation note above; not done.
- **Renaming `is_system`** — it stays as a provenance/display flag and future
  "restore defaults" anchor.

## Golden test scenarios (required before merge)

1. **Delete parity** — an unused system category deletes successfully; an in-use system
   category is refused with the existing `CategoryInUseError` (not the old system-forbidden
   error); an in-use custom category is still refused; an unused custom category still
   deletes.
2. **Merge re-point** — transactions, recurring rules, and budgets on N sources all end up
   on the target; sources are gone; N→1 (three sources, one target) works in one call.
3. **Budget sum** — source and target both have a budget for the same `month_start` →
   target budget amount becomes the sum, source budget deleted, unique constraint intact; a
   source-only month re-points cleanly; a target-only month is untouched.
4. **Validation** — target in the source set → 422; unknown/foreign-workspace source or
   target → 422/404; empty source list → 422; no partial write on any rejection.
5. **Atomicity** — a forced failure mid-merge leaves *all* source rows and their references
   unchanged (transaction rolls back).
6. **Audit** — exactly one merge audit event with correct source/target ids and moved
   counts (including budgets-summed vs budgets-repointed); no per-transaction audit spam.
7. **Capture degradation** — with "Other" deleted/merged away, `log_spending_transaction`
   for an unmatched name returns `category_matched: false` and does not error the request.
8. **Web** — delete enabled for system rows; merge dialog reassigns and the source
   disappears from the list; query invalidation refreshes transactions/budgets/analytics;
   vitest coverage for delete-enable and the merge flow.
