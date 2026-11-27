# Production Deployment Guide

This document outlines deployment configurations, environment variables, security defaults, and infrastructure setup for a production environment.

## Monorepo Layout
Deployments are built from the main repository branch:
* **API Backend**: `lifestack-api` (FastAPI / Python 3.13)
* **Frontend Web**: `lifestack-web` (React 19 / Vite 8)
* **Cloud Infrastructure**: Docker Compose v2.24.4+ + Cloudflare Tunnels (Zero Trust ingress)

---

## 1. Environment Variable Reference

Create a `.env.production` file on the deployment VM with the following keys:

### Core Settings
* `ENV`: Set to `"production"`.
* `SECRET_KEY`: A cryptographically secure random string (minimum 32 characters, e.g., generated with `openssl rand -hex 32`). **Using default values will prevent the application from starting.**
* `SESSION_SECRET_KEY`: A stable cryptographically secure random string of at least 32 characters used to sign OAuth state cookies. Every API replica must use the same value. **Using the default will prevent the application from starting.**
* `METRICS_TOKEN`: A non-default secret bearer token for the metrics endpoint (e.g., generated with `openssl rand -hex 32`). **Using the `dev-`-prefixed default value will prevent the application from starting.**
* `BACKEND_CORS_ORIGINS`: JSON array of allowed origins, e.g., `["https://app.lifestack.app"]`.
* `CSRF_TRUSTED_ORIGINS`: JSON array of allowed origins for CSRF validation, e.g., `["https://app.lifestack.app"]`.
* `COOKIE_SECURE`: Must be `True` (enforces HttpOnly, Secure session cookies).
* `GOOGLE_REDIRECT_URI` / `GITHUB_REDIRECT_URI`: Explicit HTTPS callback URLs registered with each OAuth provider, e.g. `https://lifestack-api.example.com/v1/auth/oauth/google/callback`.

### Database & Redis
* `DATABASE_URL`: PostgreSQL connection URI, e.g., `postgresql+asyncpg://user:pass@host:5432/lifestack`.
* `REDIS_URL`: Redis connection URI, e.g., `redis://host:6379/0`.
* `RATE_LIMIT_STORAGE_URI`: Redis storage backend, e.g., `redis://host:6379/1`. **In-memory storage is rejected in production.**

### Background Workers & Scheduler
* `SCHEDULER_ENABLED`: Set to `True` on exactly **one** API process instance.
  > [!IMPORTANT]
  > To prevent split-brain issues, APScheduler background jobs must only execute on one node. If multiple replicas are run, ensure only a single container has `SCHEDULER_ENABLED=True`. All scheduler tasks automatically utilize PostgreSQL advisory locks (`pg_try_advisory_xact_lock`) for safety.

### AI Integration
* `GEMINI_API_KEY`: API key for Google Gemini voice capture transcripts.
* `GEMINI_THINKING_BUDGET` (default `256`): thinking-token budget for voice tool-call reasoning; set `0` if the configured `GEMINI_MODEL` rejects a non-zero budget (spec-059).
* `CAPTURE_TURN_LOG_PATH` (spec-079 Stage A, feature-off unless set): append-only JSONL log of voice turns — tool calls, plus user/assistant transcripts when the two transcription flags below are on. Point it at `/app/logs/capture/turns.jsonl`, which `docker-compose.yml` bind-mounts to `./logs/capture` on the host so it survives container recreation — unlike the stdout-only structured logs, which don't.
* `CAPTURE_ENABLE_OUTPUT_TRANSCRIPTION` (spec-079 Stage B, default `false`): capture the assistant's spoken reply as text (live captions + assistant side of the turn log). Metered free (2026-07-13).
* `CAPTURE_ENABLE_INPUT_TRANSCRIPTION` (spec-079 Q4, default `false`): capture the user's own utterance as text into the turn log (`kind=user_transcript`) — the source for spec-079's real-usage eval slice. Metered free (2026-07-17). Set all three of these together to get a complete conversation record.
* `CAPTURE_ENABLE_SESSION_RESUMPTION` / `CAPTURE_ENABLE_CONTEXT_COMPRESSION` (spec-079 Stage B, both default `false`): WebSocket transport resilience — resumption handles let a reconnecting client restore conversation context after a dropped socket (`?resume=<handle>`); context compression keeps long sessions alive past the model's context limit. Enable only after confirming the deployed `GEMINI_MODEL` accepts the `sessionResumption`/`contextWindowCompression` setup fields.
* `CAPTURE_TOOL_DEDUP_WINDOW_SECONDS` (spec-090, default `2700`): replay-dedup window for write tool-calls on resumed sessions — a resumed connection that re-emits an already-executed call before any new user input gets the original result back (`duplicate_suppressed`) instead of double-executing. Always on (in-process guard, no env needed); widen/narrow only with measured cause. Production incident that motivated it: ≥15% of write tool-calls were resume-replays, observed up to +37 min after a suspended device reconnected with a stale handle.

### Web Push Notifications (spec-052)
* `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY`: VAPID keypair for Web Push. Generate with `vapid --gen` (from the `py-vapid` package, a `pywebpush` dependency) or `npx web-push generate-vapid-keys`. **Push is disabled (feature-off, not broken) when either key is unset** — subscription endpoints return 503 and `push_delivery_job` no-ops.
* `VAPID_SUBJECT`: A `mailto:` contact address required by the Web Push spec, e.g. `mailto:support@lifestack.app`.
* `PUSH_DELIVERY_INTERVAL_MINUTES`: How often the push-delivery queue drains. Default `1`.
* `TODO_REMINDER_INTERVAL_MINUTES`: How often due todos are scanned for reminders. Default `5`.

### PostHog Error Tracking (spec-081)
* `POSTHOG_API_KEY`: PostHog project API key. **Unset ⇒ the SDK is never initialized** — no network calls, no dependency on PostHog being reachable. Backend scope is exception capture only (unhandled API 500s + scheduled-job failures); product analytics live in the frontend, gated on the build-time `VITE_POSTHOG_KEY` (and optional `VITE_POSTHOG_HOST`) set wherever `lifestack-web` is built for production — unset there too ⇒ `posthog-js` is never initialized.
* `POSTHOG_HOST`: PostHog ingestion host. Default `https://us.i.posthog.com` — set to `https://eu.i.posthog.com` if the project was created on EU cloud.

### Resend Email Notifications (spec-081)
* `RESEND_API_KEY`: Resend API key. Unset ⇒ every email delivery is marked `skipped`, never sent.
* `EMAIL_FROM_ADDRESS`: Verified sender address, e.g. `notifications@lifestack.app` (requires DNS domain verification in the Resend dashboard).
* `EMAIL_ENABLED`: Explicit master switch, default `False` — **must be set to `True`** in addition to `RESEND_API_KEY` being present, so a copied `.env` can never accidentally start sending.
* `EMAIL_DELIVERY_BATCH_CAP`: Defensive cap on emails sent per drain-job run. Default `50` (Resend free tier is 100/day; a single-user deployment sends a handful/day).
* `EMAIL_DELIVERY_INTERVAL_MINUTES`: How often the email-delivery queue drains. Default `1`.

### OpenTelemetry Traces/Logs to PostHog (spec-082)
* `OTEL_EXPORTER_OTLP_ENDPOINT`: PostHog's OTLP endpoint, e.g. `https://us.i.posthog.com/otlp`. **Unset ⇒ no `TracerProvider`/exporter is installed** — FastAPI/SQLAlchemy/httpx instrumentation stays off and structlog output is not mirrored to OTel logs. Implement only after spec-081's PostHog project exists.
* `OTEL_EXPORTER_OTLP_HEADERS`: Standard OTel comma-separated `key=value` auth header, e.g. `Authorization=Bearer <posthog-project-key>`.
* Traces and logs pass through a redaction processor before export (query strings, request/response bodies, amount/balance/email/token/name/note/body-shaped attribute keys) — see `app/observability/tracing.py`. Switching away from PostHog later is an endpoint-URL change, not a code change.

---

## 2. Ingress & Cloudflare Tunnel

Lifestack runs with ingress configured via Cloudflare Tunnel (`cloudflared`), which removes the need to open public inbound firewall ports (like 80/443) on the VM.

1. Create a tunnel in the Cloudflare Zero Trust Dashboard.
2. Direct traffic to the internal container ports:
   * Frontend: `http://web-production:80`
   * API: `http://api-production:8000`
3. Export the tunnel token:
   * `CLOUDFLARE_TUNNEL_TOKEN=ey...`

---

## 3. Database Backups

The production environment stands up a client-side encrypted backup container (`database-backup`) that takes daily snapshots of PostgreSQL and uploads them to S3/R2 storage.

Required backup environment variables:
* `DB_BACKUP_ENCRYPTION_KEY`: Symmetric key to encrypt SQL dump files prior to egress.
* `DB_BACKUP_S3_BUCKET`: E.g., `lifestack-db-backups`.
* `DB_BACKUP_S3_ENDPOINT`: E.g., `https://<account_id>.r2.cloudflarestorage.com`.
* `DB_BACKUP_S3_ACCESS_KEY`: Cloudflare R2 / S3 access key.
* `DB_BACKUP_S3_SECRET_KEY`: Cloudflare R2 / S3 secret key.

---

## 4. Launching the Stack

`docker-compose.prod.yml` is an override file (it uses `!override`/`!reset` merge tags on the
`migrate`/`api` services), so it cannot run standalone. It must be layered on top of
`docker-compose.yml`.

Production uses the external PostgreSQL and Redis instances configured by `.env.production`; do
not enable the base file's `local` profile in a production deployment. The production override
removes the base `postgres`/`redis` `depends_on` entries from `migrate` and `api`. Without that
override, Compose excludes the profile services and reports `depends on undefined service`.

The recommended deployment entry point is the checked-in script below. It validates the merged
Compose configuration, prevents concurrent deployments, archives existing logs, recreates the
stack, waits for the API health check, and captures startup logs on success or failure:

```bash
./scripts/deploy-production.sh
```

Optional operator settings can be supplied without editing the script:

```bash
DEPLOY_HEALTH_TIMEOUT_SECONDS=300 \
DEPLOY_LOG_DIR=/var/log/lifestack/deploy \
./scripts/deploy-production.sh
```

Validate the merged configuration before changing containers:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml config --quiet
```

The `--env-file` flag is required here because values in `.env.production` are used both as
container environment and for Compose interpolation, including `CLOUDFLARE_TUNNEL_TOKEN`.

If the script is unavailable, archive the current container output before recreation. Docker's
default container stdout logs are tied to the container ID and are not available after `down` or
`--force-recreate`:

```bash
stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p logs/deploy
chmod 700 logs/deploy

docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  logs --timestamps --no-color api migrate cloudflared database-backup \
  > "logs/deploy/predeploy-${stamp}.log" || true
```

Start or recreate the stack:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  up -d --build --force-recreate --remove-orphans
```

`down` is not required for a normal deployment. If a full stop is necessary, archive logs first
and omit `-v` so named data volumes are not removed:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml down --remove-orphans
```

Verify startup and retain the first post-deploy evidence:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml ps

docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml \
  logs --since 10m --timestamps --no-color api migrate cloudflared database-backup
```

### 4a. One-time data copies after a deploy

A data repair is a reviewed script, never a migration. Run it inside the API container after the
deploy, dry-run first, and verify by a read (not by the script's own output).

**Weight into the measurement store (spec-111).** Run once right after the deploy that ships
migration `0075_health_measurements` and the weight flip. Until it runs, weight history reads as
empty (the old `weight_entries` rows are untouched).

```bash
docker exec <api container> python -m scripts.migrate_weight_to_health_measurements          # dry run
docker exec <api container> python -m scripts.migrate_weight_to_health_measurements --apply
docker exec <api container> python -m scripts.migrate_weight_to_health_measurements --apply  # no-op
```

The script is idempotent, refuses (writing nothing) if a copied row no longer matches its source,
and keeps public ids, values, timestamps and notes. Then confirm the count and the latest reading
with a read, for example `get_health_series` for `body_weight`, or:

```sql
SELECT (SELECT count(*) FROM weight_entries) AS old_rows,
       (SELECT count(*) FROM health_measurements m JOIN health_metrics t ON t.id = m.metric_id
         WHERE t.code = 'body_weight') AS new_rows;
```

`new_rows` may exceed `old_rows` once new weights are logged. `weight_entries` stays as the rollback
path until a later change drops it.

## 5. Log Retention and Incident Evidence

The API writes structured production logs to stdout. The default Docker `json-file` driver is
useful for live inspection with `docker compose logs`, but its history is removed with the
container. The `logs/deploy` archive above is a deployment safety net, not a long-term log store.

Configure the deployment host to use a persistent logging destination such as Docker's `journald`
driver or a remote collector (Vector/Loki, an OpenTelemetry backend, or an equivalent service),
with an explicit retention and disk-usage policy. Journald or a remote collector should be the
system of record for API, migration, tunnel, and backup logs; do not rely on container IDs for
incident history.

The optional `CAPTURE_TURN_LOG_PATH=/app/logs/capture/turns.jsonl` is separate: the Compose bind
mount preserves voice tool-call records under `./logs/capture` across container recreation. It does
not preserve general API stdout logs. Database audit records remain in PostgreSQL and should be
used alongside request logs when investigating financial or agent mutations.

Protect `logs/deploy` and `logs/capture` because operational output can contain identifiers and
request context. Rotate or export them through the same retention policy used by the host logging
system.

`docker-compose.prod.yml` bounds every service's `json-file` log (`max-size: 10m`, `max-file: 3`);
the VM has no `/etc/docker/daemon.json`, so that is the only cap. It applies when a container is
recreated.

## 6. Performance Diagnosis (spec-110, spec-112)

Metrics are scraped from `/metrics` by the homelab Prometheus and shown in Grafana; there is
nothing extra to run on the VM. What to look at, in order:

1. **Which route is slow?** Grafana "Avg seconds per request by route" next to "Requests by route".
   `route="mcp"` is MCP tool traffic (the MCP app is mounted at `/`); `route="unmatched"` is only
   404/scanner noise.
2. **Database or not?** "DB share of request time" and "SQL queries per request". A high query count
   is an N+1 loop; a low DB share means the time is outbound calls, CPU or swap.
3. **Was the VM starved?** "VM memory pressure" (PSI `some avg10`) and swap, read from `/proc` by the
   API itself. Compare with the slow-query graph.
4. **Which statement?** `docker logs lifestack-api-api-1 | grep slow_query` (duration, route and a
   truncated statement, never bind values), or the exact cost ranking:

   ```bash
   docker exec lifestack-api-api-1 python -m app.cli.pg_top_queries --limit 20
   ```

### Log level must not hide `slow_query`

`slow_query` is logged at WARNING. With `LOG_LEVEL=ERROR` structlog drops it while the Prometheus
counter still counts, which is how production once showed 1,141 slow queries and no statements.
Production runs `LOG_LEVEL=WARNING`. The API logs `slow_query_log_suppressed_by_log_level` at startup
if the level would hide it again.

### Enabling `pg_stat_statements` and the VM Postgres tuning (one-time, restarts Postgres)

`docker-compose.yml` starts the `postgres` service with
`-c shared_preload_libraries=pg_stat_statements`; `docker-compose.prod.yml` repeats that and adds the
VM tuning (`jit=off`, `shared_buffers=64MB`, `effective_cache_size=512MB`, `maintenance_work_mem=32MB`,
`max_connections=30`, `random_page_cost=1.1`) because the image defaults assume a much larger host than
the 1 OCPU / ~1 GB production VM. The deploy script does **not** manage Postgres (it
sits behind the `local` profile), so apply it deliberately. Take a `pg_dump -Fc` first. The API
reconnects after a few seconds; one scheduler job may log a single `connection is closed` at the
restart (applied 2026-11-26, one error, none after):

```bash
cd /home/ubuntu/lifestack-api
COMPOSE="docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml"
$COMPOSE --profile local up -d --no-deps postgres          # recreates it with the new flags
docker exec lifestack-api-postgres-1 sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"'
docker exec lifestack-api-api-1 python -m app.cli.pg_top_queries --limit 5   # expect a table, exit 0
```

Data persists in the `lifestack-api_pg-data` volume. `pg_stat_statements` stores normalised text
(`$1`, `$2`), so the report holds no amounts, names or tokens. Counters reset when Postgres restarts.

### The API lives in swap: why requests stall for 15 s and what fixes it (2026-11-26)

Symptom: after the app sits idle, the first pages you open take 15–30 s, and `slow_query` logs several
different statements finishing in the same second with durations of 15–50 s, while `pg_stat_statements`
shows no Postgres execution above ~1 s. The time is lost outside Postgres.

Cause (measured on the VM, not inferred): importing `app.main` alone costs ~215 MB resident
(fastapi, sqlalchemy/sqlmodel, fastmcp, redis, posthog, httpx, ...), and the API peaks at ~230 MB. The 954 MB
VM cannot keep that plus dockerd, containerd, Postgres and the page cache resident, so the kernel evicts the
idle API to the swapfile on the boot volume (`VmSwap` ~180 MB, `VmRSS` ~55 MB). The next burst of requests
faults ~160 MB back in as 4 KB random reads: **14.6 s, 19.3 s, 22.8 s and 31.8 s** in four timed runs.
Do not read the API's `process_resident_memory_bytes` as its size: it drops to 50-80 MB because the rest is
swapped out; use `VmSwap`/`VmHWM` in `/proc/<pid>/status`.

Measure it yourself (read-only; `VmSwap` is the part of the API that is currently on disk):

```bash
docker exec lifestack-api-api-1 sh -c 'grep -E "VmRSS|VmSwap|VmHWM" /proc/1/status'
```

What did **not** work: `mem_reservation` (cgroup `memory.low`), even with the parent `system.slice`
protected. The kernel still evicted the API within four minutes and the cgroup's `low` counter climbed to
17,269, i.e. nothing else was left to reclaim. There is not enough RAM to protect it; do not add it.

What works: **zram swap**, which turns a disk read into an in-RAM decompress. A fresh API process evicted
into zram (166 MB stored in ~46 MB of RAM, ~3.6x with zstd; disk swap unchanged) and faulted back in
**4.2 s** instead of 15-30 s. The VM runs it from `zram-swap.service` (512 MB, zstd, priority 100, above the
two disk swapfiles):

`/usr/local/sbin/zram-swap.sh` (mode 0755; `ZRAM_*` env overrides exist for testing on a spare device, and the
defaults are the production values):

```sh
#!/bin/sh
# zram-backed swap for the 1 GB production VM (spec-112 follow-up). Idempotent.
# Higher priority than the disk swapfiles, so newly evicted pages are compressed into RAM
# instead of being written to (and later read back from) the slow boot volume.
set -eu
DEV=${ZRAM_DEV:-/dev/zram0}; SIZE=${ZRAM_SIZE:-512M}; ALGO=${ZRAM_ALGO:-zstd}; PRIO=${ZRAM_PRIO:-100}
case "${1:-}" in
  start)
    modprobe zram
    if swapon --noheadings --show=NAME | grep -qx "$DEV"; then exit 0; fi
    zramctl --reset "$DEV" 2>/dev/null || true
    zramctl "$DEV" --algorithm "$ALGO" --size "$SIZE"
    mkswap -q "$DEV"
    swapon --priority "$PRIO" "$DEV"
    ;;
  stop)
    swapoff "$DEV" 2>/dev/null || true
    zramctl --reset "$DEV" 2>/dev/null || true
    ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac
```

`/etc/systemd/system/zram-swap.service`:

```ini
[Unit]
Description=zram compressed swap (512M, zstd, priority 100)
DefaultDependencies=no
After=systemd-modules-load.service
Before=swap.target shutdown.target
Conflicts=shutdown.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/zram-swap.sh start
ExecStop=/usr/local/sbin/zram-swap.sh stop

[Install]
WantedBy=swap.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now zram-swap.service
swapon --show; zramctl                                              # /dev/zram0 prio 100, zstd
```

Test overrides only with `sudo env ZRAM_DEV=/dev/zram1 ZRAM_SIZE=16M ZRAM_PRIO=1 ...` (plain `sudo -E` is
ignored on some hosts and the script would then act on the default `/dev/zram0`).

Two things to know:

1. **Restart the API once after enabling zram.** Pages that already have a copy in a disk swapfile are
   dropped on eviction without being rewritten, so they never move to zram and keep faulting from disk. A
   fresh process has no disk copies (~2 minutes to healthy on this VM).
2. **Never run `zram-swap.sh stop` (or `swapoff /dev/zram0`) while the API is swapped out**: it has to read
   everything back into RAM and needs ~170 MB free. Do not `swapoff` the disk swapfiles either (~330 MB).

Rollback: `sudo systemctl disable --now zram-swap.service` only when memory is free, otherwise just
`sudo systemctl disable zram-swap.service` and reboot. The unit failing at boot leaves the VM exactly as it
was before (no zram).

Footprint (perf/api-memory-footprint, measured in the production image with Python 3.13, importing
`app.main`; the API process starts at about this size before serving a request):

| | heap (`RssAnon`) | total RSS |
|---|---|---|
| before | 178.0 MB | 208.5 MB |
| after | 147.9 MB | 174.5 MB |

What changed: `openpyxl` (Excel import), `pdfplumber`/`pdfminer` (CAS PDF import), `pywebpush` (push
delivery) and the OpenTelemetry SDK, exporters and instrumentors are imported where they are used, not at
module top level. OpenTelemetry loads only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (it is not in
production); `redis` still imports `opentelemetry.sdk.metrics` itself, which this app cannot avoid.
PostHog and MCP stay eager because production uses both. `app/tests/test_import_footprint.py` fails if any
of those libraries is imported at start-up again. The first Excel or PDF import after a restart pays the
import cost (about a second).

Image: `--no-install-recommends` for ffmpeg (about 560 MB less; WebM/Opus, Ogg/Opus and M4A/AAC decode
byte-identical), `--compile-bytecode` (site-packages is root-owned, so `appuser` could never cache `.pyc`
and every start recompiled ~2,500 modules: import 8.4 s to about 5 s, image +130 MB), `COPY --chown`
instead of a recursive `chown` (no duplicated `/app` layer), tests excluded by `.dockerignore`, uv pinned.
Net 1.66 GB to 1.42 GB, plus `google-generativeai` and the unused `httplib2` pin removed: voice capture
talks to Gemini Live over raw `websockets` (`app/capture/agent.py`), never through the SDK.

Still open: the heap is still ~150 MB because SQLAlchemy, Pydantic (338 app models), FastAPI and the MCP
stack are core; going lower needs a bigger host or fewer Pydantic models, not more lazy imports.
