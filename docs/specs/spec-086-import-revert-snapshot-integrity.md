# Spec-086: Import-Revert Snapshot Integrity & Post-Snapshot Data-Change Provenance (api#183 item 2)

**Created:** 2026-07-18
**Status:** Layer 1 (same-day invalidation) landed as a fix; Layers 2–3 (provenance surfacing) approved (option 1). **Snapshot *restatement* evaluated and rejected as unsound — see "Why restatement is not viable" below.**
**Depends on:** spec-072 (net-worth backfill / snapshot model), spec-076 (weekly summaries), spec-085 (weekly-summary status reconciliation — the sibling api#183 work)
**Scope:** `lifestack-api` (Layer 1 + 2 + 3); a follow-up `lifestack-web` PR renders the Layer 2 warning and Layer 3 asterisk (separate repo, merged after api per one-PR-per-repo).

## Root cause (confirmed — this is the api#183 item 2 mechanism spec-085 could not reproduce)

The owner identified it: **data was imported, a snapshot captured it, then the import was reverted.**

Traced in code:
- Deleting/reverting a completed import batch (`ImportService.delete_batch`, `app/imports/service.py:1186`)
  unwinds the imported holdings/orders/cash/transfers/transactions, but **never touches
  `NetWorthSnapshot`/`PortfolioSnapshot` rows**. Every *interactive* mutation path invalidates the
  current-day snapshot (`snapshot_repo.delete_for_date(workspace_id, today)` — `app/investing/router.py`
  in ~10 places, `app/investing/service.py:297`, `app/investing/performance_service.py:179,199`); the
  import-rollback path was never wired into that invalidation.
- Snapshots are date-keyed upserts written once/day by `net_worth_snapshot_job`
  (`app/application/jobs.py:1003`, daily) and by opportunistic recompute-on-read
  (`get_net_worth`, `app/finance/service.py:1494-1506`).
- So a snapshot taken while the (later-reverted) import's data was live stays in the table as a
  permanent historical row. The weekly summary's `_net_worth_summary`/`_investing_summary`
  (`app/summaries/service.py:451-520,634-701`) diff the latest snapshot `< week_start` against the
  latest `<= week_end` with no notion of a post-hoc revert, so a genuine later week gets diffed
  against a stale pre-revert boundary — producing the unrealistic swing (e.g. 0.00% → −100.00%).

Note (nuance): the **same-day** case largely self-heals, because `get_net_worth`/portfolio-summary
recompute and re-upsert *today's* snapshot on read. The durable damage is a **past-day** snapshot
(revert after the day rolled over), which by policy we must NOT retroactively mutate.

## Why restatement is not viable (settled — do not reopen)

The tempting "correct" fix is to *recompute* the corrupted past snapshot to its true value after
the revert, logging before/after to the append-only `audit_logs` (which would satisfy the
provenance objection). We evaluated this and it is **unsound by construction**, because a past
net-worth/portfolio snapshot **cannot be faithfully recomputed** — both inputs are unrecoverable:

1. **Historical quantities are not reconstructable.** `PerformanceService.create_snapshot(date)`
   values *current* holdings (`holding_repo.get_all()`, `performance_service.py:202`) at as-of-date
   prices — it does not reconstruct share counts as of a past date. There is no date-bounded order
   replay (`_replay_orders` only rebuilds current state), and holdings created without orders
   (holdings-CSV import, `source_type` defaults to `manual`/`imported`) have no order history to
   replay at all.
2. **Historical market prices are not retained.** Price capture is forward-only:
   `investment_closing_prices_job` / `bhavcopy_price_feed_job` (`jobs.py:248,286`) write only the
   current expected close each day. `latest_prices_on_or_before(D)` for a past D returns a stale
   price or nothing (→ cost-basis fallback), never the true day-D market price.

**Conclusion:** the daily snapshot is a **primary observation**, not a recomputable cache — it is
the only faithful record that will ever exist of the point-in-time valuation, precisely because the
inputs to recompute it are gone. Therefore:
- Non-retroactivity for `net_worth_snapshots` / `portfolio_snapshots` is **forced by the data
  model**, not a relaxable simplification. (This is stronger than the general spec-049/050
  forward-only policy — here recomputation is not merely disallowed, it is impossible.)
- Restatement would substitute fabricated numbers (wrong quantities × stale/absent prices) for a
  real observation — strictly worse than preserving it. Rejected.
- **Annotation is the correct answer, not a compromise:** preserve the observation, flag that it
  includes data later reverted. The "correct" value is genuinely unknowable, so surfacing the
  caveat is the most truthful thing the system can do.

(The append-only `audit_logs` trail is still used — as the *provenance source* for the annotation
below, not to justify mutating a snapshot.)

## Design — three layers

### Layer 1 — invalidate the current-day snapshot on import revert (a FIX)
Wire the same `delete_for_date(workspace_id, today)` the interactive endpoints use into
`delete_batch`, for both `NetWorthSnapshot` and `PortfolioSnapshot`, whenever a rollback actually
removed snapshot-affecting data. This closes the same-day gap for the read paths that don't
recompute (history, weekly summary). Consistency hygiene; does not address past-day reverts.

### Layer 2 — weekly-summary "data changed after snapshot" warning (a FEATURE, needs approval)
Because we must not mutate historical snapshots, we *annotate* instead — and we need **no new
table**: `delete_batch` already writes a permanent `import_rolled_back` audit entry
(`app/core/audit.py` `AuditLog`, append-only, enforced by a DB trigger that blocks update/delete —
`app/tests/test_audit_logging.py:214`).

- Add the batch's `committed_at` to that audit entry's `details` so the window
  `[committed_at, reverted_at(=audit.timestamp)]` during which the reverted data was live is
  queryable. (Small change to `delete_batch`.)
- At weekly-summary read (`GET /summaries/weekly/latest`, `/{id}`), if any `import_rolled_back`
  live-window overlaps the snapshot dates the report's net-worth/investing sections depend on
  (`start_snapshot_date`/`end_snapshot_date`), set a new response field, e.g.
  `data_revised_after_snapshot: true` with a short human detail. Read-time only, no stored column,
  no snapshot mutation.
- This composes with spec-085's `data_stale` (fresher snapshot exists) but is a distinct signal
  (underlying data for the *existing* snapshot was reverted).

### Layer 3 — daily net-worth history "*" note (a FEATURE, needs approval)
Same overlap check on the net-worth history endpoint → a per-point boolean flag (e.g.
`data_revised: true`) the web renders as the asterisk/footnote the owner asked for. Read-time only.

## Retroactivity / policy

- No historical snapshot row is ever mutated or deleted for a past date (Layer 2/3 annotate at read
  time from the append-only audit trail). Consistent with spec-049/050 non-retroactivity.
- Layer 1 only deletes the **current-day** snapshot (already the sanctioned pattern for interactive
  edits) — a same-day row is not yet "history".

## Out of scope
- Correcting/recomputing past-day snapshots (deliberately: policy).
- Linking a snapshot to the specific source rows that produced it (snapshots store totals only; the
  audit-window overlap is intentionally coarse — a safe over-flag, never a silent miss).
- The web rendering of the Layer 2 warning / Layer 3 asterisk (separate lifestack-web PR).

## Validation
- Layer 1 Red test: commit an import that affects net worth, materialize today's
  `NetWorthSnapshot`/`PortfolioSnapshot`, revert the import → assert today's snapshots are gone
  (invalidated). Fails on current code, passes after wiring `delete_for_date`.
- Layer 2 Red test (pending approval): a reverted import whose live-window covers a summary's
  boundary snapshot dates → `data_revised_after_snapshot` true; a revert outside the window → false.
- Layer 3 Red test (pending approval): net-worth history point inside a revert window flagged,
  outside not.
- Full suite + `--cov-fail-under=80`; ruff.
