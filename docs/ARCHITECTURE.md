# Lifestack - Platform Architecture and Build Plan

> Personal OS first. SaaS-capable later. No premature platform complexity.

---

## What Lifestack Is

Lifestack is a modular monolith built around three domains:
- todo
- spending
- investing

It starts as a personal operating system for one user or one household. The architecture is deliberately shaped so it can later support SaaS features such as multiple workspaces, shared access, billing, and quotas without rewriting the core modules.

The key architectural decision is to optimize for:
- clean module boundaries
- workspace-scoped data
- a single database
- one deployable backend
- adapters like AI chat and MCP on top of the core services, not inside them

---

## Architecture Principles

### 1. Personal OS first
Stage 1 should feel complete and useful without AI, MCP, billing, or message infrastructure.

### 2. Modular monolith
One FastAPI app and one PostgreSQL database are enough for this product for a long time.

### 3. Workspace-scoped from day one
Even for a single personal user, all business tables should carry a `workspace_id`. In stage 1 there may be one default workspace per user, but that is still a real workspace concept, not a shortcut back to `user_id` scoping. Later, the same shape supports teams, households, and SaaS plans.

### 4. Adapters over core services
REST, dashboard views, chat, and MCP should all call the same application and domain services.

### 5. Scheduler before Pub/Sub
Time-based work belongs in scheduled jobs. Immediate cross-module actions should be direct service calls. Asynchronous infrastructure should appear only when reliability or scale requires it.

---

## Repository Structure

```text
sajankp/
|-- lifestack-api
|-- lifestack-web
`-- lifestack-e2e
```

The monorepo contains `lifestack-api` (FastAPI backend), `lifestack-web` (React/Vite frontend), and `lifestack-e2e` (Playwright test suite).

---

## Backend: `lifestack-api`

### Recommended Tech Stack

| Layer | Technology |
|---|---|
| Framework | FastAPI, Python 3.13 |
| Validation | Pydantic v2 |
| ORM / Models | SQLModel or SQLAlchemy |
| Database | PostgreSQL |
| Migrations | Alembic |
| Auth | JWT access + refresh flow (HttpOnly cookies, carried from to-do app) |
| Password Hashing | pwdlib — Argon2id |
| JWT Library | python-jose (HS256) |
| Rate Limiting | slowapi (Redis backend) |
| Logging | structlog (JSON output, trace/span enrichment) |
| Security Headers | Custom OWASP middleware (CSP, HSTS, X-Content-Type-Options) |
| Observability | OpenTelemetry + Prometheus + Jaeger + Loki + Grafana |
| Scheduler | APScheduler |
| Background work | In-process jobs first, DB-backed outbox later if needed |
| MCP | Optional stage 2 adapter |
| Testing | pytest |
| Linting | Ruff |
| CI | GitHub Actions |
| Containerization | Docker + Docker Compose |

### Why keep JWT auth?

The existing todo app already uses JWT-based auth with HttpOnly cookies. Reusing that model gives you:
- continuity with the current app
- less rewrite risk
- a clearer migration path into Lifestack
- XSS protection (tokens are not accessible to JavaScript)
- an API shape that still works later for mobile clients, MCP clients, or external integrations

The cookie-based auth pattern, CSRF origin checks, and session tracking are carried forward from the to-do app. Password hashing is simplified to Argon2id-only since Lifestack is a new project with no legacy bcrypt data. See [Auth Architecture](#auth-architecture) for details.

---

## Backend Directory Shape

```text
lifestack-api/
|-- app/
|   |-- main.py
|   |-- config.py
|   |
|   |-- core/
|   |   |-- auth.py              # JWT encode/decode/verify utilities
|   |   |-- dependencies.py
|   |   |-- exceptions.py
|   |   |-- scheduler.py
|   |   |-- audit.py
|   |   |-- currency.py           # stateless currency & FX helpers
|   |   `-- database/
|   |       `-- postgres.py
|   |
|   |-- auth/                    # login, register, token refresh endpoints
|   |   |-- router.py
|   |   |-- service.py
|   |   |-- repository.py
|   |   |-- models.py
|   |   `-- schemas.py
|   |
|   |-- todo/
|   |   |-- router.py
|   |   |-- service.py
|   |   |-- repository.py
|   |   |-- models.py
|   |   |-- schemas.py
|   |   `-- tests/
|   |
|   |-- spending/
|   |   |-- router.py
|   |   |-- service.py
|   |   |-- repository.py
|   |   |-- response_helpers.py  # response schema transformations
|   |   |-- models.py
|   |   |-- schemas.py
|   |   `-- tests/
|   |
|   |-- investing/
|   |   |-- router.py
|   |   |-- service.py
|   |   |-- repository.py
|   |   |-- response_helpers.py  # response schema transformations
|   |   |-- models.py
|   |   |-- schemas.py
|   |   `-- tests/
|   |
|   |-- dashboard/
|   |   |-- router.py
|   |   `-- service.py
|   |
|   |-- application/
|   |   |-- workflows.py
|   |   `-- jobs.py
|   |
|   |-- platform/
|   |   |-- models.py            # workspace, membership, role models
|   |   |-- repository.py
|   |   `-- service.py
|   |
|   |-- capture/                 # WebSocket voice agent & Gemini tools
|   |-- exports/                 # Data export generation
|   |-- imports/                 # CSV data imports & preview
|   |-- notifications/           # System notifications & counts
|   |-- summaries/               # Weekly summaries
|   |-- finance/                 # Accounts, user settings, & FX rates
|   |-- cli/                     # CLI administrative tools
|   |-- testing/                 # Local mock tools & test hooks
|   |
|   |-- ai/          # stage 2
|   `-- mcp/         # stage 2
|
|-- alembic/
|-- docs/
|-- Dockerfile
|-- docker-compose.yml
|-- docker-compose.prod.yml
`-- .env.example
```

The most important addition here is `application/`. That is where cross-module workflows belong.

---

## Layering

Each module should keep the simple shape:

```text
router.py      -> request handling and validation
service.py     -> business logic for that module
repository.py  -> database access
database       -> PostgreSQL
```

For concrete code examples of every layer (models, schemas, repository, service, router), see [PATTERNS.md](PATTERNS.md).

Cross-module behavior should not be implemented by making modules call each other freely. Put those workflows in `app/application/`.

Example:

```python
class BudgetReviewWorkflow:
    def __init__(self, spending_service, todo_service):
        self.spending_service = spending_service
        self.todo_service = todo_service

    async def handle_budget_exceeded(self, workspace_id: int) -> None:
        summary = await self.spending_service.get_budget_status(workspace_id)
        if summary.is_over_limit:
            await self.todo_service.ensure_system_task(
                workspace_id=workspace_id,
                system_key="budget_review",
                title="Review this month's spending",
                cooldown_hours=24,
            )
```

This keeps the module services focused and avoids hidden coupling.

---

## Coordination Model

This is the biggest point that needs clarity.

### What should happen synchronously?

Use direct service calls for actions that are part of one user flow and should succeed together.

Examples:
- create transaction -> update budget totals
- finish rebalance review -> mark task complete
- create export request -> record audit entry

### What should happen on a schedule?

Use APScheduler for time-based automation.

Examples:
- recurring transactions
- daily reminders
- weekly summaries
- monthly rebalance checks

### What should happen asynchronously later?

If a side effect can happen after the request, use a DB-backed job table or outbox pattern before adopting Redis Pub/Sub.

Examples:
- sending emails
- generating long AI summaries
- recalculating portfolio analytics
- exporting large datasets

### Is Pub/Sub needed now?

Probably not.

For stage 1, scheduler + direct orchestration + optional DB-backed jobs are enough.

Redis Pub/Sub becomes useful only when:
- multiple consumers need the same event
- jobs run in separate worker processes
- real-time fan-out matters
- you need independent scaling for different workloads

Until then, Pub/Sub adds moving parts without solving a stage 1 problem.

---

## Data Model Strategy

### Use one PostgreSQL database

Keep todo, spending, investing, auth, audit, and exports in the same database.

That gives you:
- simpler operations
- easier local development
- transactional consistency
- straightforward dashboard queries

### Add `workspace_id` everywhere

Every business table should be scoped by `workspace_id`.

Examples:
- `todos.workspace_id`
- `transactions.workspace_id`
- `holdings.workspace_id`
- `audit_logs.workspace_id`

That is the main design choice that makes SaaS migration easier later.

### ID strategy

Use `BIGINT` primary keys internally and UUIDs for external-facing identifiers where needed.

That gives you:
- smaller indexes and faster joins inside PostgreSQL
- simpler foreign keys across modules
- non-sequential public identifiers for URLs, exports, and integrations

A practical pattern is:
- `id` -> internal database primary key
- `public_id` -> external identifier exposed to clients
- `workspace_id` -> internal tenant foreign key on business tables

### Identity model

Keep the ownership model explicit:
- `users` represent authenticated people
- `workspaces` own the business data
- `workspace_memberships` map users to workspaces and roles

In stage 1, a single user can simply have one default workspace. The structure still scales cleanly when you later add shared or team-based usage.

### Stage 1 workspace contract

For stage 1, the backend should still behave as workspace-aware even if each user only has one default workspace.

That means:
- business tables are scoped by `workspace_id`, not by `user_id`
- authenticated request context resolves both the current user and the active workspace
- repositories query by `workspace_id`
- tests must prove that one authenticated user's workspace data is not visible from another user's workspace context

If the implementation uses a derived default workspace per user before full workspace and membership tables are introduced, document that explicitly in code and tests as a temporary stage-1 shape, not as the target long-term architecture.

### Use JSONB carefully

JSONB is fine for optional metadata or flexible notes.

Good candidates:
- tags
- ai_metadata
- import metadata

Less ideal candidates:
- recurring transaction rules
- complex subtasks with behavior
- anything queried heavily or enforced by business rules

If a structure drives real product logic, model it as a table.

---

## Auth Architecture

### Stage 1 — Cookie-Based JWT (Carried from To-Do)

Retain the proven auth pattern from the existing todo app:

| Aspect | Implementation |
|--------|----------------|
| Algorithm | HS256 (HMAC-SHA256) |
| Access Token TTL | 30 minutes (configurable via `ACCESS_TOKEN_EXPIRE_SECONDS`) |
| Refresh Token TTL | 7 days (configurable via `REFRESH_TOKEN_EXPIRE_SECONDS`) |
| Token Storage | HttpOnly secure cookies (`access_token`, `refresh_token`) |
| Password Hashing | Argon2id only (new project, no legacy hashes) |
| Session Tracking | Session ID (`sid`) embedded in JWT claims and validated against DB-backed `auth_sessions` rows |
| CSRF Protection | Origin validation for cookie-authenticated `POST`/`PUT`/`PATCH`/`DELETE` requests against trusted origins |

**Auth flow:**
1. Login → API creates an `auth_sessions` row, then sets `access_token` and `refresh_token` as HttpOnly cookies
2. Authenticated requests → middleware reads `access_token`, validates the JWT, validates the `sid` against `auth_sessions`, then the dependency layer resolves the active `workspace_id`
3. Token refresh → client sends `refresh_token` cookie to `/v1/auth/refresh`; the session must still be active before a new access token is issued
4. Logout → API revokes the active session server-side and clears both cookies

For stage 1, the active workspace may be the user's default workspace. The important rule is that handlers and repositories operate on `workspace_id`, not on raw `user_id` ownership checks. If a user somehow has no workspace membership, the fallback path must provision the default workspace and its seeded spending categories inside the same request transaction.

### Stage 2

If MCP or external API clients are added, introduce a separate auth surface for them:
- personal access tokens, or
- OAuth / integration tokens

Do not imply that web JWT auth automatically solves MCP auth. Treat MCP as a later adapter with its own documented auth flow.

That is why the README should not advertise MCP integration until it is real and tested.

---

## Dashboard Architecture

The dashboard should be a read model, not its own domain.

It should aggregate:
- upcoming todos
- budget status
- portfolio summary
- a few cross-module highlights

Its service can call repositories or module services to build a single response for the UI. Avoid duplicating business rules inside the dashboard layer.

---

## Audit Logging (Spec 004) — Implemented

Audit logging is live across the `todo` and `spending` modules.

### Implementation

- **Model**: `AuditLog` in `app/core/audit.py` — append-only table with `workspace_id`, `actor_id`, `action`, `module`, `entity_type`, `entity_id`, `details` (JSONB), and a timezone-aware `timestamp`.
- **Helper**: `AuditLogger` class injected via FastAPI `Depends(get_audit_logger)`. It enforces the event contract, applies PII/secrets redaction, and calls `session.flush()` — keeping the audit row in the same transaction as the business mutation. Commit or rollback of the business change atomically commits or discards the audit row.
- **Event contract** (all mutations must provide):
  - `entity_public_id` — public UUID of the affected entity
  - `before` — snapshot before the mutation (null for creates)
  - `after` — snapshot after the mutation (null for deletes)
  - `changed_fields` — list of field names that changed
  - `request_id` — correlation ID injected from structlog context
- **Redaction**: All `details` payloads pass through a recursive allowlist redactor that replaces keys matching `password`, `token`, `api_key`, `secret`, `account_number`, etc. with `[REDACTED]` before insertion.
- **Injection point**: Service/Workflow boundary — not inside generic repository helpers. Routers resolve the `AuditLogger` dependency alongside the DB session.
- **Observability**: Every successful write emits a `audit_log_written` structlog event with `module`, `action`, `entity_type`, `workspace_id`.

### Covered mutations

| Module | Entity | Actions |
|---|---|---|
| todo | `todo` | create, update, complete, delete |
| spending | `spending_category` | create, update, delete |
| spending | `spending_transaction` | create, update, delete |
| spending | `spending_budget` | create, update |
| application | `todo` (system) | budget_guardrail_triggered |

### Export

Export is planned for a later slice:
- CSV for analysis
- JSON for backups or migrations

It reinforces trust better than adding AI too early.

---

## Scheduler and Background Jobs (Spec 005) — Implemented

### Architecture

APScheduler (`AsyncIOScheduler`) is embedded in the FastAPI `lifespan` context manager.

**Boundary rule** (strictly enforced):

```text
app/application/jobs.py      ← scheduler wrappers only
  - advisory lock acquisition
  - workspace iteration
  - per-workspace session boundaries
  - per-workspace timeout (asyncio.wait_for)
  - error isolation (one workspace failure does not stop the batch)
  - structured log on every outcome (job_name, workspace_id, duration_ms, status)

app/application/workflows.py ← business logic only
  - no scheduler imports
  - no session lifecycle management
  - no workspace iteration
  - receives a session and a workspace, returns a result
```

Jobs call workflows. Workflows do not import or manage scheduler concerns.

### Gating

The scheduler only starts when `SCHEDULER_ENABLED=true`. In production, exactly one process instance should have this flag set. All other instances run without registering jobs.

### Split-brain prevention

Each job acquires a Postgres advisory transaction lock (`pg_try_advisory_xact_lock`) before executing. If two instances are briefly running (e.g. during a rolling deploy), only the first to acquire the lock proceeds; the other skips silently.

### Per-workspace timeout

Each workspace evaluation is wrapped in `asyncio.wait_for(..., timeout=300.0)`. A workspace that hangs is abandoned after 5 minutes with a structured error log. This prevents unbounded drain on application shutdown.

### Config

| Variable | Default | Description |
|---|---|---|
| `SCHEDULER_ENABLED` | `false` | Enable/disable job registration on startup |
| `SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS` | `false` | Safety guard: blocks registration of non-idempotent jobs unless explicitly enabled |
| `BUDGET_GUARDRAILS_INTERVAL_HOURS` | `6` | How often the budget guardrails job runs |
| `BUDGET_WARNING_THRESHOLD` | `0.9` | Spend ratio that triggers a warning todo (90%) |
| `BUDGET_CRITICAL_THRESHOLD` | `1.0` | Spend ratio that triggers a critical todo (100%) |

---

## Budget Guardrails Workflow (Spec 009) — Implemented

The budget guardrails workflow is the first production scheduler workflow.

### What it does

For every active workspace, every 6 hours:
1. Fetches current-month budgets and aggregates expense transactions per category.
2. Evaluates each budget against warning (≥90%) and critical (≥100%) thresholds.
3. Creates or updates a **system todo** for each breached category.
4. Auto-resolves (marks completed) the system todo when spend drops below threshold.
5. Writes an audit log row for every created, updated, or resolved todo.

### system_key — idempotency mechanism

`system_key` is a nullable field on the `Todo` model with a `UNIQUE(workspace_id, system_key)` constraint. It separates machine-generated todos from user-created ones.

- `system_key = None` → user-created todo (no uniqueness enforced)
- `system_key = "budget:guardrail:{category_id}"` → machine-owned, at most **one per workspace per category**

When the job re-runs, it looks up the existing todo by `system_key` and **updates** it (escalating warning→critical or resolving) rather than creating a duplicate. The DB constraint provides a hard idempotency guarantee at the storage level.

### Audit events

All guardrail actions use `module="application"`, `action="budget_guardrail_triggered"`, with `before`/`after` snapshots of the todo state at the time of the action.

---

## AI and MCP

AI features are framed as adapters over stable domain services.

### Stage 2 Design

The to-do app has an existing Gemini voice WebSocket proxy, but its architecture (direct WebSocket passthrough to a single provider) may not be the best pattern for Lifestack's multi-module scope. AI integration in Lifestack should be reconsidered with a proper adapter design:

```text
chat or MCP request
    -> AI/MCP adapter
    -> application workflow or module service
    -> repository
```

This means:
- chat does not own business logic
- MCP does not bypass validation rules
- adding or removing AI later does not damage the core product
- AI adapter should be provider-agnostic, not coupled to a single vendor

### README guidance

The README should say:
- AI chat is planned for stage 2
- MCP tools are planned for stage 2
- auth details for MCP are intentionally omitted until finalized

That removes confusion for readers and makes the project look more disciplined.

---

## Frontend: `lifestack-web`

### Recommended Stack

| Layer | Technology |
|---|---|
| Framework | React 19 + TypeScript |
| Build | Vite |
| Routing | React Router |
| State | Zustand + TanStack Query |
| Charts | Recharts |
| Testing | Vitest |

### Structure

```text
lifestack-web/
|-- src/
|   |-- shared/
|   |-- auth/
|   |-- dashboard/
|   |-- todo/
|   |-- spending/
|   |-- investing/
|   `-- chat/      # stage 2
`-- public/
```

The frontend should mirror backend module boundaries. Keep server-state in TanStack Query and reserve Zustand for auth/session/UI state that is truly client-side.

---

## Infrastructure

### Stage 1

```yaml
services:
  api:
    build: .
    ports: ["8000:8000"]
    depends_on: [postgres]

  postgres:
    image: postgres:18
    volumes: [pg-data:/var/lib/postgresql/data]
```

That is enough for the personal OS core.

### Observability Stack (Carried from To-Do)

The to-do app has a proven observability setup that should be carried forward:

```yaml
services:
  otel-collector:
    image: otel/opentelemetry-collector:latest
    # Receives traces/metrics from the API, forwards to backends

  jaeger:
    image: jaegertracing/all-in-one:latest
    # Distributed tracing UI

  prometheus:
    image: prom/prometheus:latest
    # Metrics scraping from /metrics endpoint

  loki:
    image: grafana/loki:latest
    # Log aggregation

  grafana:
    image: grafana/grafana:latest
    # Unified dashboards for traces, metrics, and logs
```

See the to-do app's `docker-compose.yml` for the full working configuration.

### Redis

Redis is included from stage 1 as the backend for rate limiting. It also serves as the foundation for future needs:
- distributed rate limiting (stage 1)
- worker queues (when needed)
- cache invalidation (when needed)
- pub/sub fan-out (when needed)

---

## Observability

All observability patterns are carried forward from the to-do app.

### Metrics

- `prometheus-fastapi-instrumentator` auto-instruments all routes
- `/metrics` endpoint protected by bearer token or dev-mode access
- Custom metrics for business events (logins, registrations, todos created/completed/deleted, DB query duration)

### Tracing

- OpenTelemetry instrumentation for FastAPI and the database driver
- OTLP HTTP export to the collector when `OTEL_EXPORTER_OTLP_ENDPOINT` is configured
- Trace context propagated through all layers

### Logging

- structlog with JSON output for machine parseability
- Middleware enriches every log entry with `trace_id` and `span_id`
- Configurable log level via `LOG_LEVEL` environment variable
- Request/response logging with timing and status codes

### Dashboards

Grafana unifies all three signals:
- Prometheus for metrics and alerting
- Jaeger for distributed trace exploration
- Loki for log search with trace correlation

---

## Security Middleware

All security middleware is carried forward from the to-do app.

### OWASP Security Headers

Security headers are applied via inline middleware in `app/main.py`. The middleware adds the following headers to all API responses (Swagger UI paths are exempted from CSP to allow the documentation UI to function):
- `Content-Security-Policy` (configurable `img-src`, `style-src`, `script-src`, `font-src`)
- `Strict-Transport-Security` (HSTS, `max-age=31536000`)
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`

### Rate Limiting

| Endpoint Type | Limit | Strategy |
|---------------|-------|----------|
| Auth endpoints (`/v1/auth/login`, `/v1/auth/register`, `/v1/auth/refresh`) | Configurable via `RATE_LIMIT_AUTH` | User ID when authenticated, IP fallback |
| API endpoints | Configurable via `RATE_LIMIT_DEFAULT` | User ID when authenticated, IP fallback |
| Storage | `memory://` by default for local/dev; Redis via `RATE_LIMIT_STORAGE_URI` for production |

Rate-limit responses should also use the same RFC 7807 `application/problem+json` shape as the rest of the API.

### CORS

- Configurable via environment variables (`BACKEND_CORS_ORIGINS`, `CORS_ALLOW_METHODS`, etc.)
- Origin sanitization strips paths and trailing slashes
- Credentials support for cookie-based auth

---

## API Versioning

All API routes should be versioned from day one:

```text
/v1/todo/
/v1/spending/
/v1/investing/
```

This provides:
- a deprecation path for breaking changes
- backwards compatibility for existing clients
- a clear contract for MCP and external integrations later

Version the router prefix, not individual endpoints. When `v2` is needed, the old routes remain active with a documented sunset date.

---

## Error Handling (RFC 7807)

All error responses should follow the [RFC 7807 Problem Details](https://www.rfc-editor.org/rfc/rfc7807) standard from day one:

```json
{
    "type": "https://lifestack.app/errors/not-found",
    "title": "Resource not found",
    "status": 404,
    "detail": "Todo with id 'abc' does not exist in this workspace",
    "instance": "/v1/todo/abc"
}
```

This gives clients:
- machine-readable error types
- human-readable descriptions
- consistent error shape across all modules
- a standard that API clients and MCP adapters can rely on

---

## Testing Strategy

### Testing Pyramid

```text
        ┌─────────────────┐
        │   E2E Tests     │  ← Slowest, run on main merge
        │  (Playwright)   │
        ├─────────────────┤
        │  Integration    │  ← Medium, run on PRs
        │  Tests (Real DB)│
        ├─────────────────┤
        │   Unit Tests    │  ← Fastest, run on every push
        │ (Mocked deps)   │
        └─────────────────┘
```

### Unit Tests
- Mock repositories and services
- Test business logic in isolation
- Target: 80%+ coverage
- Framework: pytest + AsyncMock

### Integration Tests
- Use testcontainers to spin up real PostgreSQL; add Redis-backed runs when validating the production rate-limit profile
- Tests hit actual database through the full stack
- Verify SQL queries, transactions, and constraint enforcement
- Verify workspace isolation explicitly: one authenticated user's workspace data must not leak into another user's results
- Verify migration-backed setup, not `create_all()`-style schema bootstrapping
- Verify that request-to-workspace resolution is deterministic and documented for stage 1
- Verify session revocation, duplicate-registration conflicts, and rate-limit problem responses

### Co-verification pattern (audit E2E tests)

Audit logging E2E tests must verify both the business outcome and the audit log in the **same session**, proving the log faithfully mirrors the actual DB state:

```python
async with postgres.async_session_maker() as session:
    db_entity = ...  # fetch from DB after the HTTP action

    # 1. Entity is correct in DB
    assert db_entity.title == expected_title

    # 2. API response matches DB (no silent mangling between layers)
    assert api_response["title"] == db_entity.title

    # 3. Audit log mirrors actual DB values — not the request payload
    audit = ...
    assert audit.details["after"]["title"] == db_entity.title
    assert audit.details["before"] is None  # contract enforced
```

This is stricter than checking `details["after"]["title"] == "hardcoded string"` because it proves the log captures what was *actually persisted*, not just what was *sent in the request*.

### E2E Tests
- Playwright for full browser-to-API flows
- Cover critical paths: auth, todo CRUD, cross-module workflows
- Include at least one workspace isolation scenario where a second user cannot see or mutate the first user's records
- Run against a real Docker Compose environment

---

## CI Gate Strategy

| Trigger | Unit Tests | Integration Tests | E2E Tests |
|---------|------------|-------------------|------------|
| Every push | ✅ Required | ❌ Skip | ❌ Skip |
| Pull Request | ✅ Required | ✅ Required | ❌ Skip |
| Merge to main | ✅ Required | ✅ Required | ✅ Required |
| Nightly scheduled | ✅ Required | ✅ Required | ✅ Required |

This balances developer velocity (fast push feedback) with release confidence (full E2E on merge).

---

## Build Phases

### Phase 1 - Personal OS Foundation

| Item | Status |
|---|---|
| Scaffold `lifestack-api` as a modular monolith | ✅ Done |
| JWT auth carried forward (HttpOnly cookies, CSRF, session tracking) | ✅ Done |
| Todo module (CRUD, priorities, due dates, workspace scoping) | ✅ Done |
| Spending module (categories, transactions, budgets) | ✅ Done |
| Dashboard read model | ✅ Done |
| Audit logging (Spec 004) — append-only, in-transaction, PII-redacted | ✅ Done |
| Scheduler infrastructure (Spec 005) — APScheduler, gated, advisory lock | ✅ Done |
| Budget guardrails workflow (Spec 009) — system todos, idempotency, audit | ✅ Done |
| API versioning (`/v1/`) | ✅ Done |
| RFC 7807 error responses | ✅ Done |
| Workspace-scoped data model with isolation tests | ✅ Done |
| Integration tests with real Postgres + Redis testcontainers | ✅ Done |
| Investing module | ✅ Done |
| Export (CSV / JSON) | ✅ Done |
| Recurring transactions scheduler workflow | ✅ Done |
| WebSocket voice agent / capture tools (Spec 021) | ✅ Done |
| CSV bulk data imports / preview validation (Spec 022) | ✅ Done |
| Weekly summary enrichment generation (Spec 016) | ✅ Done |
| Multi-currency finance accounts, display setting preferences & FX rates | ✅ Done |
| E2E tests (Playwright, full Docker Compose) | ✅ Done |

### Phase 2 - AI and Integrations
- design AI adapter architecture (provider-agnostic)
- add chat UI
- add usage tracking and rate limits
- add MCP as an optional adapter
- document MCP auth only when implemented

### Phase 3 - SaaS Expansion
- extend workspaces with multi-user memberships, roles, and team features
- add quotas, billing, and admin dashboard
- add workers or message infrastructure if justified by workload

---

## Carried Forward from To-Do

The following production-ready patterns are proven in the existing to-do app and should be retained in Lifestack:

| Pattern | To-Do Implementation | Status |
|---------|---------------------|--------|
| Cookie-based JWT auth | HttpOnly cookies, access + refresh tokens | ✅ Carry forward |
| CSRF protection | Origin validation on mutating requests | ✅ Carry forward |
| Password hashing | Argon2id (simplified — no bcrypt migration needed) | ✅ Carry forward |
| Session tracking | Session IDs in JWT claims | ✅ Carry forward |
| Rate limiting | slowapi with Redis backend | ✅ Carry forward |
| Security headers | OWASP middleware (CSP, HSTS, X-Frame-Options) | ✅ Carry forward |
| Structured logging | structlog with trace/span enrichment | ✅ Carry forward |
| Metrics | Prometheus + custom business metrics | ✅ Carry forward |
| Tracing | OpenTelemetry (FastAPI + DB driver) | ✅ Carry forward |
| Log aggregation | Loki integration | ✅ Carry forward |
| Dashboards | Grafana (traces + metrics + logs) | ✅ Carry forward |
| CORS | Configurable origins with sanitization | ✅ Carry forward |
| Health checks | Liveness + readiness probes | ✅ Carry forward |
| Metrics auth | Bearer token or dev-mode for `/metrics` | ✅ Carry forward |

**Not carried forward:**
- bcrypt legacy hashing — new project, Argon2id only

**Implemented in Phase 1 (not deferred):**
- Gemini voice/capture agent (Spec 021) — WebSocket-based voice agent with tool calling is implemented in `app/capture/`. Stage 2 will add a provider-agnostic AI adapter architecture on top of the existing capture surface.

**What changes in migration:**
- Database switches from MongoDB to PostgreSQL
- Sync PyMongo calls become async SQLAlchemy sessions
- Flat router-to-DB becomes layered (router → service → repository)
- `user_id` scoping becomes `workspace_id` scoping
- ObjectId IDs become BIGINT + UUID
- API routes gain `/v1/` prefix
- Error responses follow RFC 7807

---

## What Needs Extra Clarity in the Docs

These points should stay explicit across README and architecture docs:
- what works today vs what is planned
- personal OS first, SaaS later
- JWT cookie auth is intentional because it comes from the existing todo app
- MCP is not part of the core architecture yet
- scheduler and direct workflows are the default coordination model
- Pub/Sub is optional, not foundational
- `workspace_id` is the migration path to SaaS
- observability and security middleware are carried forward, not new work

---

## Bottom Line

For this project, the right architecture is:
- a tenant-aware modular monolith
- JWT cookie auth (Argon2id) retained from the current todo app
- PostgreSQL as the source of truth
- Redis for rate limiting from stage 1
- scheduler plus direct workflows for stage 1
- full observability stack (OTel + Prometheus + Jaeger + Loki + Grafana) from day one
- security middleware (OWASP headers, rate limiting, CSRF) from day one
- API versioning (`/v1/`) and RFC 7807 error responses from day one
- CI gates (unit on push, integration on PR, E2E on merge) from day one
- AI and MCP added later as stage 2 adapters with proper architecture review
- SaaS features added by extending workspace and platform layers, not by breaking the monolith apart too early

That gives you a credible personal product now and a realistic path to a platform later.
