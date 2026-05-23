# Lifestack

> A personal operating system for tasks, money, and investments.

Lifestack is an open-source personal productivity and financial platform. It starts as a personal OS for one user or one household, with a structure that can later grow into a SaaS product without a rewrite.

The core idea is simple: tasks, spending, and investing should not live in separate silos. They should share one data model, one auth system, one dashboard, and eventually one AI interface.

---

## Product Direction

Lifestack is being built in three stages:

### Stage 1: Personal OS
- One frontend, one API, one PostgreSQL database
- JWT-based auth (HttpOnly cookies) carried forward from the existing todo app
- Todo, spending, investing, dashboard, exports, reminders
- Cross-module workflows handled by application services and scheduled jobs

### Stage 2: AI and MCP
- AI chat as an adapter over existing services
- Optional MCP tools for external clients
- Provider abstraction, BYOK, usage tracking, rate limiting

### Stage 3: SaaS
- Multi-workspace and multi-user support
- Roles, billing, quotas, admin features
- Background workers and message infrastructure only if scale or product needs justify them

This keeps the first version small and credible while preserving a clean migration path later.

---

## What It Does

### Dashboard
A unified home page showing tasks due, budget status, and portfolio performance at a glance.

### Todo
A fast task manager with priorities, due dates, and a clean service-layer architecture. This module is the continuation of the earlier todo app, now folded into the larger Lifestack platform.

### Spending Tracker
Track transactions, budgets, and monthly spending patterns.

### Investment Tracker
Track holdings, performance, and portfolio-level changes over time.

### Cross-Module Workflows
The differentiator is not just having three modules. It is making them work together:
- overspending can create a review task
- a rebalance check can surface on the dashboard
- weekly summaries can combine productivity and finance data

### AI Chat and MCP
These are planned interface layers, not the foundation of the product. The core app should be useful without them. They will sit on top of the existing service layer in a later stage.

---

## Architecture Summary

**Backend (`lifestack-api`)**
- FastAPI + Python 3.13
- PostgreSQL for all modules
- Layered architecture: router -> service -> repository
- JWT auth based on the existing todo app's auth model
- APScheduler for reminders, recurring checks, and scheduled summaries
- Alembic for schema migrations
- Audit logging and data export
- Optional later additions: AI layer, MCP tools, outbox/background workers

**Frontend (`lifestack-web`)**
- React 19 + TypeScript + Vite
- React Router
- Zustand + TanStack Query
- Recharts for portfolio and spending views
- Shared UI and module-based sections

---

## Architecture in One Diagram

```text
lifestack-web (React)
      |
      +-- /            -> Dashboard
      +-- /todo
      +-- /spending
      +-- /investing
      +-- /settings
      `-- /chat        -> optional stage 2
               |
               v
        lifestack-api (FastAPI)
               |
               +-- /auth
               +-- /todo
               +-- /spending
               +-- /investing
               +-- /dashboard
               +-- /platform
               |
               +-- application services
               |      `-- cross-module workflows
               |
               +-- scheduler
               |      `-- reminders, recurring tasks, weekly summaries
               |
               `-- PostgreSQL

optional later:
- AI provider layer
- MCP adapter
- background jobs / outbox worker
- multi-workspace SaaS features
```

The core rule is: business logic lives in services, cross-module orchestration lives in application workflows, and adapters like chat or MCP call into that same layer instead of bypassing it.

---

## Why This Shape

- It is small enough for one person to build and maintain.
- It keeps the architecture honest for a showcase project.
- It avoids introducing distributed-system complexity before it is needed.
- It still supports a clean path to SaaS later through workspace scoping and modular boundaries.

---

## Features at a Glance

| Feature | Status |
|---|---|
| Todo CRUD with priorities and workspace scoping | ✅ Done |
| JWT auth (HttpOnly cookies, session tracking, CSRF) | ✅ Done |
| Spending tracker (categories, transactions, budgets) | ✅ Done |
| Unified dashboard | ✅ Done |
| Audit logging — in-transaction, append-only, PII-redacted | ✅ Done |
| Scheduler infrastructure (APScheduler, gating, advisory lock) | ✅ Done |
| Budget guardrails workflow (system todos, idempotency, auto-resolve) | ✅ Done |
| Investing module | ⏳ Next |
| Recurring transactions scheduler workflow | ⏳ Planned |
| Data export (CSV / JSON) | ⏳ Planned |
| AI chat | Stage 2 |
| MCP tools | Stage 2 |
| BYOK and provider abstraction | Stage 2 |
| Multi-workspace / SaaS platform layer | Stage 3 |

---

## Technical Debt & Future Architecture Steps

Based on architectural reviews and implementation, the following items are tracked:

1. **JWT Workspace Caching:** `workspace_id` is resolved via database lookup on every authenticated request. In Stage 2, `default_workspace_id` should be embedded in the JWT payload to eliminate this N+1 latency.
2. **Currency Serialization Strictness:** Pydantic serialization of `NUMERIC(12,2)` (Decimals) should explicitly cast to strings over the wire to prevent JavaScript floating-point rounding errors.
3. **Investing Module Audit Logging:** The investing module will receive audit logging at implementation time — it is intentionally deferred to keep audit scope consistent with implemented modules.
4. **Scheduler: Rolling Deploy Window:** The Postgres advisory lock prevents duplicate execution but does not guarantee *exactly-once* delivery if both instances acquire the lock sequentially within the same run window. Acceptable for stage 1 (idempotent workflows), but should be re-evaluated before scheduling non-idempotent jobs.

---

## Running Locally

```bash
# Clone both repos
git clone https://github.com/sajankp/lifestack-api
git clone https://github.com/sajankp/lifestack-web

# Backend
cd lifestack-api
cp .env.example .env
docker-compose up

# Frontend
cd ../lifestack-web
npm install
npm run dev
```

API docs: `http://localhost:8000/docs`

MCP integration is intentionally not documented here until its auth and usage flow are finalized.

---

## Open Source

Lifestack is open source under the Apache 2.0 license.

The goal is to build a real personal system in public, show the architecture decisions behind it, and grow it carefully instead of overscoping it from day one.

---

## Repos

| Repo | Description |
|---|---|
| [lifestack-api](https://github.com/sajankp/lifestack-api) | FastAPI backend for auth, todo, spending, investing, dashboard, and future AI adapters |
| [lifestack-web](https://github.com/sajankp/lifestack-web) | React frontend for the personal OS experience |

---

## Related

This project grows out of [FastTodo](https://github.com/sajankp/to-do), which provides the initial JWT auth direction and the original todo module foundation.

---

*Built by [@sajankp](https://github.com/sajankp)*
