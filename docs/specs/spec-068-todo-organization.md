# Spec-068: Todo organization — date-grouped task view + subtasks

**Created:** 2026-07-09
**Status:** Implemented (api#144, web#102, merged 2026-07-09)
**Scope:** multi-repo, user-facing — `lifestack-api` (subtask model + list-sort option) and `lifestack-web` (regrouped Todo page). Two PRs, api first.
**Depends on:** spec-019 (recurring todos), spec-052 (push reminders), spec-053 (calendar recurrence modes), spec-067 (morning briefing — shares the "due today" vocabulary). Product direction: the todo module was inspired by Google Tasks the way Spending was inspired by Spendee and Net Worth by INDmoney — but unlike those two, it never adopted its inspiration's organizing skeleton (owner assessment, 2026-07-09).

---

## Problem

The Todo page is a single flat stream ordered by `created_at desc` (`TodoRepository.get_all`), with an all/open/completed filter and pagination. Against Google Tasks — the module's explicit inspiration — three structural gaps stand out:

1. **No date-based grouping.** The page never answers "what's on today?" Overdue is a red tint computed inline per row (`TodoPage.tsx`, `isOverdue`); due-today isn't surfaced at all. Meanwhile the Morning Briefing (spec-067) leads with exactly these two lines and deep-links to `/todo` — where the user lands on a creation-ordered stream that doesn't visually confirm what the briefing just said.
2. **No subtasks.** The `Todo` model has no parent-child relationship; even Google Tasks' single level of nesting is absent. "Plan trip" and its five steps are five unrelated rows.
3. **Creation-date ordering.** For a daily-driver task list, due date is the ordering that matters; created-at ordering buries this week's tasks under whatever was added most recently.
4. **Row actions are invisible on touch.** Edit/delete exist per row (Task 5 wired them through the shared ConfirmDialog) but sit behind `opacity-0 group-hover:opacity-100` — hover-revealed, so on the installed PWA / any touch device they never appear at all (owner report 2026-07-09: "no UI to delete"). On desktop they're merely undiscoverable.

What the module already has and this spec deliberately builds on rather than around: priority (Google Tasks lacks it), strong recurrence (`RecurringTodoRule` with calendar modes), push reminders (`reminded_at` + spec-052), system-generated todos (`system_key` guardrail tasks), and capture ("remind me to pay rent" → structured todo).

## Goals

- The Tasks tab groups open todos into date buckets: **Overdue / Today / Upcoming / Later / No due date** — the daily-driver view Google Tasks always provides.
- Within each bucket, deterministic ordering: due date asc, then priority (high > medium > low), then `created_at` — never "whatever was inserted last".
- **One level of subtasks**: any todo can have children; children cannot have children. Completing a parent completes its open subtasks; deleting a parent deletes its subtasks (confirm dialog states the count).
- The page's "due today" set visually matches the briefing's due-today line for the same instant — the briefing's deep link lands on a page that agrees with it.
- **Row actions (edit/delete) usable on every device**: hover-reveal is a pointer-only enhancement, never the only path — on touch, actions are always visible.
- No regression to reminders, recurring generation, capture, exports, or system (guardrail) todos — subtasks are ordinary todos with a parent pointer.

## Non-goals

- **Multiple lists/projects.** For a single-user life-OS, buckets-by-date do the organizing work lists do in Google Tasks; a `list_id` concept can layer on later without conflicting with anything here.
- **Manual drag ordering and stars/pins.** Priority already covers "this one matters"; manual ordering fights the deterministic date ordering that makes the page match the briefing.
- Nesting deeper than one level.
- Capture-path subtask creation ("add a subtask to X" by voice) — capture keeps creating top-level todos; the tools are untouched.
- Recurring rules generating subtasks, or rules themselves being nestable.
- Google Tasks import.

## Solution

### A. Data model + migration (lifestack-api)

Add to `Todo` (`app/todo/models.py`):

```python
parent_id: int | None = Field(default=None, foreign_key="todos.id", index=True, ondelete="CASCADE")
```

Alembic migration `0045_todo_parent_id.py`: additive nullable column + index + self-referential FK with `ON DELETE CASCADE`. **Retroactivity: none** — existing rows get `NULL` (top-level), no backfill.

**Service rules (`TodoService`):**

- `create_todo` / `update_todo` accept an optional `parent_public_id`. Validation (reject with `ValidationError`): parent must exist in the same workspace; parent must not itself have a parent (one level); a todo with children cannot be given a parent; a todo cannot be its own parent. Clearing (`parent_public_id: null` on update) promotes a subtask to top level.
- **Completing a parent** (`completed: false → true`) also marks its open subtasks completed, in the same transaction; each auto-completed subtask gets its own audit entry (`action="complete"`, details noting the cascade). Un-completing a parent does NOT resurrect subtasks.
- **Deleting a parent** cascades at the DB level; the service counts children first so the audit log and the API error/confirm surfaces can state "and N subtasks".
- Subtasks are otherwise ordinary todos: they may carry their own `due_date` (reminders fire per spec-052 as usual), priority, and completion state. Completing all subtasks does not auto-complete the parent (Google Tasks behavior).
- `system_key` guardrail todos and recurring-rule-generated todos are always created top-level (no code change needed — nothing passes a parent); they may be given subtasks by the user like any todo.

**Overdue/due-today semantics:** subtasks count in `get_summary_counts`, `get_overdue_items`, `get_next_due_items` exactly like any todo (they are rows in the same table; no query change). The briefing needs no modification.

### B. API surface (lifestack-api)

- `TodoCreate` / `TodoUpdate`: optional `parent_public_id: uuid.UUID | None`.
- `TodoResponse`: add `parent_public_id: uuid.UUID | None` and `subtask_count: int` (0 for leaves; computed in the list/get queries via a grouped count, not N+1).
- `GET /todo/` gains `sort: Literal["created_at", "due_date"] = "created_at"` — default unchanged for back-compat. `sort=due_date` orders `due_date ASC NULLS LAST`, then priority (high first), then `created_at ASC`, implemented in `TodoRepository.get_all`.
- `DELETE /todo/completed` → bulk-delete all completed todos in the workspace, returns `{deleted: N}` — powers the "Clear completed" action (Google Tasks' "Delete all completed tasks" equivalent). Audit-logged as one entry with the count.
- Otherwise no new endpoints. Bucketing is a **client** concern because "today" is a local-timezone question and the browser knows the user's timezone; the server provides the total order, the client draws the group headers. (The briefing's server-side `now.date()` boundary stays as is — owner-accepted for v1, see Owner decisions.)

### C. Todo page (lifestack-web)

**Tasks tab, open todos** (`TodoPage.tsx` — at 876 lines, extract the new grouped list into `src/pages/todo/` components in the same pass):

- Fetch open todos with `sort=due_date` and a raised page size (200) so grouping isn't broken by pagination; if `total` exceeds the page, show a "N more not shown" footer link that loads the next page (grouping continues to work because the sort is global).
- Group client-side by local calendar day: **Overdue** (due < today, styled with the existing destructive tone), **Today**, **Upcoming** (next 7 days, rows show weekday), **Later** (beyond 7 days, rows show date), **No due date**. Empty buckets are hidden; section headers carry counts. All-empty gets the existing empty-state pattern with the create CTA.
- Within a group, subtasks render indented under their parent with the parent showing "2/3" completion progress; the parent row's checkbox triggers the cascade (the confirm dialog for **delete** on a parent states "This will also delete its N subtasks" via the shared ConfirmDialog).
- Add-subtask affordance on each top-level row (and in the edit modal: a read-only parent name with a "remove from parent" action on subtasks). The create/edit modal never offers parent selection for a todo that has children (one level, mirrored client-side).
- **Row actions on every device (owner requirement, 2026-07-09):** the edit/delete icon group loses `opacity-0 group-hover:opacity-100` as its only visibility path. On pointer devices the hover-reveal may stay as polish; on touch/coarse pointers (CSS `@media (hover: none)` / Tailwind `pointer-coarse:`) the actions render always-visible at reduced emphasis. Same treatment for the recurring-rules tab rows, which share the pattern. Delete keeps the existing shared ConfirmDialog flow.
- **Completed todos** move from a mixed status filter to a collapsed "Completed" section at the bottom (flat, paginated, `created_at desc`, no nesting — a completed subtask shows a small "↳ parent title" chip), with per-row delete and a **"Clear completed"** action (ConfirmDialog stating the count, backed by `DELETE /todo/completed`). The `all/open/completed` filter is retired; deep links using `?status=` keep working by mapping to scroll/expand of that section.
- Service/type/queryKey updates per house conventions (`todoService`, Zod schemas gain the two new fields + sort param).

**Recurring tab:** unchanged.

### D. Docs

Same-PR updates per house rule: `docs/ERD.md` (new `parent_id` column + self-edge on TODOS), `.agent/context.md` only if conventions change (they don't). `docs/specs/README.md` gains this spec's index row (2026-06→07 feature wave).

## Test plan

- **api unit (Red first):** parent validation matrix (cross-workspace, two-level, self, has-children); complete-parent cascade completes only open subtasks + audit rows; delete-parent cascade + count; `sort=due_date` total order incl. NULLS LAST and priority tiebreak; `subtask_count` correctness without N+1 (assert query count); `DELETE /todo/completed` deletes only completed rows in the caller's workspace, returns the count, and route-resolves correctly alongside `DELETE /todo/{todo_id}` (register before the UUID path).
- **api integration:** create/update/clear `parent_public_id` through the router (workspace isolation, RBAC member+); briefing due-today/overdue lines unchanged with subtasks present.
- **web (Red first):** bucketing boundaries (yesterday/today/+7d/+8d/no-date) with a frozen clock; group ordering within buckets; parent checkbox cascade UI + delete confirm copy; completed-section collapse + Clear-completed confirm with count; row actions render without hover on coarse pointers (assert the always-visible class path); Zod parse of new fields. Gates: vitest ≥ 70, `npm run build`, lint.
- **e2e:** extend `todo-smoke.spec.ts` — create a parent + subtask, assert indentation and "1/2" progress, complete the parent, assert the subtask completes; assert an overdue seeded todo renders under the "Overdue" header; on a touch-emulated context (Playwright `hasTouch`), assert the delete button is visible and the delete flow completes.

## Rollout

Two PRs: api (migration + model/service/router + tests + ERD), then web (grouped view + subtask UI + tests + e2e companion updates in `../lifestack-e2e`). No feature flag — the api change is additive; the web change replaces the list rendering wholesale but touches no other page. The migration is reversible (drop column).

## Owner decisions (2026-07-09)

1. **Timezone boundary drift: accepted for v1.** The page buckets "Today" in the browser's local timezone; the briefing keeps its server-side UTC date boundary (`workflows.py`, `_due_today_todo_lines`). The sets can differ between 00:00 and 05:30 IST, but the briefing job fires at ≈08:00 IST when they agree — "fine for the moment" (owner). Revisit only if the briefing schedule or the owner's timezone changes.
2. **Upcoming window: 7 days.**
3. **Completed section: collapsed at the bottom**, replacing the `all/open/completed` status filter.
4. **Added requirement — row actions must be usable on touch.** The owner reported "no UI to delete": per-row edit/delete exist but are hover-revealed (`opacity-0 group-hover:opacity-100`), which never fires on the installed PWA. Fix per §C (always visible on coarse pointers; hover-reveal stays as pointer-device polish). The "Clear completed" bulk action + `DELETE /todo/completed` endpoint were added alongside it (Claude's addition matching Google Tasks — strike at review if unwanted).
