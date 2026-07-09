# Spec-069: Health Memory v1 — medications + weight (manual, web/PWA-first)

**Created:** 2026-07-09
**Status:** Owner decisions recorded 2026-07-09 (all open questions resolved) — awaiting owner commit; ready for implementation once committed
**Scope:** multi-repo, user-facing — `lifestack-api` (new `health` module, reminder job, briefing/summary/export integration) and `lifestack-web` (a lean Health page). ~3 PRs.
**Depends on:** spec-052 (web push), spec-053 (calendar recurrence modes — schedule vocabulary reused), spec-067 (morning briefing — live; health lines slot in as new line types), weekly summaries, exports module. Product direction: roadmap Track 1 "Health Memory" (re-sequenced ahead of Mobile Companion, owner-accepted 2026-07-08) and PRODUCT-ASSESSMENT "Rethink 2". **Owner decision D9 (2026-07-08): v1 scope is medications + weight only.**

---

## Problem

The product is called **Life**stack but ships one real life domain (finance) plus a todo list. The roadmap's own breadth story — "a shared data model across life domains" — needs a second domain to be credible, and Health Memory is the cheapest one: every hard ingredient already exists. Push delivery (spec-052), an installable PWA (web spec-005), workspace-scoped scheduler jobs with advisory locks (`run_workspace_job`), the briefing (spec-067), weekly summaries, and exports are all live. What's missing is only the domain itself: nowhere to record "took my BP tablet" or "72.4 kg this morning."

v1 is deliberately manual and small — medications (schedule, reminders, adherence log) and weight (quick log, trend) — proving the pattern "log in 10 seconds on the PWA, get reminded by push, see it in the briefing and weekly summary" before any device sync exists.

## Goals

- **Medications:** define a medication with a recurrence-based dose schedule (daily / every N days / weekly on chosen days / every N weeks / monthly / every N months); get push reminders at dose times; log taken/skipped in one tap; see today's doses and a basic adherence history.
- **Weight:** log a weight in one field; see latest value, 7/30-day change, and a trend chart. **Kilograms only in v1 (owner decision).**
- **Roadmap data rules are binding:** every health record carries source metadata (`manual` for all of v1); health records are exportable via the existing exports module; the UI labels values as user-entered.
- **Integration over invention:** briefing lines (dose due/missed, weekly weight move), a weekly-summary section, and reminders all reuse the existing jobs/notifications/briefing/recurrence machinery.
- Capture-path logging ("log weight 72.4" → structured record with confirmation card) as a **SHOULD** — same tool pattern as spec-066, allowed to land in a follow-up PR.

## Non-goals

- Sleep, workouts, vitals, labs, symptoms (owner D9 — these are later Health Memory slices, same table patterns).
- Any device or health-app sync, camera/document extraction (Tracks 2–4). `source_type` values beyond `manual` are reserved, not implemented.
- **Nested "course" schedules** — e.g. the owner's vitamin D regimen: once a week *for one month, every 3 months*. No consumer med app models this natively and a nested-periodicity engine is not worth v1 complexity. The supported composition (owner-confirmed): create the medication with its in-course schedule (weekly) and an `end_date` one month out, plus a recurring todo every 3 months ("Restart vitamin D — re-enable in Health"); re-enabling is one tap (`is_active` + fresh dates). §D gives this a small assist (a "duplicate/restart course" action on inactive medications).
- **No diagnosis, dosing advice, or health recommendations of any kind** — roadmap Trust and Safety Boundaries ("should not diagnose medical conditions, prescribe medication"). v1 renders what the user recorded; the only computed outputs are counts, deltas, and schedule arithmetic.
- Medication database/autocomplete, interaction checking, refill auto-reordering (a free-text refill note is in; automation is out).
- Weight display units other than kg (owner decision — a display-unit setting can layer on later without data change; storage is canonical kg regardless).
- Caregiver/multi-user sharing semantics beyond the existing workspace RBAC.
- Generating todos per dose — doses would flood the todo list; medications get their own surface plus briefing visibility. (`ensure_system_task` stays finance-only.)
- Escalation/nag pushes — one reminder per dose slot, nothing further (owner decision; the briefing's missed line is the follow-up).

## Solution

### A. Data model (lifestack-api — new `app/health/` module)

New module per `docs/PATTERNS.md` (models/schemas/repository/service/router), workspace-scoped like todo/spending. One alembic migration (next free number after spec-068's; the two specs are independent — whichever merges second renumbers), three tables:

**`medications`** — id, public_id, workspace_id, user_id, `name` (free text, ≤120), `dose_text` (free text, e.g. "500 mg", ≤60), `notes`/`refill_note` (≤500), **schedule** (owner decision — recurrence-based, reusing the `RecurringTodoRule` vocabulary and `core/recurrence.py` helpers):

- `frequency` (`daily` | `weekly` | `monthly`), `interval` (≥1) — together covering *every N days*, *every N weeks*, *every N months*;
- `days_of_week` (JSON array of ints 0–6, only valid when `frequency=weekly`; e.g. Mon/Wed/Fri) — the one extension beyond `RecurringTodoRule`, which models single-cadence weekdays only;
- `anchor_date` (start; for `monthly` also fixes the day-of-month via the existing `advance_due_date` day-of-month semantics, including short-month clamping), `end_date` (nullable — this is what makes a "course"), `timezone` (IANA, validated like `RecurringTodoRule`);
- `times` (JSON array of "HH:MM" strings, ≥1 — dose times on each scheduled day);
- `is_active`, `reminders_enabled` (bool, default true), source triplet (below), timestamps.

Validation extends `validate_recurrence_fields` conventions: `days_of_week` rejected unless weekly; `monthly_mode`/ordinal machinery is **not** exposed (day-of-month only — "3rd Friday" dosing isn't a real regimen; revisit only if asked).

**`medication_events`** (adherence log) — id, public_id, workspace_id, user_id, `medication_id` FK (ON DELETE CASCADE), `scheduled_for` (tz-aware datetime — the dose slot this event answers), `status` (`taken` | `skipped`), `logged_at`, `note` (≤200), source triplet, timestamps. Unique constraint on (`medication_id`, `scheduled_for`) — one answer per dose slot; re-logging updates.

**`weight_entries`** — id, public_id, workspace_id, user_id, `measured_at` (tz-aware datetime), `weight_kg` (Decimal(6,2), canonical kg — the only unit in v1), `note` (≤200), source triplet, timestamps. Index on (workspace_id, measured_at).

**Source triplet (roadmap data rule):** `source_type` (v1: always `"manual"`), `source_ref` (nullable), `source_import_id` (nullable) — the exact column pattern `spending_transactions` already carries, so document extraction/sync/import later slot in without migration churn.

**Schedule semantics:** scheduled days are derived from (`frequency`, `interval`, `anchor_date`, `days_of_week`), advancing with `core/recurrence.py::advance_due_date` for daily/monthly cadences and week-stepping + `days_of_week` filtering for weekly; a dose slot exists at each `times` entry on each scheduled day between `anchor_date` and `end_date`, interpreted in the medication's `timezone`. A slot's derived status on read: `taken`/`skipped` if an event row exists, `pending` if in the future, `missed` if more than `HEALTH_DOSE_GRACE_HOURS` (setting, **default 4 — owner-confirmed**) past with no event. **Missed is computed, never stored** — logging late flips it to taken, no reconciliation job needed.

### B. API surface

All endpoints workspace-scoped, `require_min_role("member")`, public-id addressed, paginated lists — the todo router is the template.

- `POST/GET/PATCH/DELETE /health/medications` (+ list; PATCH covers pause/resume via `is_active` and date changes — a course restart is PATCH `{is_active: true, anchor_date, end_date}`). Deleting a medication cascades its events; the confirm surface states the event count (same rule as spec-068's parent delete).
- `GET /health/medications/schedule?date=` → the dose slots for a day with derived statuses — powers the "Today's doses" checklist and the briefing.
- `PUT /health/medications/{id}/events` → upsert an event for a slot (`scheduled_for`, `status`, optional note).
- `POST/GET/DELETE /health/weight` (list takes a date range; no PATCH — corrections are delete + re-log, keeping the log append-honest). `GET /health/weight/trend?days=` → entries + derived latest/min/max/delta stats, computed server-side once, reused by page, briefing, and summary.
- Audit logging on every mutation via the existing `AuditLogger` pattern (module `"health"`).

### C. Reminders + integrations (all reuse, no new machinery)

- **`medication_reminder_job`** — clone of `todo_reminder_job`: `run_workspace_job`, new advisory-lock constant in `app/core/constants.py`, cron every `TODO_REMINDER_INTERVAL_MINUTES`-style interval setting. Per workspace: find dose slots entering the window for active medications with `reminders_enabled`, write one `Notification` (category `medication_reminder`, title = med name, body = dose text + time, route `/health`); spec-052 push delivery and the existing per-category preference row handle the rest. **Exactly one push per dose slot, no follow-up nudges (owner decision)** — the briefing's missed line is the only escalation. Idempotency via a `last_reminded_slot` marker per medication (the `reminded_at` pattern, keyed to the slot datetime). Registered in `app/main.py` — and **verified wired** (the spec-065 lesson, restated in spec-067 §C).
- **Briefing lines (spec-067):** two new line types in `MorningBriefingWorkflow`, per-section try/except like the rest: (a) doses due today / missed yesterday — `warning` if any missed, else `info`, route `/health`; (b) weight weekly move — `info`, only when ≥2 entries exist in 7 days. Domain tiebreak: after todos, before budget lines (health is the day's "life" half). Line texts state facts only ("2 doses due today", "weight −0.4 kg this week") — no advice, per the trust boundary.
- **Weekly summary:** additive `health_summary: dict` on `WeeklySummaryResponse` (doses scheduled/taken/missed counts, adherence %, weight delta) — same shape convention as `todo_summary`/`spending_summary`; omitted/empty when the workspace has no health data.
- **Exports:** add `"health"` to `SUPPORTED_MODULES`; JSON arrays + `health/medications.csv`, `health/medication_events.csv`, `health/weight_entries.csv` following `ExportService`'s existing per-module writers. Source triplet columns included — that's the portability the roadmap data rule promises.
- **Capture (SHOULD, may be PR-4 or fold into PR-2):** two tools in `app/capture/tools.py` — `log_weight(weight_kg, note?)`, `log_medication_event(name, status, time?)` (name matched against active medications, ambiguity returns a clarify message, never a guess) — returning the spec-066 confirmation-card shape (`record type, summary, route /health`).

### D. Health page (lifestack-web)

New route `/health`, nav entry under the "Life" section beside Todos. One page, three zones (top to bottom):

1. **Today's doses** — checklist of the day's slots (med name, dose text, time, status chip); tap → taken, secondary action → skipped with optional note; missed slots visually distinct. Empty state: "No medications scheduled" + add CTA.
2. **Weight** — quick-log input (kg) + latest/Δ7d/Δ30d stat row + trend chart reusing the net-worth chart component patterns and the shared date formatters (Task 13 PR-3 consolidation).
3. **Medications** — list with add/edit/pause via the shared Radix Dialog; schedule editor mirrors the recurring-todo form's disclosure pattern: frequency + interval + dose times up front, weekday toggles when weekly, anchor/end dates + timezone (defaulting to browser) behind the "Advanced schedule"-style disclosure, with the `describeRecurrence`-style natural-language summary line ("Every week on Mon, 09:00 — until 15 Aug"). Inactive (course-ended or paused) medications show a **"Restart course"** action that pre-fills the edit dialog with `is_active: true` and fresh anchor/end dates — the one-tap half of the owner's vitamin-D composition pattern (the recurring-todo half is created in Todos as usual).

House conventions throughout: query keys in `src/lib/queryKeys.ts`, `useInvalidatingMutation` (success/error toasts free), Zod-parsed service, colocated tests. Every record row carries the small "entered manually" affordance (a tooltip/label off `source_type`) — the UI-distinguishes-entry-types rule, trivial now, load-bearing when extraction arrives. Notifications page label map gains `medication_reminder` → "Medication reminders".

## Test plan

- **api unit (Red first):** schedule arithmetic — daily/every-N-days, weekly with multi-weekday + interval, monthly incl. short-month clamp (31st anchor → Feb), timezone + DST boundary, end_date cutoff; derived slot status incl. grace window and late-log flip; `days_of_week` validation matrix (rejected for daily/monthly); event upsert uniqueness; medication delete cascade + count; weight trend stats; RBAC + workspace isolation on every endpoint (existing matrix pattern).
- **api integration:** `medication_reminder_job` — creates exactly one notification per due slot, idempotent on re-run, second concurrent invocation skips on the advisory lock, disabled/paused/course-ended meds skipped; briefing lines appear/omit correctly and degrade to omission on section failure; export with `health` module produces the three CSVs + JSON keys; weekly summary gains `health_summary` only when data exists.
- **web (Red first):** dose checklist state transitions, quick-log + trend render, schedule editor validation incl. weekly weekday toggles and the natural-language summary, restart-course prefill, Zod parses. Gates: vitest ≥ 70, `npm run build`, lint.
- **e2e:** one `health.spec.ts` journey — create a medication with a slot in the past-grace window and one upcoming, load `/health`, assert missed + pending chips, log the pending dose taken, log a weight, assert the trend row updates; assert the briefing shows the health line on the dashboard.

## Rollout

Three PRs, api-first: **PR-1** api core (migration, module, CRUD + schedule/trend endpoints, exports, ERD/PATTERNS docs updated same-PR); **PR-2** api integrations (reminder job + `docs/JOBS.md`, briefing lines, weekly-summary section; capture tools here if cheap, else follow-up); **PR-3** web (`/health` page + nav + notification label). No feature flag — everything is additive; the nav entry is the only discovery surface and ships last.

## Owner decisions (2026-07-09)

1. **Schedule richness: recurrence-based, not fixed-weekday-mask.** Every-N-days / weekly-on-days / every-N-weeks / monthly / every-N-months supported directly by reusing the `core/recurrence.py` vocabulary. Nested course regimens (the owner's vitamin D: weekly for a month, every 3 months) stay a non-goal, handled by the owner-suggested composition — a course medication with `end_date` + a recurring todo to restart — assisted by the "Restart course" action in §D.
2. **Weight: kg only.** Canonical kg storage; no display-unit setting in v1.
3. **Missed-dose grace window: 4 hours**, computed on read, env-overridable (`HEALTH_DOSE_GRACE_HOURS`).
4. **Reminder cadence: one push per dose slot, nothing further.** No escalation or "still unlogged" nudges; the morning briefing's missed line is the only follow-up.
