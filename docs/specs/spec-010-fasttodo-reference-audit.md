# Reference Spec: FastTodo Reference Audit — Patterns Adopted and Rejected

**Status:** Implemented
**Spec ID:** 010
**Scope:** Backend (`lifestack-api`) · Frontend (`lifestack-web`)
**Date:** 2026-05-16

---

## 1. Overview

FastTodo (`sajankp/to-do` + `sajankp/to-do-frontend`) was used as a reference
implementation during the production-readiness hardening phase of Lifestack. This spec
documents every pattern that was evaluated, what was adopted, what was rejected, and why —
so future agents and contributors do not re-evaluate the same ground.

FastTodo has been removed from the active workspace after this audit. It remains on
GitHub and can be referenced via the GitHub MCP server if needed.

---

## 2. Backend (`lifestack-api`)

### 2.1 Adopted

| Pattern | Where in Lifestack | Notes |
|---------|--------------------|-------|
| `is_active` guard at login | `app/auth/service.py` | One-line guard before issuing tokens |
| `StructlogMiddleware` (request context binding) | `app/core/middleware.py` | Binds `request_id`, `sid`, `client_ip`, `method`, `path` per request |
| `X-Request-ID` response header | `app/core/middleware.py` | Part of `StructlogMiddleware` |
| `structlog.configure()` via `setup_logging()` | `app/core/logging.py` | Called at startup via lifespan |
| Protocol-aware HSTS (`X-Forwarded-Proto`) | `app/core/middleware.py` | Checks forwarded proto before setting HSTS header |
| `X-XSS-Protection` header | `app/core/middleware.py` | Added to `SecurityHeadersMiddleware` |
| Configurable CSP via env vars | `app/config.py` + `app/core/middleware.py` | `CSP_IMG_SRC`, `CSP_STYLE_SRC`, `CSP_SCRIPT_SRC`, etc. |
| Rate limiter kill-switch (`RATE_LIMIT_ENABLED`) | `app/core/dependencies.py` + `app/config.py` | Disable in tests without Redis |
| Configurable cookie attributes (`COOKIE_SAMESITE`, `COOKIE_DOMAIN`) | `app/config.py` + `app/auth/router.py` | Applied consistently to login, refresh, and logout |
| Fail-fast DB readiness check on startup | `app/main.py` (lifespan) | `SELECT 1` via SQLAlchemy; blocks startup if DB unreachable |
| `ENV` setting for environment-specific logic | `app/config.py` | Replaces fragile URL-string heuristics for "is local?" checks |

### 2.2 Rejected

| Pattern | Reason |
|---------|--------|
| Transparent password hash migration | Lifestack is a new application — all passwords use Argon2id (pwdlib) from day one. No legacy hashes to migrate. If pwdlib bumps Argon2 params in future, a one-time migration script is the appropriate fix, not online transparent migration. |
| OpenTelemetry tracing (`setup_telemetry`, `PymongoInstrumentor`) | MongoDB-specific. Lifestack uses SQLAlchemy; OTel integration will be specced separately. The `add_trace_context` structlog processor (enriches logs with `trace_id`/`span_id`) is worth adopting when that spec lands. |
| Prometheus metrics (`metrics.py`) | FastTodo metrics are todo-domain specific. Lifestack will define its own domain metrics when an observability spec is approved. |
| MongoDB index management (`database/indexes.py`) | Not applicable — Lifestack uses PostgreSQL with Alembic migrations. |
| `validate_env()` utility | Lifestack's `_check_production_defaults` Pydantic model validator handles this at settings load time. |
| User lookup helper (`utils/user.py`) | Covered by `UserRepository` in Lifestack's layered architecture. |

### 2.3 Agent Tooling (from `to-do/.agent/`)

| Item | Status | Notes |
|------|--------|-------|
| `.agent/workflows/development-workflow.md` | Adopted (lean rewrite) | Trimmed from 474 → ~80 lines; removed ceremony duplicated by `AGENTS.md` and global rules |
| `.agent/workflows/pr-review.md` | Adopted as-is | Fully generic; no FastTodo-specific content |
| `.agent/skills/tdd-fastapi/SKILL.md` | Adapted | Thin process guide; defers code examples to `docs/PATTERNS.md` |

---

## 3. Frontend (`lifestack-web`)

### 3.1 Adopted

| Pattern | Where in Lifestack | Notes |
|---------|--------------------|-------|
| Refresh mutex interceptor | `src/services/api.ts` | Adapted from plain fetch → axios interceptor. Coalesces concurrent 401s into a single `POST /auth/refresh`; retries original request on success. |
| Auth observer pattern (`onUnauthorized`, `onSessionExtended`) | `src/services/api.ts` + `src/App.tsx` | Decouples API layer from React state; `App.tsx` subscribes to `onUnauthorized` to auto-clear session on refresh failure. |
| Vitest + MSW test setup | `vitest.config.ts` + `src/test/setup.ts` | `onUnhandledRequest: 'error'` ensures unintentional network calls fail fast in tests. 6 tests covering interceptor behaviour. |
| Pre-commit hooks (Prettier + conventional commits) | `.pre-commit-config.yaml` + `.prettierrc.json` | Ensures frontend code quality parity with backend. |

### 3.2 Rejected

| Pattern | Reason |
|---------|--------|
| `VoiceAssistant.tsx` | Todo-domain specific. Lifestack will spec its own AI voice integration separately. |
| UI components (Toast, Modal, Button, Input, AuthForm) | Tailwind utility-only, no design system. Lifestack-web will define its own component library per its UI spec. |
| FastTodo `types.ts` | FastTodo domain types (Todo, User). Lifestack has its own type definitions. |
| App shell / routing structure | Lifestack already has react-router-dom routing with protected routes. |

### 3.3 Deferred

| Item | Trigger |
|------|---------|
| `utils/audioUtils.ts` (base64↔PCM audio helpers) | When Lifestack AI voice assistant spec is approved |
| `add_trace_context` structlog processor (OTel trace_id in logs) | When Lifestack observability / OTel spec is approved |

---

## 4. Verification

All adopted backend patterns are covered by `app/tests/test_security_hardening.py` (6 tests).
All adopted frontend patterns are covered by `src/services/api.test.ts` (6 tests).
