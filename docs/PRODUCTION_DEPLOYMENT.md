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
