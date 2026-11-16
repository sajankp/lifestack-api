# Spec-110: Production observability on a 1 GB VM

**Status:** Draft — implementation started on the owner's go-ahead; review before merge
**Scope:** `lifestack-api` (in-process instrumentation) + `homelab-spec` (Prometheus + Grafana on the homelab host)
**Depends on:** spec-082 (tracing/log export), spec-109 (voice telemetry)

## Problem

The 2026-11-15 voice investigation could not say *why* turns are slow: median tool time 3.5 s,
p90 22.8 s, max 65.5 s, but no per-tool or per-request timing exists. Today:

- per-request duration is computed in `StructlogMiddleware` but carries no route, and is not on
  stdout; `/metrics` exposes only scheduler job metrics; OpenTelemetry tracing is off in prod
  (`OTEL_EXPORTER_OTLP_ENDPOINT` unset); nothing scrapes or stores `/metrics`;
- there is no slow-query signal.

## Constraint: the production VM is memory-bound

Measured 2026-11-15: 954 MB RAM, ~270 MB available, 611 MB swap in use, 2 vCPU ~90 % idle, 16 GB
disk free; the whole stack uses ~95 MB, `dockerd`+`containerd` ~190 MB. **Nothing may be added to
the VM beyond in-process code.** Storage, scraping and dashboards run on the homelab host
(4 cores, ~9 GB available).

## Goals

1. Request latency by **route template** (never raw path): a Prometheus histogram on `/metrics`
   plus the route on the existing "Request finished" log line.
2. Slow-query log: any SQL statement above `SLOW_QUERY_THRESHOLD_MS` (default 500, `0` disables)
   is logged with a truncated statement **without bind parameters**, and counted.
3. Voice tool execution histogram by tool name (bounded label set).
4. Prometheus + Grafana on the homelab, scraping `https://lifestack-api.sajankp.com/metrics` with
   the bearer token held in a `0600` file; Grafana reachable on localhost/tailnet only.

## Non-goals

Prometheus/Grafana/Loki on the VM; enabling OpenTelemetry tracing in prod; log shipping (logs
are already pulled to `~/lifestack-logs`); alerting.

## Design

### Metrics (all on the default registry, served by the existing `/metrics`)

| Metric | Labels | Notes |
|---|---|---|
| `lifestack_http_request_duration_seconds` (histogram) | `method`, `route`, `status_class` | buckets 0.01 … 30 s; `/health` and `/metrics` excluded |
| `lifestack_db_slow_queries_total` (counter) | none | no route label: no safe route context at the DB layer |
| `lifestack_capture_tool_duration_seconds` (histogram) | `tool`, `status` | `tool` is limited to declared voice tools, anything else is `other` |

Cardinality rules: `route` is the matched FastAPI template or the literal `unmatched` (404/scanner
noise never mints series); `method` outside the standard set becomes `OTHER`; `status_class` is
`1xx`..`5xx`. Estimated series: ~150 routes × few methods × few status classes × 12 buckets,
well under 1 MB in process.

### Slow-query logging

SQLAlchemy `before/after_cursor_execute` events on the default engine. The log line carries
`duration_ms` and the statement text collapsed to one line and truncated to 300 chars. Bind
parameters are never logged (they hold amounts, names, tokens).

### Homelab

`prometheus` (pinned image, 30-day retention, 60 s scrape, `mem_limit` 384m) and `grafana`
(pinned, `mem_limit` 384m, bound to `127.0.0.1` and the tailnet interface), a provisioned
datasource and a starter dashboard (p50/p95 by route, slowest routes, slow queries, voice tool
p95, scheduler jobs). Secrets (metrics token, Grafana admin password) live in `0600` files,
never in the compose file or git. Images are pinned, so watchtower does not auto-update them.

## Test plan

API: middleware observes the route *template* (not the raw path), `unmatched` for 404s, `/health`
excluded; slow-query logging fires above threshold, is silent below it, never logs parameters,
`0` disables; tool histogram labels are bounded. Homelab: `docker compose config` validates;
Prometheus target is `up` against production `/metrics`; Grafana health 200.

## Deployment

API: additive, no migration; deploy with the next release (`deploy-production.sh`). Until then the
scrape returns scheduler metrics only. Homelab: additive services; rollback is
`docker compose rm -sf prometheus grafana`.
