# Spec-087: Redis Caching Layer for Expensive GET Aggregation Endpoints

**Created:** 2026-07-19
**Status:** Proposed — awaiting maintainer approval. No implementation code exists yet; this spec
gates a separate follow-up implementation PR per the multi-module change-control rule.
**Depends on:** `redis>=5.0.0` is **already a pinned dependency** (`pyproject.toml:28`) via the
testcontainers-backed test suite (`app/tests/conftest.py` uses `testcontainers.redis.RedisContainer`)
and SlowAPI's rate-limit storage (`RATE_LIMIT_STORAGE_URI`, `app/config.py:83`). There is currently
**no app-level Redis client wrapper** (no `app/core/redis.py` or similar) — the implementation PR
adds one. This is not a new-dependency discussion item.
**Scope:** `lifestack-api` only — `dashboard`, `finance` (net worth, FX), `summaries` (weekly)
modules. No frontend or e2e changes.

## 1. Problem statement

`PERFORMANCE_ANALYSIS_REPORT.md` §1.2 flags four GET aggregation endpoints that recompute
expensive queries on every request with no caching layer:

- `GET /v1/dashboard/summary` — aggregates todos, spending/budget totals, and investing
  performance on every call (`DashboardSummaryWorkflow.get_summary`,
  `app/application/workflows.py:128`).
- `GET /v1/finance/net-worth` — recomputes ledger + snapshot totals live on every call
  (`NetWorthService.get_net_worth` → `_compute_net_worth`, `app/finance/service.py:1494`).
- `GET /v1/finance/fx-rates` — reads persisted day-level FX rates on every call
  (`app/finance/router.py:354`).
- `GET /v1/summaries/weekly/latest` / `/{id}` — reads a pre-generated summary row, but pays
  request overhead on every dashboard load.

The report's §9 Measurement Plan cites **350ms p95 on `/dashboard/summary`** and a target of
`<100ms`. **That 350ms figure is explicitly marked "(est.)" in the report — it has not been
measured against production or a load test in this codebase.** This spec treats it as a
directional estimate motivating the work, not a validated baseline. The implementation PR should
capture a real p95 baseline (e.g. via the existing Prometheus histograms or a load test) before
and after, rather than citing the report's number as fact.

## 2. Why a spec (change-control classification)

This change touches three modules (`dashboard`, `finance`, `summaries`) and has a real
correctness risk — a caching layer that serves a stale read on a cash-correctness-sensitive
endpoint (net worth, dashboard) is exactly the failure mode `lifestack-cash-model-reference`
warns against ("it looks right on the dashboard" is not evidence). Per
`lifestack-change-control` §1, multi-module changes with correctness risk require an approved
spec before any implementation code. This document is that spec; **no application code is
included in or written by this PR.**

## 3. Design principle (load-bearing)

**A cache is a read-time performance layer only. It must never let a stale read appear
authoritative for a correctness check.** Concretely:

- Cached endpoints return the *same value* the uncached code path would compute, just serial-
  ized from Redis instead of recomputed — never a derived/approximated value. Any window where
  a cache entry is stale relative to a write that should have invalidated it is a bug to fix, not
  a feature to work around.
- **Never cache anything on the write path**, and never cache the reconciliation endpoint
  (`GET /finance/accounts/{id}/reconciliation`) — reconciliation exists specifically to prove
  correctness by comparing two independently-computed numbers; caching either side would let a
  proof run against a stale operand (see §6 Out of scope).
- Every cache key's invalidation triggers are tied to the actual write paths that can change the
  underlying data — not to a fixed TTL alone. TTL is a *backstop* (bounds worst-case staleness if
  an invalidation path is ever missed), never the primary correctness mechanism.
- `ENABLE_CACHE` must be able to kill the entire layer instantly, in any environment, with zero
  data migration (§7).

## 4. Scope: cached endpoints, TTLs, cache keys

| # | Endpoint | TTL | Cache key |
|---|---|---|---|
| 1 | `GET /v1/dashboard/summary` | 60s | `cache:v1:ws:{workspace_id}:dashboard:summary` |
| 2 | `GET /v1/finance/net-worth` | 5 min | `cache:v1:ws:{workspace_id}:finance:net-worth` |
| 3 | `GET /v1/finance/fx-rates?base=..&quote=..` | 1 hr | `cache:v1:fx:{BASE}:{QUOTE}` |
| 4 | `GET /v1/summaries/weekly/latest` | 24h (backstop; see §5.4) | `cache:v1:ws:{workspace_id}:summaries:weekly:latest` |
| 5 | `GET /v1/summaries/weekly/{summary_id}` | 24h (backstop) | `cache:v1:ws:{workspace_id}:summaries:weekly:{summary_id}` |

Key #3 is deliberately **not** workspace-scoped — see §8 for why, and the multi-tenancy guarantee
that applies to every *other* key.

## 5. Invalidation map (the crux)

Each row below traces the actual write path in the current codebase, not a guess.

### 5.1 `dashboard:summary` (key #1)

`DashboardSummaryWorkflow.get_summary` composes four sections: `todos`, `spending` (transactions
+ budgets), `investing` (via `PerformanceService`), `system`. Traced write paths that change
displayed values:

| Trigger | Where | Busts |
|---|---|---|
| Transaction create | `POST /spending/transactions` (`app/spending/router.py:413`) | `dashboard:summary` |
| Transaction update | `PATCH /spending/transactions/{id}` (`:440`) | `dashboard:summary` |
| Transaction delete | `DELETE /spending/transactions/{id}` (`:458`) | `dashboard:summary` |
| Investing order placement | `POST /investing/orders` (`app/investing/router.py:467`) | `dashboard:summary` |
| Investing order update | `PATCH /investing/orders/{id}` (`:564`) | `dashboard:summary` |
| Investing order delete | `DELETE /investing/orders/{id}` (`:545`) | `dashboard:summary` |

**Deliberately excluded:** capital transfer create/update/delete. Traced `DashboardSummaryWorkflow`
end to end — its `investing` section comes from `PerformanceService` (IRR/TWR/holdings
performance), and transfers do not feed that computation directly (transfers move cash between
accounts; performance is computed from orders and holdings). Budget and todo mutations are also
excluded — they're outside the write-path list this spec was scoped against, and the 60s TTL is
an accepted staleness backstop for that gap (a user changing a budget and immediately checking the
dashboard may see stale spotlight numbers for up to 60s — judged acceptable; **flagged explicitly
here rather than silently narrowed**, see §11 Q2).

### 5.2 `finance:net-worth` (key #2)

Traced `NetWorthService.get_net_worth` → `_compute_net_worth` (`app/finance/service.py:1494`):
it computes **live** from the ledger (spending accounts) and current snapshot rows (brokerage
cash), then opportunistically upserts *today's* `NetWorthSnapshot` row as a side effect — it does
**not** read that stored row back for the response. This matters:

| Trigger | Where | Busts | Why |
|---|---|---|---|
| Transaction create/update/delete | `app/spending/router.py` | `finance:net-worth` | ledger-managed spending accounts feed net worth directly |
| Capital transfer create/update/delete | `POST/PATCH/DELETE /finance/transfers` (`app/finance/router.py:438,456,474`) | `finance:net-worth` | transfers write both ledger rows and (for investing legs) snapshot rows that are inputs to `_compute_net_worth` |
| Investing order placement/update/delete | `app/investing/router.py` | `finance:net-worth` | orders move brokerage-cash snapshots (`snapshot_repo.delete_for_date` fires on every mutating investing route) which feed net worth |
| Manual cash-balance create/update/delete | `POST/PATCH/DELETE /investing/cash-balances` (`app/investing/router.py:169,191,218`) | `finance:net-worth` | the user's "match my statement" mechanism directly writes the snapshot row net worth reads |

**Deliberately excluded:** `net_worth_snapshot_job`. Traced it (`app/application/jobs.py:1003`) —
it materializes the **daily history** row (`NetWorthSnapshotRepository`), which is read by
`GET /finance/net-worth/history`, not by `GET /finance/net-worth` (the cached endpoint here). The
job's write is invisible to this cache key by construction; listing it as a trigger would be
theater, not correctness. `/finance/net-worth/history` is out of scope for this spec (§6).

### 5.3 `fx:{BASE}:{QUOTE}` (key #3)

| Trigger | Where | Busts |
|---|---|---|
| FX rate ingestion | `fx_rate_ingestion_job` (`app/application/jobs.py:684`) | the specific `{base}:{quote}` key(s) it just upserted |

The job already knows exactly which pairs it wrote in that run — invalidate precisely, per pair,
inline in the job, rather than a prefix `SCAN`/`KEYS` sweep (avoids an O(n) blocking Redis op in
production). The 1hr TTL is the backstop for any pair the job fails to touch (partial ingestion
failure, provider outage for one pair).

### 5.4 `summaries:weekly:*` (keys #4, #5)

| Trigger | Where | Busts |
|---|---|---|
| New summary generated | `weekly_summary_job` | `summaries:weekly:latest` |
| Explicit regeneration | `POST /summaries/weekly/{id}/regenerate` (`app/summaries/router.py:114`) | `summaries:weekly:latest` **and** `summaries:weekly:{id}` |
| Mark as read | `POST /summaries/weekly/{id}/read` (`:102`) | `summaries:weekly:{id}` **and** `summaries:weekly:latest` (if `{id}` is currently latest) — `read_at` is part of the cached response payload (`WeeklySummaryResponse.read_at`, `app/summaries/schemas.py:26`); serving a cached pre-read response after the user marks it read is a real, user-visible staleness bug, not a hypothetical one |

**Addition beyond the base four write-path categories (flagging explicitly, not silently
expanding scope):** import-batch revert (`ImportService.delete_batch`,
`app/imports/service.py:1186`) can change a weekly summary's `data_revised_after_snapshot` flag
(spec-086 Layer 2, computed at read time from an audit-log overlap check). Because this is a
single cheap key evict per workspace and the alternative is a genuinely wrong cached flag, this
spec recommends adding `delete_batch` as an invalidation trigger for `summaries:weekly:latest`
and any cached `{id}` overlapping the revert window. Flagged for maintainer sign-off in §11 Q4
rather than assumed.

## 6. Out of scope

- **List/detail endpoints with filters or pagination** (`/spending/transactions`,
  `/investing/orders`, `/investing/holdings`, `/finance/transfers`, etc.) — unbounded key
  cardinality from filter/pagination combinations; caching these is a different design problem
  (would need per-filter-hash keys and a much larger invalidation surface) and is not part of
  this slice.
- **`GET /finance/net-worth/history`** — variable date-range cardinality, plus interaction with
  the spec-086 `data_revised` per-point annotation (a cached historical point could go stale
  relative to a later import revert). Deferred; not one of the four endpoints named in this
  spec's scope.
- **`GET /finance/accounts/{id}/reconciliation`** — never cached. This endpoint's entire purpose
  is to prove two independently-computed numbers agree (lifestack-cash-model-reference §3); a
  cache would risk exactly the "stale read treated as authoritative" failure this spec's design
  principle (§3) forbids.
- **`GET /dashboard/briefing`** — not named in this spec's scope; candidate for a follow-up once
  the dashboard-summary cache is proven stable in production.
- **Any write path (POST/PATCH/PUT/DELETE)** — nothing on a mutation path is ever cached.
- **Investing analytics** (`/investing/performance/*`, `/investing/analytics/*`) — future
  candidate per the report's §1.5, not in this slice.
- **A new dependency or client library** — `redis>=5.0.0` already covers this; the implementation
  PR adds a thin `app/core/cache.py`-style wrapper using `redis.asyncio`, not a new package
  (no `fastapi-cache2`/`aiocache` — keeps the surface area small and consistent with how this
  codebase hand-rolls `ETagMiddleware` rather than pulling in a library for it).

## 7. `ENABLE_CACHE` configuration flag

**Recommendation: default `False` in all environments**, following the existing conservative-flag
pattern in `app/config.py` (`EMAIL_ENABLED: bool = False`, `SCHEDULER_ENABLED: bool = False`,
`REFERENCE_DATA_API_ENABLED: bool = False`) rather than the "on by default, opt out" pattern used
for `RATE_LIMIT_ENABLED`.

Justification: this is a new correctness-adjacent system touching cash-sensitive endpoints
(net worth, dashboard). Shipping it default-ON risks silently serving stale data in production on
day one if an invalidation path has a bug the test suite missed. Shipping default-OFF means:
- The implementation PR merges with zero behavior change in any environment.
- The maintainer enables it deliberately per environment (staging first, per §9) after the
  invalidation test matrix (§5, one Red→Green pair per trigger row) is green.
- If a staleness bug surfaces in production after enabling, `ENABLE_CACHE=false` + restart
  reverts to the current always-fresh behavior with **no data changes, no rollback migration,
  no redeploy of a different code version** — exactly the report's §8 risk-mitigation ask.

Follow the `RATE_LIMIT_ENABLED` precedent for a production guard: if `ENABLE_CACHE=true` in
`staging`/`production` but no cache Redis URL is configured, the config validator should raise
`ValueError` at boot (same pattern as `app/config.py:388-392`) rather than silently no-op.

## 8. Redis key naming scheme

- Prefix: `cache:v1:` — the `v1` segment is a cache-format version, not an API version; bumping it
  is the escape hatch to invalidate every cached entry at once after a payload-shape change,
  without needing a Redis `FLUSHDB` or a manual sweep.
- Workspace-scoped keys embed `ws:{workspace_id}` immediately after the version segment:
  `cache:v1:ws:{workspace_id}:<resource>:<qualifier>`. This is a hard rule for every key in this
  spec **except** FX rates (below) — workspace isolation must never depend on the caller
  remembering to filter; the key itself must make cross-workspace collision structurally
  impossible.
- **Exception, stated explicitly so it isn't mistaken for an oversight:** `cache:v1:fx:{BASE}:
  {QUOTE}` has no `ws:{workspace_id}` segment. Traced `GET /finance/fx-rates`
  (`app/finance/router.py:354-370`) — its own docstring states "FX rates are globally scoped
  system reference data (market data)," the endpoint takes no `workspace_id` dependency at all,
  and `fx_rates` (spec-011 §5.3) is explicitly workspace-agnostic reference data, unique on
  `(base, quote, as_of, source)` with no workspace column. Scoping this key by workspace would
  fragment a genuinely shared cache entry N-ways for no isolation benefit — there is no
  workspace-private data in an FX pair. Every other cached resource in this spec **is**
  workspace-owned data and **must** carry `ws:{workspace_id}`.
- Multi-tenancy guarantee: because `workspace_id` comes from `get_current_workspace_id` (the same
  dependency the read/write handlers themselves use for isolation), a cache-key builder that
  always takes `workspace_id` as its first argument for every non-FX resource makes it
  structurally impossible for handler code to build a key for the wrong workspace by omission.

## 9. Rollout / rollback plan

1. Merge the implementation PR with `ENABLE_CACHE=false` everywhere — zero behavior change on
   merge.
2. Implementation PR's test matrix: one Red→Green pair per row in §5's tables (assert cache
   populated on read, assert the specific write path busts the specific key, assert TTL as
   configured) — this is the "comprehensive invalidation tests" the report's §8 risk row asks for.
3. Enable `ENABLE_CACHE=true` in staging only. Run the golden scenarios (G1–G7,
   `lifestack-cash-model-reference` §8) end-to-end against the e2e stack with caching ON; assert
   every reconciliation/net-worth/dashboard output is byte-identical to a cache-OFF baseline run
   of the same scenarios.
4. Add three Prometheus metrics, consistent with the existing `scheduler_metrics.py` pattern:
   `cache_hit_total{key_type}`, `cache_miss_total{key_type}`, `cache_invalidation_total{key_type,
   trigger}`. Hit rate is the efficacy signal; invalidation-trigger counts are the correctness
   signal (a key type whose invalidation counter never fires despite its write paths being
   exercised is a red flag that a trigger is wired wrong).
5. Add a staleness canary: a low-frequency staging-only job that, for a sample of cached keys,
   compares the cached value to a forced fresh recompute and logs/alerts on any diff. This is the
   concrete mechanism for "detect staleness bugs before declaring it stable" — hit-rate metrics
   alone cannot detect a wrong-but-present cache entry.
6. Burn in staging for a defined period (recommend 1–2 weeks, covering at least one weekly-summary
   generation cycle) with the canary clean and hit rate stable before enabling in production.
7. Enable in production; keep the canary running against a production sample at low frequency;
   watch the same three metrics.
8. Rollback at any point: `ENABLE_CACHE=false` + restart. No migration, no data change, no code
   redeploy required — the uncached code path is the one that has run in production up to now and
   remains behind the flag, not deleted.

## 10. Correctness / retroactivity

This spec changes **read latency only**. It never changes what any number means, how it's
computed, or what gets written to the database. There is nothing to backfill and no historical row
is ever touched — every cached value is byte-identical to what the existing (uncached) code path
already returns; the cache only avoids recomputing it within its invalidation window.
`ENABLE_CACHE=false` at any time returns the system to its current behavior exactly, with no data
migration. This follows the same non-retroactivity posture as spec-049/050/086: a behavior change
here is forward-only and reversible, not something that requires reconciling past data.

## 11. Open questions

1. **Should `weekly:latest`/`weekly:{id}` be cached at all in this first cut?** Traced
   `WeeklySummaryService` — unlike dashboard/net-worth, the weekly-summary GET endpoints read a
   single pre-generated row with no expensive aggregation at read time (the aggregation happened
   once at generation/regeneration). The performance case for caching it is weaker than the other
   three. **Recommendation: include it in the spec (keys #4/#5) since the invalidation map is
   cheap and well-defined, but treat it as lowest priority in the implementation PR** — ship
   dashboard-summary and net-worth caching first, measure actual impact, and only build the
   weekly-summary cache if profiling shows it's part of a real cross-request cost (e.g. dashboard
   prefetching it on every load). Maintainer should confirm or cut this from scope.
2. **Dashboard-summary + budget/todo mutations:** §5.1 deliberately excludes budget and todo
   writes from the invalidation map, relying on the 60s TTL as the staleness bound. Confirm this
   is acceptable UX (a budget edit may not reflect on the dashboard for up to 60s) versus adding
   those triggers too.
3. **Cache-check vs. `ETagMiddleware` ordering:** `main.py` already wraps `dashboard`, `finance`,
   and `summaries` paths in `ETagMiddleware` (`main.py:263-276`). The implementation PR needs to
   decide whether the Redis cache check happens before or after ETag computation (cache the raw
   response body so ETag can still be computed from it, vs. cache the ETag alongside the body to
   skip re-hashing) — a design detail for that PR, flagged now so it isn't missed.
4. **Import-revert as a weekly-summary invalidation trigger (§5.4):** this was discovered by
   tracing spec-086's read-time annotation, not requested in the original write-path list.
   Recommend including it; maintainer sign-off requested since it's an addition beyond the
   originally scoped triggers.

## 12. Validation (for the implementation PR, not this spec)

Per `lifestack-change-control` §2.3-2.5: Red-before-Green for every row in §5's invalidation
tables; full suite + 80% coverage gate; `ruff check`/`ruff format` clean; golden scenarios G1–G7
re-run with caching ON per §9 step 3 before merge to main is even considered "stable" (not
required before merge — required before flipping `ENABLE_CACHE=true` past staging).
