# Spec-085: Weekly Summary & Analytics Coverage Status Reconciliation (api#183)

**Created:** 2026-07-18
**Status:** Implemented (items 1 and 3, this PR) — item 2 (zero-baseline swing) blocked on reproduction data, see Decision section; tracked back on api#183
**Depends on:** spec-076 (Weekly Summaries enhancements — `_net_worth_summary`/`_investing_summary`), spec-012 (Look-Through Exposure and Overlap Analytics — `ExposureAnalyticsService.exposure`)
**Scope:** `lifestack-api` only (`app/summaries/service.py`, `app/investing/service.py`, `app/summaries/schemas.py`). No schema/migration change. No frontend change required — the frontend already has correct fallback rendering gated on `status != 'complete'` for weekly summaries; this fix makes generation return that status when it should.

## Problem (github.com/sajankp/lifestack-api/issues/183)

Three related correctness issues surfaced by the 2026-07-16 UX review, all in status/coverage
reporting rather than the underlying values:

1. **Coverage is count-weighted, not value-weighted — misleadingly reads as "no data".**
   `ExposureAnalyticsService.exposure()` (`app/investing/service.py:1698-1700`) computes
   `coverage = decomposed / decomposable`, a ratio of the **number** of fund/ETF positions whose
   look-through resolved, with no relation to how much portfolio **value** those positions
   represent. A single small unresolved fund position can drag `coverage` to exactly `0` even
   when the overwhelming majority of the portfolio's value is stocks (never touch this ratio at
   all — they go straight to `direct`/`lookthrough`) and are fully resolved and already charted.
   The reviewer's report — "Coverage: 0 / Status: partial" shown next to charts with real data —
   is this: `analysis_status` is already consistent with the (flawed) count-based `coverage` in
   this case (both correctly flag "not fully resolved"), but the raw `0` reads as "nothing is
   covered" when in dollar terms almost everything is.
2. **Zero/stale boundary reported as `'complete'`.** `_net_worth_summary`/`_investing_summary`
   (`app/summaries/service.py:634-701`, `451-520`) select the most recent snapshot before
   `week_start` and on/before `week_end` as boundaries. If no new snapshot lands during a given
   week, the same snapshot row is returned as **both** the start and end boundary — the diff is
   trivially zero, but the code reports `status: "complete"` with a real-looking `week_change`/
   `week_change_pct`, rather than surfacing that no fresh measurement exists for that week.
3. **No staleness signal.** A stored `WeeklySummary` can be shown well after `generated_at` with
   no indication that a newer regeneration would produce a materially different figure.

## Decision — the part that needed maintainer judgment, and what actually held up

An existing test, `test_weekly_summary_net_worth_zero_baseline_stays_a_real_complete_summary`
(`app/tests/summaries/test_weekly_summary.py:566-616`), deliberately asserts the *opposite* of a
naive "zero baseline → unavailable" fix: a genuine zero-value snapshot (a user's first tracked
net worth, or a real wipe-out week) is legitimate data and must stay `"complete"`. A fix that
flags any zero boundary as suspect would regress that intentional behavior.

**First hypothesis (disproven by its own Red test — recorded here so it isn't retried):** that
`start_snapshot`/`end_snapshot` resolving to the identical row (no snapshot landed during the
target week at all) was slipping through as `"complete"`. A Red test built for exactly that shape
**passed against the unmodified code**, because it's structurally unreachable: `start_snapshot` is
selected via `snapshot_date < week_start`, so if `end_snapshot` is ever the same row, its date is
*also* `< week_start` — which the existing guard (`end_snapshot.snapshot_date < week_start` →
`"unavailable"`) already catches unconditionally. There is no code path where the two boundaries
share a row and the week still reports `"complete"`. No fix was applied for this because there is
nothing to fix here — the existing guard already handles it. (Caught before shipping only because
the Red-test-first discipline requires proving the failure before writing the fix — a test that
never fails proves nothing, per this repo's own change-control rule.)

**Where this leaves item 2:** the exact reproduction (a real `week_change_pct` swing described as
"(0.00%) one week, (−100.00%) the next") could not be reconstructed from the code alone — every
snapshot-boundary path traced (same-row reuse, stale carry-forward, genuine-zero) either already
returns `"unavailable"` correctly or matches the deliberately-defended `"complete"` zero-baseline
behavior. **This item needs the actual reproduction data (workspace, the two week ranges, and the
raw stored `net_worth_summary`/`investing_summary` JSON for both weeks) before a real fix can be
designed** — implementing a guess here risks either a no-op (like the disproven hypothesis above)
or a regression against the tested zero-baseline behavior. Filed back for that data rather than
guessed at further. Items 1 and 3 below do not depend on this and proceed independently.

For (1), `coverage` becomes value-weighted: `resolved_value / attempted_value`, where
`attempted_value` is the total value of every position that was successfully valued (all stocks
+ all fund/ETF positions considered for decomposition) and `resolved_value` is the subset of that
value that's fully accounted for (stock value, always resolved once valued, plus fund value whose
look-through actually decomposed). A portfolio that's 95% resolved stocks and one small
unresolved fund now reads as ~95% coverage instead of a count-based `0`— matching what the charts
actually show. `analysis_status` is then reconciled to agree with this corrected number:
`"complete"` only when coverage is full (== 1) **and** there are no other warnings; otherwise
`"partial"`.

For (3), add a lightweight, non-persisted staleness signal computed at read time: whether a
snapshot dated after `generated_at` now exists that would change the boundary computation — not
a stored field, no migration, purely a response-time comparison against the same repositories
already queried for the diff.

## Out of scope

- `snapshot_coverage` still measures only fund/ETF look-through resolution (never stock-position
  data quality issues like a missing company link, which surface as `warnings` instead) — only
  its weighting changes (count → value), not its subject matter.
- Any change to `NetWorthSnapshot`/`PortfolioSnapshot` creation logic — confirmed via
  `app/finance/service.py:1494-1526` that a snapshot is only ever written when its value is
  genuinely computable; no code path fabricates a zero for missing data.
- Retroactive correction of previously generated `WeeklySummary` rows — this changes status
  determination for future generation/regeneration only, consistent with the project's
  non-retroactivity convention for correctness fixes (spec-049/050 precedent).
- Frontend changes — none required; existing `status !== 'complete'` gating already renders the
  right fallback copy once the backend stops mislabeling these weeks.

## Validation

- Existing zero-baseline tests (`test_weekly_summary_net_worth_zero_baseline_stays_a_real_complete_summary`,
  `test_weekly_summary_investing_zero_baseline_stays_a_real_complete_summary`) must continue
  to pass unchanged — they exercise the distinct-rows case.
- New test for `ExposureAnalyticsService.exposure()`: a portfolio with a large resolved stock
  position and one small unresolved fund position must yield `coverage` close to (but under) `1`
  — not `0` — and `analysis_status == "partial"`.
- Full suite + `--cov-fail-under=80` per standard fix lifecycle.
