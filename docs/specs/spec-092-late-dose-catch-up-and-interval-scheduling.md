# Spec-092: Late-dose catch-up + opt-in interval-from-last-dose scheduling

**Created:** 2026-07-24
**Status:** Implemented locally, uncommitted — full api + web suites green (api 948 passed /
84% cov; web green / 77% line cov). Commit + PRs held until 21:00 2026-07-24 per owner
instruction; branch `feat/health-catch-up-interval-scheduling` in both repos.
**Scope:** multi-repo, user-facing — `lifestack-api` (schema migration, schedule
arithmetic, new overdue endpoint) and `lifestack-web` (Health page date navigation +
catch-up section, medication schedule-mode selector). 2 PRs (api first, then web).
**Depends on / extends:** spec-069 (Health Memory v1 — medications + weight). Reuses that
module's derived-slot model, `HEALTH_DOSE_GRACE_HOURS`, audit logging, and Zod/service
web conventions.

---

## Problem

Two real gaps surfaced by the owner on an "every 2 days" (`daily`, `interval=2`)
medication:

1. **You cannot mark a dose once its day has passed.** The Health page hard-pins the
   schedule query to `today` (`HealthPage.tsx` — `todayDate()`), and there is no date
   navigation. For an every-N-days medication, an "off" day shows "No medications
   scheduled today," and a scheduled day's slot becomes unreachable the moment the day
   rolls over. The backend already accepts a late log against any slot
   (`upsert_event` does not gate on "today"), so this is purely a discovery/UX gap: the
   past slot exists and is answerable, the UI just never surfaces it again.

2. **Real-world adherence is interval-based for some medications.** People often take an
   every-N-days dose a day late; for a class of medications the clinically meaningful
   quantity is the *interval since the last actual dose*, not a fixed calendar grid.
   Today's model is always a fixed anchor grid: a late dose does not shift future doses.

### Consensus on #2 (why this is opt-in, not the default)

There is no universal answer — it is **drug-class dependent**:

- **Fixed-schedule** medications (most chronic meds; weekly bisphosphonates; anything
  where steady calendar cadence matters). Standard missed-dose guidance is "take it when
  you remember, then resume the normal schedule" — i.e. the fixed grid. Do **not** shift
  future doses.
- **Minimum-interval** medications ("wait ≥ N days between doses"). Here the interval
  from the *actual* last dose is what matters, so re-anchoring to the day it was taken is
  correct.

The dominant guidance maps to the fixed grid we already have. **Silently re-anchoring a
chronic-med schedule because someone logged one dose late would be the risky default** —
it quietly drifts the regimen off its intended grid. So re-anchoring becomes an explicit
**per-medication mode**, never automatic, and `fixed` stays the default for every
existing and new medication.

## Goals

- **Catch-up (both modes):** from the Health page, reach and answer (taken/skipped) any
  recently-missed dose without hunting — via (a) day navigation (prev/next day on the
  schedule view) and (b) a proactive "Catch up" section listing unanswered past-grace
  doses across active medications within a lookback window.
- **Honest intake time:** an event records *when the dose was actually taken*
  (`taken_at`), defaulting to now when marking, so a late/back-dated log carries the
  truth rather than the slot time. This is the field interval mode anchors on.
- **Opt-in interval scheduling:** a medication may set
  `schedule_mode = interval_from_last_dose` (daily interval only). Its next dose is
  computed as *last actual dose + interval days*; marking a dose taken (even late)
  re-anchors the next dose off the intake day.
- **No silent behavior change:** every existing medication is `fixed`; the fixed grid,
  reminder job, briefing, summary, and exports behave exactly as before for `fixed`.

## Non-goals

- Interval mode for `weekly`/`monthly` frequencies — rejected at validation. "Every N
  weeks/months from last dose" is not a real regimen shape and week/month cadences are
  inherently fixed-calendar. Interval mode ⇒ `frequency = daily`.
- Materialising a *future grid* for interval medications. By construction only the **next
  dose** is knowable (each subsequent dose re-anchors off an actual intake), so the
  schedule/overdue views show at most one live interval-mode slot per medication.
- Auto-migrating any existing medication to interval mode. Owner sets it per med.
- Escalation/nag pushes, dosing advice, or any change to the reminder cadence (one push
  per slot — spec-069 owner decision stands). The reminder job simply reads whatever the
  next slot is under the med's mode.
- Retroactive rewrite of historical `medication_events` — `taken_at` backfills to
  `logged_at` for existing rows via server default expression at migration time only
  (see §A); no data job touches history afterward.
- Weight — untouched.

## Solution

### A. Data model (lifestack-api) — migration `0059_medication_schedule_mode`

Down-revision `0058_job_failures`. Two additive columns, both with working `downgrade()`:

- **`medications.schedule_mode`** — `VARCHAR(24) NOT NULL DEFAULT 'fixed'`, plus
  `CheckConstraint("schedule_mode IN ('fixed','interval_from_last_dose')",
  name="ck_medications_schedule_mode")`. Every existing row becomes `fixed` via the
  server default.
- **`medication_events.taken_at`** — `TIMESTAMPTZ NULL`. Semantics: the moment the dose
  was actually taken. For `taken` events it is set (defaults to now / the upsert value);
  for `skipped` events it stays `NULL` (nothing was taken). Existing rows: backfill
  `taken_at = logged_at` **for `status='taken'` rows only**, as a one-shot
  `op.execute(...)` inside the migration `upgrade()` (this is not a snapshot/ledger table
  — it is a derived adherence log — so the append-only-history rule does not apply; the
  backfill is documented here and confined to the migration).

`downgrade()` drops both columns (and the check constraint) cleanly.

Model changes (`app/health/models.py`): add `schedule_mode: str` (default `"fixed"`) with
the matching `CheckConstraint`, and `taken_at: datetime | None` on `MedicationEvent`.

### B. Schedule arithmetic (lifestack-api)

`fixed` mode keeps `schedule.py::is_scheduled_date` / `get_dose_slots_for_date` /
`get_dose_slots_in_window` exactly as-is — a pure function of the rule, no event lookup.

`interval_from_last_dose` mode is **history-dependent**, so it lives in the service (which
has the event repository), with one new pure helper in `schedule.py`:

```
def interval_next_due_date(medication, last_event, tz) -> date | None:
    # No prior event → first dose sits on anchor_date.
    if last_event is None:
        return medication.anchor_date
    if last_event.status == "taken":
        base = (last_event.taken_at or last_event.scheduled_for).astimezone(tz).date()
    else:  # skipped: no intake to anchor to → advance off the slot it answered
        base = last_event.scheduled_for.astimezone(tz).date()
    due = base + timedelta(days=medication.interval)
    return due if (medication.end_date is None or due <= medication.end_date) else None
```

Key properties:
- **Exactly one live slot** per interval med: the next due date's `times`. If the med has
  never been dosed, that is `anchor_date`; otherwise `last_event_anchor + interval`.
- The single live slot is **sticky when overdue**: if `next_due_date` is already in the
  past and unanswered, it stays the live slot (deriving to `missed` after grace) until
  answered — it does not silently roll forward. Answering it (taken) re-anchors the *next*
  due off the intake day; skipping advances off the slot date. This is the "resume from
  when you actually take it" behaviour.
- End-date honoured: once the computed next due would exceed `end_date`, there is no slot.

### C. Service + API surface (lifestack-api)

- **`get_schedule(workspace_id, target)`** — for each active med: `fixed` → existing path;
  `interval_from_last_dose` → fetch the med's latest event, compute
  `interval_next_due_date`, and emit slot(s) **only if the due date == target**. Status
  derives via the existing `derive_slot_status` (taken/skipped from the answering event,
  else pending/missed by grace). This makes day-navigation Just Work for both modes.
- **`get_overdue_slots(workspace_id, lookback_days)`** *(new)* → unanswered, past-grace
  (`missed`) slots across active meds, newest-first, powering the "Catch up" section:
  - `fixed`: scan each date in `[today − lookback_days, today]` (med timezone), collect
    slots whose derived status is `missed`.
  - `interval_from_last_dose`: include the single live slot iff it is `missed` and within
    the lookback window.
  Returns `list[DoseSlot]` (same schema), so the web reuses the checklist component.
- **New route** `GET /health/medications/overdue?lookback_days=` (default from
  `HEALTH_CATCH_UP_LOOKBACK_DAYS`, `ge=1, le=30`), workspace-scoped, same auth as the
  schedule route.
- **`upsert_event`** — accept optional `taken_at`; when `status='taken'` and `taken_at`
  omitted, default to `datetime.now(UTC)`; when `status='skipped'`, force `taken_at=None`.
  Everything else (audit, uniqueness) unchanged; `taken_at` joins `_EVENT_AUDIT_FIELDS`.
- **Reminder job** — no logic change; it already iterates slots via the schedule helpers.
  For interval meds the job reads the single next slot (add an interval branch in the
  job's per-med slot lookup mirroring `get_schedule`, so a reminder fires for the next due
  dose). One push per slot, idempotent on `last_reminded_slot` — unchanged.
- **Schemas:** `MedicationBase`/`Update`/`Response` gain `schedule_mode`
  (`Literal["fixed","interval_from_last_dose"]`, default `"fixed"`), with a validator
  rejecting `interval_from_last_dose` unless `frequency=="daily"` and `days_of_week` is
  unset. `MedicationEventUpsert` gains `taken_at: datetime | None`; `DoseSlot` and
  `MedicationEventResponse` expose `taken_at` for display.

### C2. Weekly-summary correctness (lifestack-api)

`WeeklySummaryService._health_summary` counts `doses_scheduled` by walking each
day of the week and calling `get_dose_slots_for_date(med, day)` for every med —
the **fixed grid**. For an interval_from_last_dose med that grid is meaningless,
so it would corrupt `doses_scheduled`, and hence `doses_missed` and
`adherence_pct`. Fix: split meds by mode. Fixed meds keep the grid walk; interval
meds are counted event-derived via `_interval_scheduled_count` = (answered events
whose `scheduled_for` lands in the week) + (each med's single live due slot when
it falls in the week and isn't already answered). Because answered interval events
are also in the workspace taken/skipped totals, `missed = scheduled − taken −
skipped` still holds exactly (the answered term cancels, leaving the real misses).
No other summary/briefing/export field reads the grid for interval meds — exports
serialize columns dynamically (so `schedule_mode`/`taken_at` flow through with no
code change) and the briefing reuses the same schedule helpers.

### D. Config (lifestack-api)

Add `HEALTH_CATCH_UP_LOOKBACK_DAYS: int = 7` to `app/config.py` (env-overridable), the
default window for the overdue endpoint. No other flags.

### E. Health page (lifestack-web)

1. **Day navigation on the doses view.** Replace the hard-pinned `today` with a
   `selectedDate` state and a `‹ prev / date / next ›` control above the checklist.
   `getSchedule(selectedDate)` drives it; the mark-taken/skipped mutations invalidate the
   selected day. "Today" shortcut when not on today. This alone makes any past slot
   reachable.
2. **"Catch up" section** (above Today's doses, shown only when non-empty) — calls the new
   `getOverdue()` service method, renders missed slots with the existing `DoseChecklist`
   (each row labelled with its date), taken/skipped actions invalidate both the overdue
   query and the affected day's schedule.
3. **Schedule-mode control in the medication form** (`MedicationFormDialog`) — a small
   two-option selector: **Fixed schedule** (default) vs **Every N days from last dose**.
   Selecting the interval mode forces `frequency=daily` (hide weekly/monthly + weekday
   toggles), consistent with backend validation. The natural-language summary
   (`describeMedicationSchedule`) gains an interval-mode phrasing: *"Every 2 days from
   your last dose"*.
4. **Types/service/zod:** `schedule_mode` on `MedicationSchema`/`Create`/`Update`;
   `taken_at` on `DoseSlotSchema`/`MedicationEventResponseSchema`/`MedicationEventUpsert`;
   new `healthService.getOverdue(lookbackDays?)` parsing `z.array(DoseSlotSchema)`; new
   query key `queryKeys.health.overdue()`.

## Test plan (Red first, both repos)

**api unit:**
- `interval_next_due_date`: no events → anchor; taken event re-anchors off `taken_at`
  (late log shifts forward); skipped event advances off slot date; end_date cutoff → None;
  timezone boundary.
- `get_schedule` interval branch: only the due date yields a slot; overdue live slot
  derives `missed`; answering it advances the next due; day-navigation to a past fixed-mode
  slot returns it answerable.
- `get_overdue_slots`: fixed missed doses across the window collected newest-first; interval
  overdue slot included once; answered/pending/out-of-window excluded; workspace isolation.
- weekly `_health_summary`: an interval med taken mid-week counts its intake-based scheduled
  doses (Mon taken + re-anchored Wed due = 2), NOT the fixed-grid Mon/Wed/Fri/Sun = 4;
  `doses_missed`/`adherence_pct` follow; fixed-med counts unchanged (regression).
- schema validation matrix: `interval_from_last_dose` rejected for weekly/monthly and when
  `days_of_week` set; accepted for daily. `upsert_event` `taken_at` defaulting (taken →
  now when omitted; skipped → forced None).
- reminder job: interval med gets exactly one reminder for its next due slot; idempotent
  on re-run; fixed med behaviour unchanged (regression).
- migration: upgrade adds columns + backfills `taken_at=logged_at` for taken rows only;
  downgrade drops cleanly.

**web (Red first):** day-navigation changes the queried date and keeps marking working;
catch-up section renders overdue rows and hides when empty; schedule-mode selector forces
daily and round-trips; interval summary string; Zod parses new fields. Gates: vitest ≥ 70,
`npm run build`, lint.

**e2e (optional, if cheap):** extend `health.spec.ts` — create an every-2-days med, miss a
dose, catch it up from the Catch-up section; toggle a med to interval mode and confirm the
next due shifts after a late take.

## Rollout

Two PRs, api-first. **PR-1 (api):** migration, model/schema/service/schedule changes, new
overdue endpoint + config flag, reminder-job interval branch, tests, ARCHITECTURE/JOBS docs
touch if needed, this spec → `Implemented`. **PR-2 (web):** day nav + catch-up section +
schedule-mode selector + types/service, tests. No feature flag — additive; `fixed` is the
unchanged default so nothing shifts for existing data.

## Owner decisions (2026-07-24)

1. **Re-anchoring is opt-in per medication, `fixed` is the default.** Silent re-anchoring
   of a chronic schedule on a single late log is the wrong default (clinical-drift risk);
   the fixed grid matches dominant missed-dose guidance.
2. **Interval mode is daily-interval only.** Weekly/monthly re-anchoring is rejected.
3. **Only the next dose is materialised for interval meds**; the live slot is sticky when
   overdue and re-anchors off the *actual intake day* (`taken_at`) on a taken answer.
4. **Catch-up window default 7 days** (`HEALTH_CATCH_UP_LOOKBACK_DAYS`), env-overridable;
   day navigation covers anything older.
```
