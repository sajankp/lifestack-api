# Spec-089: Deterministic IST-Morning Daily Job Schedule (jitter removal + dependency ordering)

**Created:** 2026-07-19
**Status:** Implemented (2026-07-21)
**Depends on / supersedes:** the daily-job schedule + `register_daily_job_staggered` jitter added in
`a025a1a` ("stagger clustered daily jobs"). **Supersedes spec-088's digest/heartbeat times** (moved
earlier to match the new window — see §Interaction). Preserves the api#119 advisory-lock design.
**Scope:** `lifestack-api` only — `app/main.py` (lifespan job registration), `app/core/scheduler.py`
(jitter now optional/off for these), `app/config.py` (schedule-anchor setting defaults),
`.env.example`, and the scheduler registration test. No `lifestack-web` / `lifestack-e2e` changes.
**Source:** owner request (2026-07-19) — the app has one user, in India (IST = UTC+5:30). The current
schedule spreads daily jobs across 02:00–07:00 UTC (**07:30–12:30 IST**), so net worth, insights, and
the morning briefing land in the owner's *midday*, not ready for the morning. This spec repacks the
daily jobs into a deterministic **IST-early-morning window (03:00–05:30 IST = 21:30–00:00 UTC)**,
ordered by their real data dependencies, finishing as early as the one external constraint allows.

---

## Motivation & two problems with today's schedule

1. **Wrong time-of-day for a solo IST user.** Data the owner wants fresh on waking (net worth,
   dashboard insights, briefing) currently computes at 11:30–12:30 IST. It should be done before the
   morning.
2. **Jitter is actively harmful here.** `register_daily_job_staggered` adds ±60 min jitter, added
   (`a025a1a`) to spread DB contention across a busy cluster. On a **single-user 1 GB VM there is no
   such contention** — jobs run for seconds against tiny tables. Worse, the jitter **breaks a real
   ordering dependency**: `bhavcopy_price_feed_job` is documented to run *before*
   `investment_closing_prices_job` (bhavcopy is the preferred official NSE price source for INR
   holdings; Yahoo is only the fallback the closing-prices job uses for symbols bhavcopy missed —
   see `jobs.py:286-292`). Both currently sit at 02:00/02:30 UTC **with ±60min jitter**, so on any
   given day closing-prices can fire first and INR holdings silently get Yahoo prices instead of the
   official bhavcopy close. Deterministic fixed times fix this.

## The one hard constraint: US market close (the binding floor)

`investment_closing_prices_job` (`jobs.py:248`) values holdings at the **latest completed market
close** via Yahoo. The owner holds US stocks, so it depends on the **US regular-session close**:

- US close 16:00 ET → **20:00 UTC (EDT, Mar–Nov)** / **21:00 UTC (EST, Nov–Mar)**.
- Provider (Yahoo) EOD data settles within ~15–60 min of close → safe by ~**22:00 UTC worst case (EST)**.
- Scheduling this job at **23:00 UTC (04:30 IST)** gives a **~1 h margin past the worst-case (winter)
  settle**, year-round (no DST branching needed — we design to the later EST close).

Everything **downstream** of prices (`net_worth_snapshot` → `dashboard_insights` →
`morning_briefing` → the spec-088 `job_failure_digest`) must run *after* 23:00 UTC. Everything
**independent or upstream** (`fx_rate_ingestion`, `recurring_transactions`, `bhavcopy_price_feed`,
the cleanup jobs) runs before it. That fixes the earliest possible finish at **00:00 UTC = 05:30 IST**
(closing floor + five 15-min-spaced downstream stages). `bhavcopy` is *not* gated by the same-day
Indian close — it fetches the **previous** Indian trading day's bhavcopy (`_previous_weekday`,
available overnight), so it only needs to precede closing-prices, not wait for 15:30 IST.

## Dependency DAG (what forces the order)

```
fx_rate_ingestion ─┐                         (FX needed by valuation & multi-currency net worth)
                   ├─> investment_closing_prices ─> net_worth_snapshot ─> dashboard_insights ─┐
bhavcopy_price_feed┘   (bhavcopy MUST precede)      (needs prices+FX)      (needs net worth)   ├─> morning_briefing ─> job_failure_digest(088)
recurring_transactions ────────────────────────────────────────────────────────────────────┘   (needs everything)   (must be last)
export/session/import_preview cleanup ── independent, no gate
```

## Proposed schedule — no jitter, deterministic, 15-min grid

All times **fixed** (no jitter). IST = UTC + 5:30. In IST this is one contiguous 03:00–05:30 morning
window; in UTC it straddles midnight (21:30 → 00:00), which is irrelevant to execution — APScheduler
cron triggers fire at real-time instants 15 min apart regardless of civil date.

| # | Job | IST | UTC | Ordering reason |
|---|-----|-----|-----|-----------------|
| 1 | `export_cleanup` | 03:00 | 21:30 | independent (no gate) |
| 2 | `session_cleanup` | 03:15 | 21:45 | independent |
| 3 | `import_preview_cleanup` | 03:30 | 22:00 | independent |
| 4 | `fx_rate_ingestion` | 03:45 | 22:15 | first in chain — FX feeds valuation & net worth |
| 5 | `recurring_transactions` | 04:00 | 22:30 | new txns before balances are snapshotted |
| 6 | `bhavcopy_price_feed` | 04:15 | 22:45 | prior Indian close; **MUST precede closing-prices** |
| 7 | `investment_closing_prices` | **04:30** | **23:00** | **US-close floor** (~1 h past worst-case EST settle) |
| 8 | `net_worth_snapshot` | 04:45 | 23:15 | needs prices + FX |
| 9 | `dashboard_insights` | 05:00 | 23:30 | needs net worth / summaries |
| 10 | `morning_briefing` | 05:15 | 23:45 | needs everything above |
| 11 | `job_failure_digest` (spec-088) | 05:30 | 00:00 | must run last to capture all failures |
| — | `job_health_heartbeat` (spec-088) | Mon 05:45 | Mon 00:15 | weekly, after digest |

**Data is ready by ~05:30 IST** — before the owner's morning, and the whole run is deterministic.

### Unchanged (explicitly out of scope for retiming)

- **Interval jobs** — `budget_guardrails`, `kpi_guardrails` (every `BUDGET_GUARDRAILS_INTERVAL_HOURS`),
  `push_delivery` / `email_delivery` (1 min), `todo_reminder` / `medication_reminder` (5 min). These
  aren't time-of-day anchored; the 1-min `email_delivery` interval means the digest/heartbeat emails
  still flush promptly.
- **`weekly_summary`** — hourly cron at :30 UTC, cadence-gated; independent of this window.
- **`load_reference_securities_job` / `merge_company_identities_job`** — not registered as daily
  lifespan jobs; untouched.

## Interaction with spec-088 (supersedes its times)

Spec-088 originally set the digest at 04:00 UTC (09:30 IST) and heartbeat Mon 04:30 UTC, sized around
the *old* 02:00–07:00 UTC cluster. Under this spec the whole chain finishes by 23:45 UTC, so the
digest moves to **00:00 UTC (05:30 IST)** and the heartbeat to **Mon 00:15 UTC (05:45 IST)**. Spec-088
has been updated to these times with a pointer here. The digest remains **fixed (un-jittered)** and
strictly last, exactly as spec-088 requires (a jittered digest could fire before the jobs it reports).

## Config changes (`Settings` default updates)

| Setting | Old default | New default | Note |
|---|---|---|---|
| `RECURRING_TXN_GENERATION_HOUR` | `0` | `22` | + run at minute 30 → add `minute_utc=30` to the `register_daily_job` call (or a `RECURRING_TXN_GENERATION_MINUTE` setting; call-site arg is simpler). |
| `BRIEFING_JOB_HOUR_UTC` | `2` | `23` | |
| `BRIEFING_JOB_MINUTE_UTC` | `30` | `45` | |

The eight jobs currently registered via `register_daily_job_staggered` with hard-coded `hour_utc`
(fx, bhavcopy, closing, cleanups, insights, net-worth) get their new fixed UTC times inline and are
switched to **non-staggered** registration. Rather than delete `register_daily_job_staggered`, add an
opt-out (`jitter_minutes=0`) or call `register_daily_job`; the staggered helper stays available but is
no longer used by these.

## Out of scope

- Any change to job *logic*, the advisory-lock design, retry (spec-088), or interval-job cadences.
- Per-DST-season dynamic scheduling — we deliberately design to the worst-case (EST) US close and
  eat a harmless extra hour of margin in summer rather than add timezone logic.
- Making the schedule fully env-driven (each job an env var) — over-config for one operator; a few
  anchor settings stay configurable, the rest are sensible fixed defaults.

## Risks

| Risk | Mitigation |
|---|---|
| US close settles late (holiday-shortened session, provider delay) → closing-prices runs before data is ready | 1 h margin past worst-case settle absorbs normal variance; the job already no-ops gracefully on missing data and re-prices next run. Lever if it ever bites: push job #7 30 min later (all downstream shifts with it). Documented in the runbook. |
| Removing jitter reintroduces contention | None on a single-user 1 GB box — jobs are seconds-long against tiny tables and now run strictly sequentially 15 min apart (less concurrent than jittered overlap). |
| bhavcopy still races closing-prices | Fixed 22:45 vs 23:00 UTC with no jitter — deterministic 15-min ordering guarantee (the whole point). |
| A slow stage (LLM briefing) overruns its 15-min slot into the next job | Single-user stages complete in seconds–low-minutes; 15 min is ample. If briefing ever overruns, only the digest (a light ledger read) follows and is unaffected by briefing latency. |

## Testing plan (Red/Green, coverage gate 80%)

The scheduler is registered in `main.py` lifespan; assert **registration**, not wall-clock waits:
1. After startup, each retimed job is registered at its **exact fixed UTC trigger** (hour/minute) with
   **no jitter** (assert the trigger fields, not a range).
2. **Ordering guarantee:** `bhavcopy_price_feed` trigger is strictly earlier than
   `investment_closing_prices` (the regression this spec fixes) — and the full chain is monotonic
   (fx < closing < net_worth < insights < briefing < digest) in real-time order across the UTC-midnight
   boundary.
3. `investment_closing_prices` is registered no earlier than 23:00 UTC (the US-close floor guard).
4. Interval jobs and `weekly_summary` are unchanged (registration untouched).
5. Existing `test_scheduler.py` advisory-lock / single-connection assertions still pass (this spec
   changes *when* jobs run, never *how* — no behavior change inside any job).

## Rollout

- Single `chore:` change to `main.py` + `config.py` defaults + `.env.example`; no migration, no
  data touch.
- **Runbook update in the same pass** (CLAUDE.md rule): record the new IST-morning schedule table,
  the US-close floor rationale, and the "push closing-prices +30 min" lever in
  `lifestack-run-and-operate` + `lifestack-config-and-flags` domain memory.
- Deterministic + earlier means the owner sees fresh data by ~05:30 IST from the first day it ships.
