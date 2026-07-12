# Spec-076: Weekly Summaries — Cadence, Regeneration, Expanded Insights

**Created:** 2026-07-12
**Status:** Approved (implementation) — open questions resolved by owner 2026-07-12
**Depends on:** spec-013 (weekly summaries), spec-067 (morning briefing — consumes a "fresh" weekly summary Mondays), spec-058 (dashboard insights), spec-069 (health memory — `_health_summary` already exists in `WeeklySummaryService`)
**Scope:** multi-repo, user-facing — `lifestack-api` (cadence config, regenerate endpoint, new sections) + `lifestack-web` (settings, regenerate affordance, new section rendering).

## Problem

Sequence #3. Three limitations of the implemented weekly summary:

1. **Fixed cadence** — one cron (Mon 01:30 UTC) for every workspace; no per-workspace day/time and
   no monthly variant.
2. **No correction path** — a summary generated before a data fix (late import, corrected
   transaction, backfilled dividend) stays wrong forever; there is no regenerate flow, so the
   permanent record contradicts the corrected books.
3. **Insight coverage lags the product** — sections cover spending/investing/health-v1, but the
   2026-07 wave shipped dividends (spec-073), return metrics/XIRR (spec-071), and net-worth
   history (spec-065) which the summary does not yet surface.

## Solution

1. **Per-workspace cadence setting** (`weekly` only in v1; day-of-week + hour; `monthly` deferred
   per resolved Q3) —
   job scaffolding stays a single cron tick that selects workspaces due (same advisory-lock,
   one-connection pattern as api#119; see PATTERNS.md Scheduled Jobs before touching).
2. **Regeneration** — `POST /v1/summaries/{public_id}/regenerate` recomputes the same period from
   current data. Versioned, not destructive: the superseded record is retained
   (`superseded_by_id`), the new one carries `regenerated_at` + `regeneration_reason`. Summaries
   are derived state — regeneration is the replay-determinism principle applied to summaries, and
   it must NOT re-trigger notifications/push (the briefing consumed the original; a regenerate is
   a bookkeeping correction, not a new event).
3. **New sections** — dividend income received in period (spec-073 events), net-worth change with
   as-of provenance (spec-065 snapshots), notable return-metric moves (spec-071). Each section
   follows the existing deterministic, source-linked composition rule (zero LLM, like spec-067).

## Backend / API / schema impact

- `weekly_summaries`: add `superseded_by_id` (nullable self-FK), `regenerated_at`,
  `regeneration_reason`; workspace-scoped composite FK as always. Migration with working
  downgrade; enum-inline pattern if any new enum.
- New settings fields for cadence (workspace-scoped); listing endpoints default to
  latest-non-superseded.

## Out of scope

- Email delivery of summaries (Notifications is parked — owner decision 2026-07-12).
- LLM-generated narrative text (deterministic composition stays the rule).
- Backfilling summaries for weeks before the feature existed.

## Resolved questions (owner, 2026-07-12)

1. Retention: **keep every superseded version** (no cap).
2. Briefing reads **latest-non-superseded at compose time**; already-sent briefings are never
   retroactively edited.
3. Monthly cadence: **deferred** — v1 ships day/hour configurability only (drop `monthly` from
   the v1 cadence options in Solution 1).
4. Regeneration: **manual-only** in v1.
