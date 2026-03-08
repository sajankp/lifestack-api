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
Even for a single personal user, all business tables should carry a `workspace_id`. In stage 1 there may be one workspace per user. Later, the same shape supports teams, households, and SaaS plans.

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
|-- to-do
`-- to-do-frontend
```

The older todo app remains a useful reference, especially for auth and product behavior, but Lifestack should become the new primary codebase.

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
| Auth | JWT access + refresh flow retained from the existing todo app |
| Scheduler | APScheduler |
| Background work | In-process jobs first, DB-backed outbox later if needed |
| AI | Optional stage 2 provider layer |
| MCP | Optional stage 2 adapter |
| Testing | pytest |
| Linting | Ruff |
| CI | GitHub Actions |
| Containerization | Docker + Docker Compose |

### Why keep JWT auth?

The existing todo app already uses JWT-based auth. Reusing that model gives you:
- continuity with the current app
- less rewrite risk
- a clearer migration path into Lifestack
- an API shape that still works later for mobile clients, MCP clients, or external integrations

If you already have refresh-token rotation in the todo app, keep it. If not, add it inside the auth module rather than redesigning auth again.

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
|   |   |-- models.py
|   |   |-- schemas.py
|   |   `-- tests/
|   |
|   |-- investing/
|   |   |-- router.py
|   |   |-- service.py
|   |   |-- repository.py
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
|   |   |-- workspaces.py
|   |   |-- memberships.py
|   |   |-- exports.py
|   |   `-- usage.py
|   |
|   |-- ai/          # stage 2
|   `-- mcp/         # stage 2
|
|-- alembic/
|-- docs/
|-- Dockerfile
|-- docker-compose.yml
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

### Stage 1

Retain JWT auth from the existing todo app:
- short-lived access token
- refresh token flow
- user-scoped and workspace-scoped access checks

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

## Audit and Export

These are worth keeping early because they support the personal-OS story well.

### Audit log

Use an append-only audit table for mutations that matter:
- todo create/update/complete
- transaction create/update/delete
- holding create/update/delete
- exports generated

Audit writes should happen in the same transaction boundary as the business change where possible.

### Export

Export is a strong feature for a personal tool:
- CSV for analysis
- JSON for backups or migrations

It also reinforces trust better than adding AI too early.

---

## AI and MCP

These should be framed as adapters over stable domain services.

### Stage 2 design

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

That is enough for the personal OS.

### Stage 2 or later

Add Redis only if you actually need:
- distributed rate limiting
- worker queues
- cache invalidation
- pub/sub fan-out

Do not make Redis mandatory before one of those needs is real.

---

## Build Phases

### Phase 1 - Personal OS Foundation
- scaffold `lifestack-api` as a modular monolith
- carry JWT auth forward from the existing todo app
- move todo into the shared platform codebase
- build spending and investing modules
- add dashboard read model
- add audit logging
- add export
- add scheduler-based reminders and recurring jobs

### Phase 2 - AI and Integrations
- add AI provider abstraction
- add chat UI
- add usage tracking and rate limits
- add MCP as an optional adapter
- document MCP auth only when implemented

### Phase 3 - SaaS Expansion
- extend workspaces with multi-user memberships, roles, and team features
- add quotas, billing, and admin dashboard
- add workers or message infrastructure if justified by workload

---

## What Needs Extra Clarity in the Docs

These points should stay explicit across README and architecture docs:
- what works today vs what is planned
- personal OS first, SaaS later
- JWT auth is intentional because it comes from the existing todo app
- MCP is not part of the core architecture yet
- scheduler and direct workflows are the default coordination model
- Pub/Sub is optional, not foundational
- `workspace_id` is the migration path to SaaS

---

## Bottom Line

For this project, the right architecture is:
- a tenant-aware modular monolith
- JWT auth retained from the current todo app
- PostgreSQL as the source of truth
- scheduler plus direct workflows for stage 1
- AI and MCP added later as adapters
- SaaS features added by extending workspace and platform layers, not by breaking the monolith apart too early

That gives you a credible personal product now and a realistic path to a platform later.
