# Lifestack

> A finance-led personal operations command center for actions and money, with a clear path to health, documents, memory, and agent access later.

Lifestack is an open-source personal operating system for one user or one household. The current product is intentionally finance-led: spending, investing, tasks, imports/exports, notifications, and summaries form the working demo surface while higher-trust domains stay clearly staged for later.

The core idea is simple: tasks, spending, investing, health, documents, and personal knowledge should not live in separate silos. They should share one data model, one auth system, one dashboard, and eventually one AI interface. Today, that loop is anchored in money and action management.

The long-term wedge is a calm daily briefing: what needs attention today, what changed across money and health, which documents or reports need follow-up, and what the personal coach recommends with visible source context.

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
- Todo, spending, investing, dashboard, imports, exports, reminders, workspace RBAC, and safe demo reset
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
- Health-app/device sync foundation for Apple Health, Google Health Connect, or equivalent providers
- Personal-device-first experience for daily use

### Stage 5: Health Module
- Health tracking for labs, vitals, medications, workouts, sleep, weight, and symptoms
- Medication tracker and reminder flows built on the existing scheduler and notifications model
- Longitudinal health records owned by the user end to end
- Shared dashboard and workflow integration with the rest of the system

### Stage 6: Document Intelligence
- Document ingestion for receipts, statements, reports, prescriptions, and forms
- Structured extraction into the right modules where confidence is high
- Searchable source documents linked back to normalized records

### Stage 7: Memory and Second Brain
- Journal, notes, documents, health records, and activity combined into a personal knowledge layer
- Timeline and context retrieval across life domains
- Source-backed retrieval and AI summaries grounded in personal data, not generic chat history
- Permissioned MCP and agent-access surfaces for trusted external assistants
- Personal coach workflows that can reason across actions, money, health, documents, and memory

### Stage 8: SaaS
- Multi-workspace and multi-user support
- Roles, billing, quotas, and admin features
- Heavier background infrastructure only if product scale justifies it

This keeps the first versions focused on personal usefulness, tight feedback loops, and end-to-end data ownership before platform expansion.

---

## What Works Today

### Dashboard
A unified home page showing what needs attention across tasks, money, notifications, summaries, and longer-term planning.

### Todo
A fast task manager with priorities, due dates, and a clean service-layer architecture. This module is the continuation of the earlier todo app, now folded into the larger Lifestack platform.

### Spending Tracker
Track transactions, budgets, and monthly spending patterns.

### Investment Tracker
Track account-backed holdings, cash balances, FX conversion, performance snapshots, and portfolio-level changes over time.

### Capture, Notifications, Summaries, Imports, and Exports
Capture todo and spending intents, receive in-app notifications, review weekly summaries, import CSV data, and export workspace data.

### Cross-Module Workflows
The differentiator is not just having three modules. It is making them work together:
- overspending can create a review task
- a rebalance check can surface on the dashboard
- weekly summaries can combine productivity and finance data

### AI Chat and MCP
These are planned interface layers, not the foundation of the product. The core app should be useful without them. Chat and voice should act through existing services; MCP should expose permissioned personal context to trusted external agents without making any one model vendor the source of truth.

---

## Future Product Tracks

These tracks are planned after completing Gate 0: Product Foundation Stabilization to ensure the system can protect personal data, run reliably, and support mobile-first capture.

The roadmap uses human-readable eras: Foundation, Mobile Companion, Health Memory, Health Sync, Document Intelligence, Source-Backed Second Brain, Agent Access, and Personal Coach.

### Mobile Companion and Sync
- Mobile app for quick capture, notifications, camera upload, and background sync.
- Health-app sync through the mobile app for sleep, weight, workouts, and other supported metrics.
- Explicit source metadata so manual entries, device sync, and document extraction remain distinguishable.

### Health, Medication, and Workout Tracking
- Manual health MVP first: weight, sleep, workouts, vitals, symptoms, medications, and labs.
- Medication tracker with schedules, refill notes, adherence history, and reminders.
- Workout tracker for strength/cardio sessions, recovery notes, and trend review.
- Health dashboard summaries and follow-up tasks generated through normal workflow services.

### Documents and Source-Backed Memory
- Document upload for receipts, statements, prescriptions, lab reports, and forms.
- Structured extraction only when confidence is high, with source links back to the original file.
- Retrieval-augmented search over documents, notes, and normalized records with citations.

### Personal Coach
- A coach layer over structured data, not a standalone chatbot.
- Planning and recommendations grounded in todos, finance state, health metrics, medication adherence, workouts, documents, and journal/memory context.
- Clear permission boundaries, audit trails, and exportability before broader AI automation.

### MCP and Agent Access
- Permissioned MCP tools can let trusted external agents read selected preferences, second-brain memory, todos, documents, and finance/health summaries.
- Investment context can be exposed for research workflows such as ETF overlap, underlying company exposure, FX context, reports, and tax-prep inputs.
- MCP should remain an integration surface, not a primary navigation module.

The product strategy and roadmap for this future track lives in [Product Strategy and Roadmap](docs/product/PRODUCT_STRATEGY_AND_ROADMAP.md).

### Trust Model
- Consent controls what data domains sync, retrieval, and coach features may use.
- External agent access is scoped by domain and capability.
- Every imported, synced, extracted, or assistant-used record keeps source metadata.
- Sensitive health and financial changes require user confirmation.
- Document-backed and health-backed answers should cite source records.
- Export and deletion remain core product requirements.

### Candidate Module Parking Lot
Calendar/time, goals/routines, projects, people/relationships, mood/energy, and food/nutrition are captured in the strategy doc as possible later modules. They are intentionally not active roadmap commitments.

### Future Cross-Module Examples
- a medication dose can create a reminder and adherence entry
- a missed sleep target can surface alongside workload and workout intensity
- a lab result can trigger a health follow-up reminder
- a document upload can populate structured data in the right module
- a coach summary can cite the source data it used before suggesting a plan

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
| JWT auth (HttpOnly cookies, session tracking, Origin/Referer CSRF checks, double-submit CSRF token, password policy, password change, logout-all) | ✅ Done |
| Workspace RBAC and workspace selection APIs | ✅ Gate 0 foundation |
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
| Quick capture API routing (Spec 018) | Planned |
| Data import/export lifecycle controls | ✅ Gate 0 foundation |
| Structured source metadata for spending transactions, budgets, and holdings | ✅ Gate 0 foundation |
| Safe deterministic demo reset | ✅ Gate 0 foundation |
| Voice-first capture for todos, spending, and investing cash balance tools | Stage 2 / partial |
| AI assistant over existing modules | Stage 3 / planned |
| Mobile companion app | Stage 4 / planned |
| Health-app sync via mobile | Stage 4-5 / planned |
| Health metrics, medications, and workouts | Stage 5 / planned |
| Document ingestion and extraction | Stage 6 / planned |
| RAG-backed documents and second-brain memory | Stage 7 / planned |
| Personal coach over structured life data | Stage 7+ / planned |
| MCP tools | Later-stage interface layer |
| BYOK and provider abstraction | Later-stage AI infrastructure |
| Multi-workspace / SaaS platform layer | Stage 8 |

---

## Technical Debt & Future Architecture Steps

Based on architectural reviews and implementation, the following items are tracked:

1. **Scheduler: Running Deploy Window:** Advisory locks still do not provide strict exactly-once delivery semantics. As a hard guardrail, non-idempotent scheduler jobs are now blocked unless `SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS=true` is explicitly set.
2. **Cross-repo full-stack E2E test harness:** FE and BE are separate repos, and the dedicated `lifestack-e2e` repo now hosts the real UI+API+DB suite with stack orchestration scripts.
3. **Gate 0 hardening work:** Investing account identity, finance correctness, deterministic demo/reset data, and richer import/source lifecycle coverage have been implemented for the public-demo path. Production config policy now fails closed for invalid `ENV`, default secrets, insecure cookies, disabled rate limiting, and in-memory production rate-limit storage. Finance `NUMERIC` models now keep Decimal annotations through FX rates and capital transfers instead of casting through floats. Import artifact storage keys are generated from workspace/import IDs rather than user-supplied filenames. API CI now runs dependency audit, Bandit static analysis, and verified-secret scanning. Source metadata now exposes a structured response contract for manual and imported transactions, including import batch references and completed import rollback support across spending transactions, spending budgets, and investing holdings. FX rates are globally scoped read-only market data for users, with writes owned by scheduled ingestion/service code. Broader maintainability work remains in module decomposition and E2E harness cleanup.

### Source Metadata Contract

Transaction and record responses now keep the legacy `source_type` and `source_ref` fields and also expose a structured `source_metadata` object. Manual rows identify as `manual_entry`; imported rows identify as `bulk_import` and include the import batch public id, import module, row number when available, and whether rollback is currently supported.

This is the Gate 0 source-trust contract. It covers spending transactions, spending budgets, and investing holdings. Future modules (e.g. health, documents, memory) should reuse this shape before exposing data to document intelligence, second-brain retrieval, or MCP/agent access.

### Deterministic Demo Reset & Seeding

The platform workspace endpoint `POST /v1/platform/workspaces/{workspace_id}/reset-demo` cleans up workspace-scoped demo data and seeds a deterministic fixture. It is intentionally guarded:

- Requires `ENABLE_DEMO_RESET=true`.
- Requires the caller to be an `owner` or `admin` of the target workspace.
- Requires the target workspace to be the caller's active workspace.
- Emits audit events for denied attempts and successful resets.
- The frontend only shows the action when `GET /v1/platform/workspaces/{workspace_id}/reset-demo/status` reports it is allowed, and the confirmation dialog requires typing the active workspace name.

Fixture version `2026-06-10` seeds:

- Accounts: `brokerage` (USD), `wallet` (USD), `eur-wallet` (EUR).
- Budgets: Rent `1500.00`, Food `400.00`, Utilities `200.00`.
- Transactions: monthly rent, groceries, coffee, and salary deposit.
- Investing: AAPL and MSFT holdings, USD brokerage cash, EUR wallet cash, and EUR/USD plus GBP/USD FX rates.
- Tasks and notifications: two review todos and a welcome notification.

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

## Voice Capture Runtime Limits

The `/v1/capture/agent/ws` voice WebSocket is cookie-authenticated and bounded by conservative defaults:

- `CAPTURE_MAX_WS_FRAME_BYTES=262144`
- `CAPTURE_MAX_SESSION_BYTES=15728640`
- `CAPTURE_MAX_SESSION_SECONDS=300`
- `CAPTURE_MAX_TEXT_CHARS=4000`

Provider errors are logged server-side and returned to the client as sanitized retryable messages.

## Local E2E Test Hooks

The `/v1/e2e/...` workflow trigger routes are registered only when `ENABLE_E2E_TEST_HOOKS=true` and `ENV` is `local` or `test`. They exist for the isolated `lifestack-e2e` Docker Compose harness so Playwright can trigger budget guardrails and recurring transaction generation over authenticated HTTP instead of shelling into containers. Production and staging settings reject this flag.

## Bulk Import Storage Configuration

Spec 020 bulk CSV imports support configurable file persistence:

- `IMPORT_STORAGE_BACKEND=none|local|s3`
- `IMPORT_LOCAL_PATH=/tmp/lifestack-imports` (when backend is `local`)
- `MAX_MULTIPART_BODY_BYTES=10485760` rejects oversized multipart requests before upload parsing.
- `IMPORT_S3_*` for S3-compatible providers (AWS S3, MinIO, Cloudflare R2)

Cloudflare R2 can be configured either with `IMPORT_S3_*` or the alias envs:

- `CLOUDFLARE_R2_ENDPOINT`
- `CLOUDFLARE_R2_BUCKET`
- `CLOUDFLARE_R2_REGION`
- `CLOUDFLARE_R2_ACCESS_KEY`
- `CLOUDFLARE_R2_SECRET_KEY`

R2 endpoint format:

- `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`

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
| [lifestack-e2e](https://github.com/sajankp/lifestack-e2e) | Standalone full-stack Playwright suite for API, Web, Postgres, and Redis |

---

## Related

This project grows out of [FastTodo](https://github.com/sajankp/to-do), which provides the initial JWT auth direction and the original todo module foundation.

---

*Built by [@sajankp](https://github.com/sajankp)*
