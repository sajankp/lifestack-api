# Spec-080: Weekly-Summary Read State (dismiss the "summary is ready" briefing line)

**Created:** 2026-07-13
**Status:** Approved (owner, 2026-07-13) — api implemented; web dismiss UI pending
**Depends on:** spec-067 (Morning Briefing), spec-076 (Weekly Summaries enhancements)
**Scope:** multi-repo, user-facing — `lifestack-api` (`app/summaries/`, `app/application/workflows.py`, one Alembic migration) + `lifestack-web` (`WeeklySummariesPage`, `summaries` service, briefing refetch).

## Problem

The Morning Briefing shows a line — *"Weekly summary for week of YYYY-MM-DD is ready"* —
whenever the latest `WeeklySummary` is younger than `_BRIEFING_WEEKLY_SUMMARY_FRESH_HOURS`
(48h) (`app/application/workflows.py:600-617`). It is purely age-gated: **there is no
read/seen/viewed state on the model** (`app/summaries/models.py:8-29` has only `week_start`,
`week_end`, `generated_at`, the summary blobs, `created_at`). So once the user has opened
`/summaries` and read the summary, the line keeps showing for the full 48-hour window — a
"you have unread mail" nudge that never clears when you read the mail. Observed by the owner:
summary read hours ago, line still present.

This is the same class as the dose-due-today bug (a resolved item still nudging), but unlike
that one it cannot be fixed by reinterpreting existing data — there is no signal recording that
the summary was seen. It needs a new stored field, hence a spec.

## Solution

Record when a summary is read, and suppress the briefing line once it is (still also bounded by
the existing 48h freshness window — a read summary is hidden; an unread one older than 48h is
still hidden).

### Data model (the decision that needs approval)

**Recommended — a nullable `read_at` timestamp on `weekly_summaries`.**
- Matches the product's single-active-user-per-workspace reality; no new table.
- `read_at: datetime | None`, `NULL` = unread. Set to `now()` on first read; idempotent
  (second read is a no-op, does not move the timestamp).

**Alternative (rejected for now, documented) — a per-user `weekly_summary_reads` join table**
(`(summary_id, user_id, read_at)`). Correct if a workspace ever has multiple human members who
each read independently. Rejected because the briefing is already effectively single-reader and
this adds a table + join for no current benefit. **Out-of-scope note below records the limitation.**

### API

- Migration: add nullable `read_at TIMESTAMPTZ` to `weekly_summaries`. Working `downgrade()`
  drops the column. No enum, no backfill (existing rows are `NULL` = unread — acceptable, they
  age out of the 48h window on their own).
- New endpoint `POST /v1/summaries/weekly/{summary_id}/read` → sets `read_at = now()` if currently
  `NULL`, returns the updated `WeeklySummaryResponse`. Workspace-scoped like the existing GETs.
  404 on unknown/other-workspace id (reuse `by_public_id`).
- `WeeklySummaryResponse` gains `read_at: datetime | None` (frontend uses it to avoid a redundant
  mark-read call and could show a read/unread affordance later).

### Briefing change

`_weekly_summary_lines` (`app/application/workflows.py:600-617`): after the freshness check,
also return `[]` when `latest.read_at is not None`. One added condition; the 48h window stays.

### Frontend

- `summaries.ts`: add `markRead(summaryId)` → `POST .../read`.
- `WeeklySummariesPage`: when a summary's detail is opened/expanded and its `read_at` is null,
  fire `markRead` once (mutation), then invalidate the briefing query so the dashboard line
  clears on next view. Reading is the natural trigger — no extra "mark read" button.

## Testing (TDD, Red first)

- **api**: Red test — briefing omits the weekly-summary line when `latest.read_at` is set (fresh
  but read). Green — the added condition. Plus: `POST /read` sets `read_at`, is idempotent, and is
  workspace-scoped (404 cross-workspace). Migration up/down covered by the existing migration test
  pattern.
- **web**: Red — opening a summary whose `read_at` is null calls `markRead` and invalidates the
  briefing query; an already-read summary does not re-call. MSW-mocked.

## Out of scope

- **Per-user read state.** With `read_at` on the row, if two members share a workspace, one
  reading clears the line for both. Accepted given single-active-user usage; revisiting requires
  the join-table alternative above and its own spec.
- **Unread badges / counts elsewhere** in the UI — this spec only clears the briefing line.
- **Retroactivity**: none needed. `read_at` is forward-only; pre-migration summaries are `NULL`
  (unread) and simply age out of the 48h window as they already do.
- Auto-marking read from anywhere other than actually opening the summary detail (e.g. marking
  read just because the dashboard rendered the line) — explicitly not done; that would defeat the
  purpose.
