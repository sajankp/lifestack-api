# Lifestack

> A private personal operating system for actions, money, health, documents, and memory.

Lifestack is an open-source personal operating system for one user or one household. It is designed to unify the parts of life that usually end up fragmented across separate apps, documents, trackers, and inboxes, while preserving a path to broader platform features later.

The core idea is simple: tasks, spending, investing, health, documents, and personal knowledge should not live in separate silos. They should share one data model, one auth system, one dashboard, and eventually one AI interface.

---

## Product Principles

- Own your data end to end. Personal data should remain portable, inspectable, and useful without being trapped inside disconnected tools.
- Capture should be easier than forgetting. Voice, mobile, documents, and lightweight flows should reduce friction at the moment life happens.
- AI is an interface, not the foundation. The assistant should act through clear product capabilities and structured data, not replace the system itself.
- Every module should strengthen the same operating loop. Tasks, money, health, documents, and memory should help users notice, decide, and act in one place.
- Start personal, then expand. Lifestack should become truly useful for one person or one household before it grows into a broader platform.
- Add complexity only when it earns its keep. New infrastructure, automation, and platform features should follow real product need, not lead it.

---

## Product Direction

Lifestack is being built in stages, with the goal of becoming a private personal operating system before it ever becomes a SaaS product:

### Stage 1: Personal OS Foundation
- One frontend, one API, one PostgreSQL database
- JWT-based auth (HttpOnly cookies) carried forward from the existing todo app
- Todo, spending, investing, dashboard, exports, reminders
- Cross-module workflows handled by application services and scheduled jobs

### Stage 2: Capture Layer
- Fast capture for todo, spending, and journal entries
- Simple voice-first input for core actions
- Mobile-friendly flows for quick logging and review

### Stage 3: AI Assistant Interface
- AI adapter over existing services, not a replacement for them
- Voice and chat actions such as creating todos, logging spending, and summarizing state
- Function-calling orchestration grounded in existing module APIs

### Stage 4: Mobile Companion
- Companion mobile app with the same core model and visual language
- Notifications, quick capture, camera upload, and background sync
- Personal-device-first experience for daily use

### Stage 5: Health Module
- Health tracking for labs, vitals, medications, workouts, sleep, and symptoms
- Longitudinal health records owned by the user end to end
- Shared dashboard and workflow integration with the rest of the system

### Stage 6: Document Intelligence
- Document ingestion for receipts, statements, reports, prescriptions, and forms
- Structured extraction into the right modules where confidence is high
- Searchable source documents linked back to normalized records

### Stage 7: Memory and Second Brain
- Journal, notes, documents, and activity combined into a personal knowledge layer
- Timeline and context retrieval across life domains
- AI summaries and planning grounded in personal data, not generic chat history

### Stage 8: SaaS
- Multi-workspace and multi-user support
- Roles, billing, quotas, and admin features
- Optional external-facing MCP/integration layers and heavier background infrastructure if product scale justifies them

This keeps the first versions focused on personal usefulness, tight feedback loops, and end-to-end data ownership before platform expansion.

---

## What It Does

### Dashboard
A unified home page showing what needs attention across tasks, money, health, and longer-term planning.

### Todo
A fast task manager with priorities, due dates, and a clean service-layer architecture. This module is the continuation of the earlier todo app, now folded into the larger Lifestack platform.

### Spending Tracker
Track transactions, budgets, and monthly spending patterns.

### Investment Tracker
Track holdings, performance, and portfolio-level changes over time.

### Health
Track lab results, vitals, medications, workouts, sleep, symptoms, and other health records as part of the same personal system.

### Documents
Ingest receipts, statements, reports, and other documents so the system can extract structured data and keep the original source material linked.

### Journal and Memory
Capture notes, reflections, and events in a way that can later support timelines, reviews, and assistant context.

### Cross-Module Workflows
The differentiator is not just having three modules. It is making them work together:
- overspending can create a review task
- a rebalance check can surface on the dashboard
- weekly summaries can combine productivity and finance data
- a lab result can trigger a health follow-up reminder
- a document upload can populate structured data in the right module

### AI Chat and MCP
These are planned interface layers, not the foundation of the product. The core app should be useful without them. They will sit on top of the existing service layer in later stages, starting with low-friction capture and assistant actions.

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
- Optional later additions: health, documents, journal/memory, AI layer, MCP tools, outbox/background workers

**Frontend (`lifestack-web`)**
- React 19 + TypeScript + Vite
- React Router
- Zustand + TanStack Query
- Shared UI and module-based sections across core product domains

---

## Architecture in One Diagram

```text
lifestack-web (React)
      |
      +-- /            -> Dashboard
      +-- /todo
      +-- /spending
      +-- /investing
      +-- /health      -> later stage
      +-- /documents   -> later stage
      +-- /journal     -> later stage
      `-- /login, /register
               |
               v
        lifestack-api (FastAPI)
               |
               +-- /auth
               +-- /todo
               +-- /spending
               +-- /investing
               +-- /dashboard
               +-- /health      -> later stage
               +-- /documents   -> later stage
               +-- /memory      -> later stage
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
- mobile companion app
- background jobs / outbox worker
- multi-workspace SaaS features
```

The core rule is: business logic lives in services, cross-module orchestration lives in application workflows, and adapters like chat or MCP call into that same layer instead of bypassing it.

---

## Why This Shape

- It is small enough for one person to build and maintain.
- It keeps the architecture honest for a showcase project.
- It avoids introducing distributed-system complexity before it is needed.
- It still supports a clean path to richer personal domains and to SaaS later through workspace scoping and modular boundaries.

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
| Investing module (Spec 008 baseline) | ✅ Done |
| Investing currency/account governance + FX + transfer ledger (Spec 011) | ✅ Done |
| Look-through exposure + overlap analytics APIs (Spec 012 backend) | ✅ Done |
| Recurring transactions scheduler workflow (Spec 013) | ✅ Done |
| Recurring todo rules + scheduler generation (Spec 019) | ✅ Done |
| Notifications inbox + preferences (Spec 015, in-app) | ✅ Done |
| Weekly summaries API + dashboard integration (Spec 016) | ✅ Done |
| Spending analytics endpoints (Spec 017) | ✅ Done |
| Quick capture API routing (Spec 018) | ✅ Done |
| Data export (CSV / JSON) | ✅ Done |
| Voice-first capture for todos and spending | Stage 2 |
| AI assistant over existing modules | Stage 3 |
| Mobile companion app | Stage 4 |
| Health module | Stage 5 |
| Document ingestion and extraction | Stage 6 |
| Journal / second-brain memory layer | Stage 7 |
| MCP tools | Later-stage interface layer |
| BYOK and provider abstraction | Later-stage AI infrastructure |
| Multi-workspace / SaaS platform layer | Stage 8 |

---

## Technical Debt & Future Architecture Steps

Based on architectural reviews and implementation, the following items are tracked:

1. **Scheduler: Rolling Deploy Window:** Advisory locks still do not provide strict exactly-once delivery semantics. As a hard guardrail, non-idempotent scheduler jobs are now blocked unless `SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS=true` is explicitly set.
2. **Cross-repo full-stack E2E test harness:** FE and BE are separate repos; true UI+API+DB end-to-end tests should be hosted in a dedicated integration repo (planned scope item).

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
