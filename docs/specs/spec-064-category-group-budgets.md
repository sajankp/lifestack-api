# Spec-064: Recurring Date-Ranged Budgets & Category Groups

**Created:** 2026-07-07
**Status:** Draft (design) — pending owner approval
**Depends on:** none (spending-ledger policy only; no snapshot/order math)

---

## Problem

Two limitations, one spec, because they touch the same table and UI:

**1. Budgets are one row per month.** `SpendingBudget` is keyed by `(workspace_id,
category_id, month_start)` with unique constraint `uq_budget_workspace_category_month`
(`app/spending/models.py:112-143`) — a budget is scoped to exactly one calendar month. There is
no recurrence: nothing carries a budget forward, so tracking Groceries every month means hand-
creating a fresh row each month (the scheduler has no rollover job — `app/application/jobs.py`
only *evaluates* existing budgets). It is also hostile to **historical pattern analysis**: to
see how the last 12 months tracked against a $500 target you must back-fill 12 rows. This is
not how mainstream budgeting apps model it — they treat a category budget as a *recurring
monthly target* you compare actuals against month over month, which is exactly what is missing
here.

**2. Budgets are single-category only.** `SpendingCategory` is flat with no parent/group field
(`app/spending/models.py:39-63`), and `SpendingBudget.category_id` is NOT NULL, so there is no
way to budget a *group* of related categories together (e.g. "Household" = Groceries +
Utilities + Rent).

The dashboard currently folds every category budget into one number —
`month_budget = sum(budget.amount for budget in budgets)` (`app/application/workflows.py:148`),
one "Budget remaining" tile (`DashboardPage.tsx:112-114`). Once category budgets and group
budgets coexist, a single combined total has no honest meaning; per owner direction
(2026-07-07) this spec does **not** reconcile them into one number — they are independent
parallel primitives, and the dashboard stops showing a combined total.

**Existing budget data is disposable.** The owner has never used budgets in earnest; every
`spending_budgets` row that exists is test data (owner decision, 2026-07-07). This spec
therefore rebuilds the table **destructively** — no data migration, no backfill, no
behavior-preservation guarantee for existing rows. That removes what would otherwise be the
riskiest part of the change (a rename-with-backfill migration with a lossy downgrade).

## Solution

### 1. Budgets become recurring, date-ranged, monthly-amount rows

A `SpendingBudget` row now carries:
- `start_month` (date, first-of-month, required).
- `end_month` (date, first-of-month, **nullable**) — inclusive; `NULL` means "ongoing / till
  date".
- `amount` — the **monthly** target, applied to *every* month in `[start_month, end_month]`.

One row therefore covers a span of months. "Groceries $500/month from Jan 2025 onward" is a
single row that evaluates against Jan, Feb, Mar … automatically, and — because the span can
start in the past — every past month in range lights up in analytics with no back-filling. A
single-month budget is just the degenerate case `start_month == end_month`; CSV import keeps
producing exactly such rows (see Editability), so the shape stays first-class.

**No overlapping budgets for the same scope.** Two budgets for the same category (or same
group) whose `[start_month, end_month]` windows overlap would make "which monthly target
applies to month M" ambiguous, so it is forbidden. This is the range-aware successor to today's
per-month unique constraint. **Enforcement is at application write-time** (an overlap query
before insert/update, in the style of the existing `get_by_category_and_month` check
`service.py:1235`), *not* a DB constraint — a true non-overlap guarantee needs a Postgres
`EXCLUDE USING gist` exclusion constraint, which requires enabling the `btree_gist` extension
(a new infra dependency this spec deliberately avoids). The rebuilt table has no per-month
unique constraint; the application overlap check subsumes it. *(Accepted limitation:
application-level enforcement has a theoretical write-write race; acceptable for single-
maintainer workspaces and consistent with the existing budget-creation check, which is already
app-level.)*

**Changing the target over time = segment, don't mutate.** Editing a budget's `amount` changes
it for the *whole* range, so it is for corrections only. To change the monthly target going
forward (e.g. $500 → $600 starting July), you **end the current budget** (`end_month = June`)
and **create a new one** (`start_month = July`, ongoing) — two non-overlapping segments, so the
$500 history for Jan–June is preserved. This is exposed as a dedicated endpoint,
`POST /spending/budgets/{public_id}/change-amount` with body `{amount, from_month}`, which
performs both writes **in one DB transaction** (sets the old row's
`end_month = from_month - 1 month`, creates the successor row with the old row's scope,
`start_month = from_month`, `end_month = NULL`; rejects `from_month <= start_month` with 422).
The web budget form's one-click "change amount from this month" action calls this endpoint — a
client-side end-then-create pair would not be atomic (a failure between the two calls would
leave the old budget ended with no successor), so the transaction lives in the backend. The
stored primitive is still just two ordinary rows.

**Editability:** `amount` and `end_month` are editable (end_month to stop or extend an ongoing
budget); `start_month` and scope are immutable after creation (delete + recreate to change
them). CSV-imported budgets stay single-month by default (`start_month = end_month = month_start`
from the file, `app/imports/spending_import.py:224-259`) — the import contract is unchanged; an
`end_month` import column is a possible future add, out of scope here.

### 2. Category groups — a flat, single-parent field; sharing is structurally impossible

New table `category_groups` (workspace-scoped, same shape as `spending_categories`): `id`,
`public_id`, `workspace_id`, `name`, `normalized_name`, `color`, `icon`, timestamps; unique
`(workspace_id, normalized_name)`.

`SpendingCategory` gets one new nullable column, `category_group_id` (composite FK to
`(category_groups.id, category_groups.workspace_id)`, same tenant-isolation pattern as
`fk_spending_transactions_category_workspace`). Because this is a single FK column on the
category — not a join table — **a category cannot belong to two groups**; "groups share a
category" is not expressible in the schema. Grouping is purely organizational and carries no
budget implication by itself. Unlike budgets, `spending_categories` holds real data (every
transaction references a category), so this change is strictly additive — no destructive
license extends to it.

### 3. Group budgets are a second, independent budget scope

`SpendingBudget.category_id` is nullable in the rebuilt table; a nullable `category_group_id`
column (same composite-FK pattern) sits beside it. A `CHECK` enforces exactly one of the two is
set on a row — a budget is category-scoped **or** group-scoped, never both — the only
cross-column rule. The recurring/date-range mechanics of section 1 apply identically to both
scopes (a group budget is "$X/month for the whole group, from start to end"), and the
non-overlap rule applies per scope (no two overlapping budgets for the same group).

**Explicitly not enforced (owner decision, 2026-07-07):** a category may have its own budget
*and* belong to a group that also has a budget for the same month. Both are tracked and shown
independently; neither blocks the other; group assignment never checks for budgets. The only
guard on groups is referential: **deleting a group is refused (409) while it has a budget
covering the current or a future month** (i.e. `end_month IS NULL OR end_month >= current
month`), the shape of category deletion's `has_usage()` check (`service.py:260`) — just to avoid
orphaning a group-scoped row. Deleting an otherwise-unbudgeted group un-groups its member
categories (`category_group_id → NULL`); categories and their transactions are never touched.

### 4. Dashboard: no combined total, a small "budget spotlight" instead

`SpendingSummary.month_budget` (`app/dashboard/schemas.py:18`) and the `budgetRemaining` tile it
feeds (`DashboardPage.tsx:48-50`, `:107-114`) are **removed** — there is no longer one honest
"total budgeted" number. `month_spent` (total current-month expense, unrelated to any budget)
and `top_overspent_categories` (`workflows.py:150-166` — always a *list* of individually-
overspent categories, never a sum) both stay unchanged; the current-month category budget for
that list is now "the category budget whose range contains this month" (unique by non-overlap)
instead of a direct month lookup.

New: `SpendingSummary.budget_spotlight: list[BudgetSpotlightItem]` — **up to 2** group budgets
whose range covers the current month, sorted by `utilization_pct` descending (closest-to/over-
budget first; groups with no covering budget never appear; 0–2 items). `BudgetSpotlightItem` is
a typed Pydantic schema — the same fields as a group's `BudgetPerformanceItem` (section 5) plus
`daily_amount_left = remaining / days_remaining_in_month` (today counted as remaining; clamped
to `0` when `remaining <= 0`) — so the web service layer gets a concrete shape to validate with
Zod (per the response-validation contract, web#80); an untyped dict list is not acceptable
here. The daily-pace figure is **group-only** per owner direction; single-category displays are
untouched.

### 5. Budget-performance analytics: parallel, uncombined category and group lists

`BudgetService.get_budget_performance` (`app/spending/service.py:1308-1432`) is reworked for the
recurring model and gains a group list:

- **Per-month expansion.** Over a query window `[from_month, to_month]`, a scope's
  `budget_amount` is now `monthly amount × (count of months in the window its range covers)`,
  summed across its non-overlapping segments — not a raw row sum. `actual_amount` is the
  existing expense-transaction sum over the window.
- `categories`: same item shape, computed for **every** category regardless of group membership
  (no exclusion — nothing to double-count against, since totals are never combined).
- `groups` (new `list[BudgetPerformanceItem]`, group_id/group_name in place of the category
  fields; `actual_amount` = summed expense across all member categories): computed
  independently.
- `totals` (categories) is unchanged in meaning; new `group_totals: BudgetPerformanceTotals` is
  computed **only** over `groups`. The two lists and two totals are never merged — the "separate
  budgets, not one reconciled number" decision, applied to the detailed view too.

This itemized, no-combined-total view lives in the existing Analytics tab
(`AnalyticsTab.tsx:74-76`, already calling `getBudgetPerformance`): it renders the `categories`
list as today plus a new `groups` section using the same progress-bar/status presentation. A
dedicated stand-alone budgets analytics page is a reasonable future follow-up, not needed now.

## Migration (destructive rebuild of `spending_budgets`)

Because all existing budget rows are disposable test data (owner decision, 2026-07-07):

- **`upgrade()`**: `drop_table("spending_budgets")`, then `create_table("spending_budgets")` in
  the new shape — `start_month` (date, NOT NULL), `end_month` (date, nullable), nullable
  `category_id` + nullable `category_group_id` (composite FKs), the exactly-one-scope `CHECK`,
  no per-month unique constraint. Plus, additively: `create_table("category_groups")` and
  add-column + composite FK + index for `spending_categories.category_group_id`.
- **`downgrade()`**: symmetric — drop the new `spending_budgets`, recreate the old shape
  (`month_start`, NOT NULL `category_id`, `uq_budget_workspace_category_month`); drop the
  `spending_categories` column/FK/index; drop `category_groups`. Both directions discard budget
  rows by design, so the downgrade is **unconditionally clean** — no data preconditions, no
  lossy collapse, satisfying the always-downgrade-cleanly policy outright.
- No backfill, no rename, no data copy in either direction.

## Backend impact (`lifestack-api`)

- `app/spending/models.py`: `SpendingBudget` rewritten — `start_month`, nullable `end_month`,
  nullable `category_id`, `category_group_id`, scope CHECK, no per-month unique constraint; new
  `CategoryGroup` table; `category_group_id` on `SpendingCategory`.
- Alembic migration: destructive rebuild per the Migration section above.
- `app/spending/schemas.py`: `CategoryGroupCreate/Update/Response`; `CategoryUpdate` +
  `category_group_id` (free assignment); `CategoryResponse` + `category_group_id`; `BudgetCreate`
  → optional `category_id` XOR `category_group_id` (model validator) + `start_month` + optional
  `end_month` (validator: first-of-month, `end_month >= start_month`); `BudgetUpdate` allows
  `amount` and `end_month`; `BudgetChangeAmountRequest` (`amount`, `from_month`);
  `BudgetResponse` + `start_month`/`end_month`/`category_group_id`; `BudgetSpotlightItem`;
  `BudgetPerformanceResponse` + `groups` + `group_totals`.
- `app/spending/service.py`: `CategoryGroupService` (CRUD; delete's current/future-budget
  referential guard); `create_budget`/`update_budget` gain the **overlap check** (per scope,
  range-aware) and scope resolution (a `get_by_group_and_month`-style covering-budget lookup);
  `change_budget_amount` (single-transaction end-old + create-new, from_month validation);
  `get_budget_performance` reworked for per-month expansion + the independent `groups`/
  `group_totals`.
- `app/spending/repository.py`: `CategoryGroupRepository`; range-overlap query and covering-
  budget lookup for both scopes; current/future-budget existence check for group deletion;
  update `get_by_category_and_month`/`get_month_total`/list filters to range containment
  (`start_month <= m AND (end_month IS NULL OR end_month >= m)`).
- `app/spending/router.py`: `POST/GET/PATCH/DELETE /spending/category-groups[/{id}]`;
  `POST /spending/budgets/{public_id}/change-amount`.
- `app/imports/spending_import.py`: imported budgets set `end_month = start_month` (single-month);
  no import-contract change.
- `app/application/workflows.py` + `app/dashboard/schemas.py`: remove `month_budget`; add
  `budget_spotlight` (reuses current-month group performance, sorted, top 2, `daily_amount_left`);
  current-month category lookup for `top_overspent_categories` switches to range containment.
- Existing budget test fixtures and any demo/e2e seed data referencing `month_start` are
  rewritten to the new shape (those environments rebuild from scratch; nothing to preserve).
- No cash-model §6 entry — spending-ledger policy only.

## Web impact (`lifestack-web`)

- `types/spending.ts`: `CategoryGroup`; `Budget` gains `start_month`/`end_month`/optional
  `category_group_id`, `category_id` optional; category type + `category_group_id`; dashboard
  summary + `budget_spotlight` (typed `BudgetSpotlightItem` Zod schema), minus `month_budget`;
  `BudgetPerformanceResponse` + `groups`/`group_totals`.
- `services/spending.ts`: `/spending/category-groups` CRUD; budget create/update payloads carry
  `start_month`/`end_month`/scope; `changeBudgetAmount` calling the change-amount endpoint.
- `MasterConfigPage.tsx`: "Category Groups" panel (same table/dialog pattern as categories,
  `:691-724`, `:822-846`) — list/create/rename/delete (delete dialog: "N categories will be
  un-grouped, not deleted"; surfaces the backend 409). Category editor gains a group `<select>`
  ("No group" + groups), free assignment.
- `SpendingPage.tsx` budget form (`handleSaveBudget` ~`:880`, mutations `:385-392`): scope
  toggle (Category / Group); a **start month** and optional **end month** ("Ongoing" default);
  a "change amount from this month" action that calls the atomic change-amount endpoint;
  category dropdown lists all categories (nothing to filter).
- `BudgetsTab.tsx`: category and group cards in one list, each showing its range
  ("Jan 2025 – ongoing"); group cards show member-category count; progress-bar math
  (`spent/budget*100`) unchanged, group spend summed client-side from category totals (which
  carry `category_group_id`).
- `DashboardPage.tsx`: remove the "Budget remaining" tile + the `month_budget` note (`:48-50`,
  `:107-114`); add a "Budget spotlight" section (0–2 group cards: progress bar, status color,
  "$X/day left"). `top_overspent_categories` usage (`:135`, `:167-173`, `:269`) unchanged.
- `AnalyticsTab.tsx`: render the new `groups` list beside the existing `categories` list, each
  with its own totals — two separate itemized sections.

## Out of scope

- **Leftover rollover** (unused budget carrying into next month, YNAB-style) — the recurring
  target is flat per month; envelope carryover is a later spec.
- **Total-envelope budgets** (one lump sum across a range, for trips/projects) — the amount is
  always a monthly figure here; a fixed-total budget type is a separate future primitive.
- **DB-level non-overlap** (exclusion constraint / `btree_gist`) — enforced in the application;
  the extension dependency is deliberately avoided.
- **Preserving existing budget rows** — all current `spending_budgets` data is test data and is
  dropped by the migration (owner decision, 2026-07-07); no data path in either migration
  direction.
- **Nested groups**, **cross-workspace/shared groups** — flat, single-level, workspace-scoped.
- **Any reconciliation between category and group budgets** — independent by design.
- **Historical group-membership versioning** — group performance for past months uses *current*
  membership (budgets aren't versioned either).
- **A dedicated stand-alone budgets analytics page** — the itemized view lands in the existing
  Analytics tab; a separate page is a possible follow-up.
- **Renaming/touching `top_overspent_categories`** beyond the current-month lookup change — it
  was never a sum.

## Owner decisions (2026-07-07)

- **Category and group budgets are never reconciled into one number** — independent parallel
  primitives; the dashboard's combined total is removed, not redefined.
- **No cross-scope enforcement** — a category may hold its own budget while its group is also
  budgeted; both shown independently.
- **Daily-pace figure is group-only** (`daily_amount_left` on the spotlight).
- **Destructive rewrite of `spending_budgets` approved** — the owner has never used budgets;
  any existing rows are test data and are discarded. Drop-and-recreate migration, no backfill,
  unconditionally clean downgrade.

## Golden test scenarios (required before merge)

1. **Recurring coverage** — a single "$500/month, Jan 2025 → ongoing" budget makes every month
   from Jan onward evaluate against $500 in `get_budget_performance` and on the dashboard, with
   no per-month rows; a past `start_month` surfaces historical months automatically.
2. **Non-overlap** — creating/updating a category (or group) budget whose range overlaps an
   existing budget for the same scope is refused (422); non-overlapping adjacent segments (Jan–
   Jun, then Jul–ongoing) both persist.
3. **Segmentation preserves history, atomically** — `change-amount` on "$500, Jan→ongoing" with
   `{amount: 600, from_month: Jul}`: Jan–June evaluate against $500, Jul onward against $600;
   `from_month <= start_month` is refused (422); a forced failure on the create step rolls back
   the end-old write (no ended-with-no-successor state ever persists).
4. **Migration round-trip** — upgrade on a database containing old-shape budget rows succeeds
   (rows dropped, new empty table in place); downgrade recreates the old shape empty;
   upgrade→downgrade→upgrade is unconditionally clean with no data preconditions.
5. **Group CRUD** — create/rename/delete a group; delete un-groups members (untouched
   categories/transactions); delete refused (409) while a group budget covers the current or a
   future month.
6. **No cross-scope enforcement** — a category can be grouped while holding its own current-
   month budget (no error); an individual budget can be created for a category in a budgeted
   group (no error); both persist and read back independently.
7. **Budget performance — parallel lists** — a group of 3 categories with one group budget:
   `groups.actual_amount` = sum of all 3 categories' expense in range; all 3 categories still
   appear in `categories` with their own figures; `totals` and `group_totals` each internally
   correct and never summed together.
8. **Dashboard spotlight** — 3 groups with current-month-covering budgets at different
   utilization: `budget_spotlight` returns the top 2 by `utilization_pct` desc, each with a
   correct `daily_amount_left`; a group with no covering budget never appears; `month_budget`
   absent from the response.
9. **Web** — group panel CRUD; category group `<select>` assigns freely; budget form scope
   toggle + start/end month + "change amount from this month" via the atomic endpoint;
   `BudgetsTab` renders category and group cards with ranges and correct spend; `DashboardPage`
   shows the spotlight (0/1/2 cards) and no "Budget remaining" tile; `AnalyticsTab` renders both
   itemized lists; vitest coverage for all of the above.
