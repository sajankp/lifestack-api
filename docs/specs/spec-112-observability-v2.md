# Spec-112: Observability v2 — make slow queries visible and point the metrics at fixes

**Status:** Approved by the owner (2026-11-26); implemented on `feat/spec-112-observability-v2`, not yet merged or deployed
**Scope:** `lifestack-api` (in-process instrumentation, prod compose logging, `pg_stat_statements`) + `homelab-spec` (Grafana dashboard)
**Depends on:** spec-110 (production observability). Keeps its constraint: **nothing is added to the
production VM beyond in-process code.**

## Problem

spec-110 shipped and Prometheus has scraped prod since 2026-11-16. The data it collected shows real
latency problems, but cannot yet say *why*, and one of its signals is silently broken.

### Evidence (Prometheus, 7 days to 2026-11-26; `increase()` over the histogram `_sum`/`_count`)

| Route | Avg s/request | Requests |
|---|---|---|
| `/investing/performance/returns` | 19.9 | ≥15 |
| `/finance/net-worth` | 15.5 | 30 (9 of 30 took >30 s, the top bucket) |
| `/investing/summary` | 12.5 | ≥15 |
| `/finance/activity-feed` | 11.3 | 41 |
| `/dashboard/briefing`, `/investing/holdings`, `/spending/categories` | 8.2–8.7 | ≥15 |

- `lifestack_db_slow_queries_total` rose by **1,141** in 7 days (daily 9–399), yet the production DB is
  **28 MB**, so these are not large-table scans.
- Scheduler jobs are healthy (p95 ≤ 3.6 s, zero failures); 5xx ≈ 1 in 7 days. The problem is user-facing
  read latency, not failures.
- 9,622 of ~10,700 requests (≈92 %) are labelled `route="unmatched"` and account for 4,985 s. The MCP app is
  mounted at `/` (`app/main.py`, `_app.mount("/", _mcp_app)`), so requests it serves carry no `route` in the
  ASGI scope. MCP traffic is therefore invisible per tool. (Inferred from the mount and the 2xx count; not
  yet measured per path.)
- The VM has 954 MB RAM with 600–700 MB swap in use (spec-110 baseline, re-measured 2026-11-26).
  Swap thrash is a hypothesis for the slow queries, **not established**.

### Root cause of the missing slow-query log lines

Production sets `LOG_LEVEL=ERROR`. `setup_logging()` builds
`structlog.make_filtering_bound_logger(ERROR)`, which drops every `logger.warning(...)`, including
`slow_query` (`app/core/database/slow_query.py`). The counter increments before the logger call, so it
still counts. Verified on the VM: the container has 0 structlog JSON lines, and the earlier synced
`api.log` also has none. Result: we know *that* 1,141 queries were slow but never *which*.

## Goals

1. Slow-query statements appear in prod logs, and cannot go silently missing again.
2. Container logs cannot grow without bound (the prod `api` container uses `json-file` with no limits and
   there is no `daemon.json`).
3. Every slow request can be attributed: where its time goes (DB vs. non-DB), how many queries it ran, and
   whether the host was under memory pressure.
4. MCP traffic is visible per tool instead of one `unmatched` series.
5. A Grafana view that answers "which route, why, since when".

## Non-goals

Exporters or any new container on the VM (the `pg_stat_statements` extension runs inside the existing
Postgres); enabling OTel tracing; log shipping; alerting; fixing the slow endpoints themselves (that
follows from the data this spec produces, as separate specs/PRs).

## Design

### 1. Slow-query visibility (config first)

- **Prod config:** `LOG_LEVEL=WARNING` in `.env.production` (config only; no code path changes). Volume is
  not yet known: there are 46 `logger.warning` call sites and no JSON baseline. The rollout measures it
  (see Rollout).
- **Guard (small code change):** at startup, if `SLOW_QUERY_THRESHOLD_MS > 0` and `LOG_LEVEL` is above
  `WARNING`, emit one `error`-level event `slow_query_log_suppressed_by_log_level`. This turns the silent
  failure into a visible one. Test: `LOG_LEVEL=ERROR` + threshold 500 emits the event; `WARNING` does not.
- **Rotation:** `logging: {driver: json-file, options: {max-size: 10m, max-file: "3"}}` on every service in
  `docker-compose.prod.yml`. Applies on the next recreate.

### 2. Request attribution metrics (all in-process, bounded labels)

| Metric | Labels | Purpose |
|---|---|---|
| `lifestack_http_request_db_queries` (histogram) | `route` | queries per request — exposes N+1 |
| `lifestack_http_request_db_seconds` (histogram) | `route` | DB time per request; `duration − db` ≈ non-DB time |
| `lifestack_db_slow_queries_total` | add `route` | which routes produce slow queries (still no statement label) |
| `lifestack_db_pool_{size,checked_out,overflow}` (gauges) | none | pool saturation |
| `lifestack_host_memory_available_bytes`, `lifestack_host_swap_used_bytes`, `lifestack_host_memory_pressure_some_avg10` (gauges) | none | tests the swap-thrash hypothesis; read from `/proc/meminfo` and `/proc/pressure/memory` — **host** values are visible inside the container; PSI availability must be verified on the VM |

Mechanism: a `ContextVar` holder set by `StructlogMiddleware` per request; the existing
`before/after_cursor_execute` listeners add to it and to the slow-query counter's `route` label. The route
template is read from the shared ASGI scope (mutated in place by Starlette routing). Outside a request
(scheduler, CLI) the label is `none`. **To verify in implementation:** that the holder and `scope["route"]`
are visible from the SQLAlchemy listener under `BaseHTTPMiddleware` (the spec-110 note said "no safe route
context at the DB layer"; this must be proven by a test, not assumed).

### 3. MCP visibility

For requests served by the root-mounted MCP app, label `route` as `mcp` (path under `MCP_MOUNT_PATH`) and
add `lifestack_mcp_tool_duration_seconds{tool,status}` with the same bounded-label rule as the voice tool
histogram (`known_tools` passed in by the caller; anything else → `other`). Cardinality: current MCP tool
catalog size × 2 statuses.

### 4. `pg_stat_statements` (approved)

The Postgres service preloads `pg_stat_statements` (`max=1000`, `track=top`). `python -m app.cli.pg_top_queries`
prints the top statements by total execution time with their share, calls, mean/max time and rows. The
extension stores normalised text only, so the report holds no bind values (verified against a real
Postgres 18). The production `postgres` container sits behind the `local` compose profile and is not
touched by `deploy-production.sh`, so enabling it is a deliberate, documented step that restarts
Postgres for a few seconds (`docs/PRODUCTION_DEPLOYMENT.md` §6). Cost on the VM: shared memory for 1,000
statements (a few MB); measure after enabling.

### 5. Dashboard (homelab-spec)

Extend `configs/grafana/dashboards/lifestack-api.json` (generated by `build_lifestack_dashboard.py`; edit
the generator, not the JSON): avg and p95 by route with request counts (so a p95 from 3 requests is not
mistaken for a trend), DB queries/request, DB-time share, slow queries by route, pool, host memory/swap/PSI,
MCP tool latency.

## Rollout

1. PR to `lifestack-api` (spec first, then code, TDD, pre-commit + `--cov-fail-under=80`); dashboard PR/commit
   to `homelab-spec`.
2. **One** prod redeploy bundling `LOG_LEVEL`, compose logging and the code. The 2026-11-26 redeploy took
   ~40 min on the VM and `cloudflared` was down for a few minutes; batching avoids paying that twice.
3. After 24 h: count warning events per hour. If the volume is noise, set `LOG_LEVEL` back and move
   `slow_query` to its own handler (follow-up).
4. After 48 h: list the top slow statements and rank routes by DB share. Open separate specs for the top
   offenders. No performance fix is claimed by this spec.

## Acceptance

- `docker logs` on prod shows `slow_query` lines with duration and truncated statement, and no bind params.
- Starting with `LOG_LEVEL=ERROR` and threshold > 0 logs `slow_query_log_suppressed_by_log_level`.
- `/metrics` shows the new series with bounded labels (test asserts raw paths and unknown tool names never
  mint series — mutation-checked, as in spec-110).
- MCP requests no longer dominate `route="unmatched"`.
- Container logs rotate (`docker inspect` shows `max-size`).
- Added in-process memory cost measured on the VM (target: < 5 MB RSS; the VM has ~270 MB free).

## VM tuning applied out-of-band (2026-11-26, approved)

Done on the production VM ahead of the code redeploy, none of it needing application code:
- Docker build cache older than 72 h pruned, the unused `litellm` image removed (disk 72% → 40%).
- `journald` capped at 100 MB (`/etc/systemd/journald.conf.d/size.conf`; 835 MB → 71 MB).
- Postgres recreated with `pg_stat_statements` plus VM-sized settings (`docker-compose.prod.yml`,
  `postgres` service) and `CREATE EXTENSION pg_stat_statements` run. Container log rotation now applies
  to `postgres`; `api`, `cloudflared` and `database-backup` pick it up on the next deploy.
- Not done: zram, trimming idle host services, moving to an arm64 instance (separate decisions).

## Open questions (owner) — resolved 2026-11-26

1. **Log-level approach:** `LOG_LEVEL=WARNING` in prod, plus the startup guard. Approved.
2. **`pg_stat_statements`:** approved (reversed from the original recommendation to skip).
3. **Reading `/proc` for VM memory pressure:** approved.
4. **Alerting:** not requested; remains out of scope.
