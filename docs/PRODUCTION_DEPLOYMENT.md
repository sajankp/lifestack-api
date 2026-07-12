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
* `BACKEND_CORS_ORIGINS`: JSON array of allowed origins, e.g., `["https://app.lifestack.app"]`.
* `CSRF_TRUSTED_ORIGINS`: JSON array of allowed origins for CSRF validation, e.g., `["https://app.lifestack.app"]`.
* `COOKIE_SECURE`: Must be `True` (enforces HttpOnly, Secure session cookies).

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

### Web Push Notifications (spec-052)
* `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY`: VAPID keypair for Web Push. Generate with `vapid --gen` (from the `py-vapid` package, a `pywebpush` dependency) or `npx web-push generate-vapid-keys`. **Push is disabled (feature-off, not broken) when either key is unset** — subscription endpoints return 503 and `push_delivery_job` no-ops.
* `VAPID_SUBJECT`: A `mailto:` contact address required by the Web Push spec, e.g. `mailto:support@lifestack.app`.
* `PUSH_DELIVERY_INTERVAL_MINUTES`: How often the push-delivery queue drains. Default `1`.
* `TODO_REMINDER_INTERVAL_MINUTES`: How often due todos are scanned for reminders. Default `5`.

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
`migrate`/`api` services), so it cannot run standalone — it must be layered on top of the base
`docker-compose.yml`, which also owns the `postgres`/`redis` services:

```bash
docker compose --profile local --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml up -d --build --force-recreate
```
