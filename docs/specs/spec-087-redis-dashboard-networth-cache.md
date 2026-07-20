# Spec-087: Redis Cache for Dashboard Summary & Net Worth

**Created:** 2026-07-19
**Status:** Approved (2026-07-19) — not yet implemented
**Depends on:** none (additive). Related: spec-086 (net-worth snapshot upsert-on-read semantics — see "Interaction with snapshot upsert" below)
**Scope:** `lifestack-api` only — `app/core/cache.py` (new), `app/dashboard/router.py`, `app/finance/router.py`, `app/config.py`, `.env.example`. No `lifestack-web` or `lifestack-e2e` changes required.
**Source:** `PERFORMANCE_ANALYSIS_REPORT.md` §1.2 ("No API-level caching") and §5 Quick Win #7 ("Cache dashboard summary in Redis (TTL 60s) — -200ms p95 on `/dashboard/summary`")

---

## Problem

`GET /dashboard/summary` and `GET /finance/net-worth` recompute their full aggregation on every
request — todos + spending category totals + budgets + investing performance for the dashboard;
holdings valuation + cash balances + FX conversion for net worth. Both are read-heavy, expensive,
and change slowly relative to how often a personal-finance UI polls/re-renders them. The existing
`ETagMiddleware` (`app/core/etag.py`) only saves *bandwidth* on a 304 — it hashes the response body
*after* the endpoint has already done the full computation, so it doesn't help backend load at all.

Redis is already provisioned (`docker-compose.yml` `redis` service, `redis>=5.0.0` in
`pyproject.toml`, `RedisContainer` fixture already wired into `app/tests/conftest.py` for
rate-limit tests) but is not used for anything except SlowAPI's rate-limit storage. This spec adds
a second, independent use: a small read-through cache for these two endpoints.

## Design

### Scope: TTL-only, no active invalidation (key decision — see "Open decision" below)

The report's recommendation ("TTL 60s, invalidated on transaction create/update") implies wiring
invalidation into every mutation that could affect these two aggregates: spending transactions,
budgets, capital transfers, investing orders, dividends, cash-balance edits, corporate actions,
imports, todo completions (dashboard only). That's a lot of call sites across modules that — per
the architecture contract — **must not import each other**; the only way to invalidate from all of
them without violating that boundary would be routing every mutation through
`app/application/workflows.py`, which most of them don't do today. That's a much bigger, higher-risk
change than "add a cache."

This spec proposes **TTL-only caching, no invalidation hooks**. A personal-finance dashboard being
up to `N` seconds stale after an edit is imperceptible in practice (the user made the edit, sees
their own transaction list update instantly since that endpoint isn't cached — only the aggregate
summary lags), and it keeps the change additive and isolated to two GET routes with zero touch
points in any mutation path.

- `GET /dashboard/summary` → TTL **30s**
- `GET /finance/net-worth` → TTL **120s**

(Both configurable via env; see Config below.)

### Where the cache lives

New module `app/core/cache.py` (cross-cutting infra, same tier as `app/core/etag.py`), NOT
`app/application/` — this is a pure infra concern, not cross-module business orchestration:

```python
class ResponseCache:
    def __init__(self, redis_url: str, enabled: bool): ...
    async def get_json(self, key: str) -> dict | None: ...
    async def set_json(self, key: str, value: dict, ttl_seconds: int) -> None: ...
```

- Backed by `redis.asyncio.Redis.from_url(settings.REDIS_URL)` — a new dedicated async client,
  not reused from SlowAPI's limiter (that client is internal to the `limits` library's storage
  backend and isn't exposed for arbitrary get/set).
- Keys are namespaced and workspace-scoped (invariant #1 — every business-data read is
  workspace-scoped): `cache:v1:dashboard:summary:{workspace_id}`,
  `cache:v1:finance:net-worth:{workspace_id}`. Same Redis DB as rate limiting (`REDIS_URL`,
  currently unused despite being set in `docker-compose.yml` since 2026-xx) — no key collision risk
  since the `limits` library's keys have its own opaque prefix.
- Value stored is the endpoint's `response_model.model_dump(mode="json")` as a JSON string — plain
  cache-aside, not raw response bytes, so `response_model` validation still runs uniformly on both
  hit and miss paths (negligible cost, no DB/compute involved).
- **Fail open, always.** Any Redis error (connection refused, timeout) on get or set is caught,
  logged once per occurrence at `warning` (not `error` — this is not a production incident), and
  treated as a cache miss / no-op. Caching is strictly an optimization layer; it must never become
  a new hard dependency for these two endpoints. This matches "keep the demo path working at all
  times" — if Redis is down, dashboard/net-worth just get slower, not broken.

### Router changes

Both routes get a thin cache-aside wrapper — no changes to `DashboardSummaryWorkflow` or
`NetWorthService` themselves:

```python
@router.get("/summary", response_model=DashboardSummary)
async def get_summary(
    dashboard_workflow: Annotated[DashboardSummaryWorkflow, Depends(get_dashboard_summary_workflow)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    cache: Annotated[ResponseCache, Depends(get_response_cache)],
):
    key = f"cache:v1:dashboard:summary:{workspace_id}"
    if cached := await cache.get_json(key):
        return cached
    result = await dashboard_workflow.get_summary(workspace_id)
    await cache.set_json(key, result.model_dump(mode="json"), ttl_seconds=settings.DASHBOARD_CACHE_TTL_SECONDS)
    return result
```

Same pattern for `GET /finance/net-worth`. Neither route takes query parameters that affect the
response today (verified: both depend only on `workspace_id`), so `workspace_id` alone is a
sufficient cache key — no risk of serving one workspace's data shaped by another's query params.

### Config (new `Settings` fields in `app/config.py`)

| Field | Default | Notes |
|---|---|---|
| `ENABLE_RESPONSE_CACHE` | `False` | Master switch. Off in local/test by default (tests assert live behavior unless explicitly opted in via the existing `redis_container` fixture). Recommend `True` in staging/production. |
| `REDIS_URL` | `redis://localhost:6379/1` | Not currently a `Settings` field — `docker-compose.yml` sets the env var already but no code reads it. This spec adds the field. |
| `DASHBOARD_CACHE_TTL_SECONDS` | `30` | |
| `NET_WORTH_CACHE_TTL_SECONDS` | `120` | |

No production-validator hard-requirement (unlike `RATE_LIMIT_STORAGE_URI`) — because of fail-open,
there's no insecure-default class of bug here to guard against; worst case with cache disabled or
misconfigured is "no speedup," not a correctness or security issue.

### Interaction with snapshot upsert (spec-086 context)

`NetWorthService.get_net_worth` has a side effect: it opportunistically upserts today's
`NetWorthSnapshot` on every call (this is the "same-day self-heals" mechanism spec-086 documents).
Caching means that upsert now happens at most once per `NET_WORTH_CACHE_TTL_SECONDS` window instead
of on every request. This doesn't weaken the self-heal invariant — it still runs same-day, just not
on every single hit — and arguably reduces redundant same-row upserts. Calling this out explicitly
since it's the one place this spec touches behavior spec-086 depended on; not treated as a risk.

## Out of scope

- Weekly summary, briefing, and FX-rate caching (report also recommends these — separate spec if
  pursued; briefing in particular is user-scoped, not just workspace-scoped, and briefing's LLM
  cost profile is different enough to warrant its own TTL/invalidation discussion).
- Any invalidation-on-write mechanism (see "Scope" above).
- Materialized views / SQL-level pre-aggregation (report §1.5, §4.2 — separate, larger effort).
- Read replicas, prepared-statement/pool changes (already resolved per the report).

## Risks

| Risk | Mitigation |
|---|---|
| Stale data shown for up to TTL after an edit | TTL kept short (30s/120s); acceptable per "Scope" rationale above. Documented behavior, not a bug. |
| Redis unavailable in prod | Fail-open on every get/set; endpoint falls back to live compute. No new hard dependency. |
| Cache key leaks across workspaces | Key always includes `workspace_id`; no shared/global key used. |
| New dependency concern (change-control policy: "no new dependency without discussion") | `redis` package already in `pyproject.toml` (used by SlowAPI + test fixtures) — no new dependency being added, only a new dedicated client instance. |

## Testing plan (Red/Green, coverage gate 80% applies)

Using the existing `redis_container` fixture (`app/tests/conftest.py`) — same testcontainer already
used for rate-limit tests:

1. Cache miss: first request computes live, stores in Redis, response matches direct
   workflow/service call.
2. Cache hit: second request within TTL returns cached value; assert the underlying
   workflow/service method was **not** called again (mock/spy).
3. TTL expiry: set a short TTL in the test, wait past it (or use Redis `TTL`/manual `EXPIRE`
   inspection instead of a real sleep), confirm recompute happens.
4. Workspace isolation: two workspaces hitting the same route get independent cache entries.
5. Fail-open: point `REDIS_URL` at an unreachable address, confirm the endpoint still returns
   200 with a correct live-computed body (no exception surfaced to the client).
6. `ENABLE_RESPONSE_CACHE=False`: confirm every request computes live (no caching behavior at all).

## Rollout

- Merge with `ENABLE_RESPONSE_CACHE` defaulting to `False` everywhere.
- Owner flips it on in staging first via env var, observes for a few days, then production —
  no code change needed to toggle.
- No runbook update needed beyond documenting the two new env vars in `lifestack-config-and-flags`
  domain memory (done in the same PR per CLAUDE.md's "runbook in the same pass" rule, scoped here
  as: this is a new config axis, not a runtime/deployment fix, but the config skill should still
  list it).
