# Spec-082: OpenTelemetry Traces and Logs to PostHog

**Created:** 2026-07-14
**Status:** Approved (owner, 2026-07-14) — implement only after spec-081 ships
**Depends on:** spec-081 (PostHog project, key handling, privacy rules — must ship first)

## Problem

The API has structured logging (structlog) and *partial* tracing scaffolding
that exports nothing: `opentelemetry-api`/`-sdk` and the FastAPI instrumentor
are already dependencies, `OTEL_EXPORTER_OTLP_ENDPOINT` already exists in
`Settings`, and `app/main.py` calls `FastAPIInstrumentor.instrument_app()`
when that setting is present — but no `TracerProvider` or OTLP exporter is
ever configured, so spans hit the default no-op provider and are dropped.
There is no off-box log aggregation either: latency questions ("which
endpoint is slow, and is it the DB or the FX call?") and post-incident log
searches both require shelling into the production host. The owner plan's P5 infrastructure item proposed
OpenTelemetry + self-hosted Grafana; that stack is 3–4 extra containers to
run, back up, and upgrade on the same host that serves the app.

PostHog now ingests both signals over standard OTLP — distributed tracing via
a generic OTLP receiver and logs over OpenTelemetry, with a free monthly tier
(50GB logs; verified 2026-07-14) — in the same project spec-081 already uses
for errors and analytics. Decision (owner, 2026-07-14): re-target the P5 item
from self-hosted Grafana to PostHog's OTLP endpoint, keeping the
vendor-neutral OpenTelemetry instrumentation. Separate spec and separate
implementation from spec-081.

## Solution

### Instrumentation (vendor-neutral by construction)

- Complete the existing scaffolding: configure a real `TracerProvider` +
  batch OTLP exporter behind the existing `OTEL_EXPORTER_OTLP_ENDPOINT`
  setting, and add SQLAlchemy/httpx auto-instrumentation alongside the
  already-wired FastAPI instrumentor. New dependencies (proposed here per the
  dependency rule): `opentelemetry-exporter-otlp` and the SQLAlchemy/httpx
  instrumentation packages — the api/sdk/FastAPI packages are already in
  `pyproject.toml`.
- Exporter is plain OTLP/HTTP configured entirely by env vars
  (`OTEL_EXPORTER_OTLP_ENDPOINT` + auth header carrying the PostHog project
  key). Unset ⇒ no tracer/exporter is installed — dev, test, e2e, and CI are
  untouched and need no mocking. Switching away from PostHog later is an
  endpoint-URL change, not a code change.
- Ship structlog output as OTel logs through the same exporter, correlated to
  the active trace/span ids. Local container stdout logging stays unchanged
  (the runbook's `docker logs` path keeps working).

### Privacy (binding, the core design problem of this spec)

Traces and logs are far chattier than spec-081's exception events. Before any
span or log line leaves the box:

- A redaction processor strips or hashes: query strings, request/response
  bodies (never captured as attributes), path parameters that are entity ids,
  and any attribute key matching a denylist (amount, balance, email, token,
  name, note, body).
- DB spans record statement *templates* only (no bound parameters) — enforce
  the instrumentation option, don't trust the default.
- The redaction processor gets its own tests: a request through an
  instrumented app must produce exported spans containing zero denylisted
  values (exporter captured in-memory).
- Never log or export PII/tokens (standing security rule).

### Scope of signals

- **Traces:** API request spans + DB + outbound httpx. Scheduled jobs get a
  root span per run so job latency is visible.
- **Logs:** existing structlog events, unmodified in content.
- **Metrics:** NOT included. PostHog has no Prometheus/Grafana-style
  infra-metrics product; API latency comes from traces, host health stays
  `docker stats`/runbook territory. If real metrics needs emerge, that is a
  new roadmap discussion, not scope creep here.

## Backend impact / API / schema impact

- No schema changes, no API contract changes, no frontend changes.
- New deps (api only): `opentelemetry-exporter-otlp` and the SQLAlchemy/httpx
  instrumentation packages (api/sdk/FastAPI instrumentation already present).
- Settings: `OTEL_EXPORTER_OTLP_ENDPOINT` already exists; add the standard
  OTel auth-header env var (not a custom name). Update
  `docs/PRODUCTION_DEPLOYMENT.md` and the config catalog in the same pass.
- The existing token-guarded Prometheus `/metrics` endpoint
  (`app/core/health.py`) is untouched by this spec.
- Startup cost when enabled is a background batch exporter; when disabled,
  zero.

## Testing

- Red first. In-memory span exporter tests: instrumentation present on a test
  app, redaction processor behavior (the denylist test above), disabled-by-
  default proof (no env ⇒ no exporter installed).
- Full suite + coverage gate unchanged; e2e stack sets no OTel env, proving
  the inert path.

## Out of scope

- Self-hosted Grafana/Tempo/Loki (superseded by this spec's PostHog target).
- Self-hosted OpenObserve (evaluated 2026-07-14 as the single-container
  self-hosted fallback; not adopted — PostHog is already required by spec-081
  and keeps errors/traces/logs in one project. Because the instrumentation is
  vendor-neutral OTLP, retargeting to OpenObserve later is an
  `OTEL_EXPORTER_OTLP_ENDPOINT` change plus a compose entry, not a code change).
- Metrics of any kind (see above).
- Frontend tracing/instrumentation (`posthog-js` from spec-081 already covers
  frontend visibility).
- Alerting/SLOs on trace data.
- Sampling tuning beyond a single head-sampling ratio env var; revisit only if
  free-tier volume is ever approached.
