# Spec-053: Calendar Recurrence Modes (Month-End, Nth Weekday)

**Created:** 2026-07-03
**Status:** Implemented (api#109, merged 2026-07-04)
**Depends on:** existing recurring workflows (`RecurringTodoRule`, `RecurringTransaction`, `app/application/workflows.py` generation jobs)

---

## Problem

Both recurrence models (`recurring_todo_rules`, `recurring_transactions`) share the same
vocabulary: `frequency ∈ {daily, weekly, monthly, yearly}` × `interval ≥ 1`, advanced by
`_advance_due_date` (currently defined in `app/spending/service.py:1179` and imported from
there by `app/application/workflows.py` — including for *todo* rules). That vocabulary
cannot express two common calendar shapes:

1. **"Last day of the month"** — rent, salary credit, card statement dates.
2. **"Nth weekday of the month"** — "first Friday" reviews, "second Saturday" chores.

Worse, the natural attempt to approximate month-end today ("anchor it on the 31st") is
silently wrong — the monthly advance clamps to the target month's length using the
*current* date's day, so the anchor's intent is lost permanently after the first short
month:

| Rule anchored 2026-01-31, monthly | today's `_advance_due_date` | user intent (month-end) |
|---|---|---|
| 1st advance | 2026-02-28 | 2026-02-28 ✓ |
| 2nd advance | **2026-03-28** ✗ | 2026-03-31 |
| 3rd advance | **2026-04-28** ✗ | 2026-04-30 |
| every advance after | drifts on the 28th forever | tracks month-end |

The clamp is one-way: `day = min(current.day, monthrange(...))` never re-expands, because
after February the function only ever sees `current.day == 28`. Every existing monthly rule
anchored on the 29th–31st has already drifted this way.

What is deliberately **not** a problem: "every other day" (medication cadence) already
works today — `frequency=daily, interval=2`, exposed in both UIs — and recurring todos
already carry `due_time` + `timezone`. No change to those semantics here.

## Solution

Extend the shared recurrence vocabulary with a **monthly mode**, applied identically to
both rule tables, and fix the anchor-day drift as part of the default mode's definition.

### Schema change (both `recurring_todo_rules` and `recurring_transactions`)

| Column | Type | Notes |
|---|---|---|
| monthly_mode | enum: `"day_of_month"`, `"last_day"`, `"nth_weekday"`; default `"day_of_month"` | only meaningful when `frequency="monthly"`; schema validators reject it otherwise |
| by_weekday | smallint, nullable | 0=Monday … 6=Sunday (ISO), required iff `nth_weekday` |
| by_ordinal | smallint, nullable | 1–4 = first–fourth, **-1 = last**, required iff `nth_weekday` |

DB `CHECK` (same on both tables): `(monthly_mode = 'nth_weekday') = (by_weekday IS NOT NULL AND by_ordinal IS NOT NULL)`,
plus range checks `by_weekday BETWEEN 0 AND 6`, `by_ordinal IN (-1, 1, 2, 3, 4)`.
Existing rows get the default `day_of_month` with both nullables null — no data migration.
The `action_type`-style inline-enum Alembic pattern applies (no explicit pre-create;
`checkfirst=True` on downgrade).

The two columns + mode enum (rather than an RRULE string) keep validation database-level,
the UI a pair of dropdowns, and the advance function pure arithmetic — full RFC-5545 is
explicitly out of scope.

### Advance function: extract, extend, fix

Move `_advance_due_date` out of `app/spending/service.py` into a new shared
`app/core/recurrence.py` as `advance_due_date` — today a private spending function is
imported by the application layer and applied to todo rules, which is exactly the
cross-module reach-through the architecture forbids modules to depend on. Spending and
workflows both import from core afterwards; no behavior change from the move itself.

New signature takes the rule's calendar fields:

```
def advance_due_date(
    current: date, frequency: str, interval: int, *,
    anchor_day: int | None = None,          # anchor_date.day, for day_of_month
    monthly_mode: str = "day_of_month",
    by_weekday: int | None = None,
    by_ordinal: int | None = None,
) -> date
```

- **`day_of_month` (default, drift fix):** target day is
  `min(anchor_day, monthrange(target_year, target_month))` — the *anchor's* day, clamped
  per month, instead of the current date's day. A rule anchored on the 31st now yields
  Jan 31 → Feb 28 → **Mar 31** → Apr 30. With `anchor_day=None` the old current-day
  behavior is kept (callers always pass it; the default exists so the function is safely
  callable in isolation).
- **`last_day`:** target is `monthrange(target_year, target_month)` — always the final
  calendar day. Anchored 2026-01-31: Feb 28 → Mar 31 → Apr 30 → May 31 (and Feb 29 in a
  leap year such as 2028).
- **`nth_weekday`:** compute the target month (current month + interval, same year-roll
  arithmetic as today), then the `by_ordinal`-th `by_weekday` of that month (`-1` = last).
  First Friday (`by_weekday=4, by_ordinal=1`) from 2026-07-03: → 2026-08-07 → 2026-09-04
  (Aug 1 2026 is a Saturday, Sep 1 a Tuesday — the "first Friday" lands wherever the
  calendar puts it, which is the point).
- `daily`/`weekly`/`yearly` are untouched; `interval` composes with every mode
  ("every 2nd month, last day" works).

**Behavior-change note (change-control):** the `day_of_month` drift fix alters future
advances of *existing* rules anchored on day 29–31 — they stop drifting and re-expand
toward their anchor day. That is the intent (the current behavior is a bug), it is
forward-only (no stored `next_due_date` is rewritten, no backfill), and it is called out
in the PR description. Rules anchored on days 1–28 are bit-for-bit unaffected.

### Generation jobs

`recurring_transactions_job` / recurring-todo generation in `workflows.py` change only in
what they pass to the advance function (the rule's new fields). Catch-up limits
(`RECURRING_*_CATCHUP_LIMIT_DAYS`), advisory locks, and idempotency semantics are
untouched.

### API / schema impact

- `RecurringTodoRuleCreate/Update` and `RecurringTransactionCreate/Update`
  (`app/todo/schemas.py`, `app/spending/schemas.py`): new optional fields with cross-field
  validation mirroring the DB CHECK (`nth_weekday` ⇒ both `by_weekday`/`by_ordinal`
  present; any mode other than default ⇒ `frequency="monthly"`). Response schemas echo the
  fields.
- No endpoint shape changes otherwise; existing clients that never send `monthly_mode` get
  today's behavior (modulo the drift fix).

### Frontend (`lifestack-web`)

Both recurrence forms — the recurring-todo form in `TodoPage.tsx` and the spending
`RecurringTab.tsx` — gain, **only when frequency = monthly**, a mode select
("On day N" / "On the last day" / "On the Nth weekday") and, for the third mode, ordinal +
weekday selects. Rule list rows render a human string ("Every month on the last day",
"Every 2nd month on the first Friday"). Vitest coverage for the conditional rendering and
payload shape.

## Backend impact (`lifestack-api`)

- `app/core/recurrence.py`: new module (function moved + extended); `app/spending/service.py`
  drops its private copy; `app/application/workflows.py` imports from core.
- `app/todo/models.py`, `app/spending/models.py`: three columns each + CHECKs.
- `app/todo/schemas.py`, `app/spending/schemas.py`: fields + validators.
- `alembic/versions/`: next free number at implementation time (0037 is claimed by
  spec-051; 0038 may be claimed by spec-052 — take the next by merge order). Both tables in
  one migration, clean downgrade.
- `docs/JOBS.md`: note the advance-function move and modes.

## Out of scope

- **Sub-daily / hourly recurrence** — explicitly dropped (owner decision, 2026-07-03).
  Recurrence stays day-granular; time-of-day reminding is delivery's job (spec-052 web
  push + existing `due_time`/`timezone` on todo rules), not more generated rows.
- **Full RFC-5545 RRULE** (BYSETPOS combinations, multi-weekday weekly sets, EXDATE…) —
  the two added modes cover the actual reported needs; an RRULE engine is a rewrite, not a
  refinement.
- **Weekly "on Mon+Thu" multi-day sets** — expressible today as two rules; not worth the
  schema complexity until that becomes painful.
- **Backfilling drifted `next_due_date` values** — existing rules that already drifted to
  the 28th keep their current next occurrence and simply stop drifting from there
  (non-retroactivity house rule). A user who wants the original date back edits the rule.
- **Recurrence for other entities** (budgets, transfers) — nothing else has a recurrence
  model today.

## Golden test scenarios (required before merge)

1. **Drift fix** — `day_of_month`, anchor 2026-01-31: advances yield Feb 28, Mar 31,
   Apr 30 (not Mar 28); anchor on day ≤ 28 produces byte-identical results to the old
   function across 24 consecutive advances (regression guard).
2. **Last day** — anchor 2026-01-31, `last_day`: Feb 28 → Mar 31 → Apr 30; leap-year
   February (2028-02-29).
3. **Nth weekday** — first Friday from 2026-07-03: 2026-08-07 → 2026-09-04; last Sunday
   (`by_ordinal=-1`) across a year boundary (Dec → Jan); `interval=2` composition.
4. **Validation** — `nth_weekday` without `by_weekday`/`by_ordinal` rejected (422 and DB
   CHECK); `monthly_mode` with `frequency="weekly"` rejected; both tables.
5. **End-to-end generation** — a `last_day` recurring transaction and an `nth_weekday`
   recurring todo each generate their instance on the correct date via the real jobs, and
   `next_due_date` advances correctly afterward.
