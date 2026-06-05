# Security Checklist (Phase 1/1.1)

This checklist is for release-hardening and regression review on `main`.

## Auth and Session
- [ ] JWT cookie auth flows (`/auth/login`, `/auth/refresh`, `/auth/logout`) validated.
- [ ] Session revocation tested (revoked session cannot call protected endpoints).
- [x] CSRF origin checks enforced on cookie-authenticated mutations.
- [x] Double-submit CSRF token required for cookie-authenticated mutations.

## Authorization and Tenant Isolation
- [ ] Cross-workspace access attempts return non-disclosing errors (`404` where applicable).
- [ ] Write endpoints reject foreign `public_id` references across workspaces.
- [ ] Capture, notification, summary, finance, spending, todo, and investing routes are workspace-scoped.

## Input Validation and Error Safety
- [ ] Request validation returns RFC 7807 problem responses.
- [ ] Invalid frequency/interval/date inputs are rejected with `4xx`, not `5xx`.
- [ ] Scheduler/workflow loops have non-advancing-date safety guards.
- [x] Multipart imports are rejected at request ingress when they exceed `MAX_MULTIPART_BODY_BYTES`.

## Audit and Traceability
- [ ] Mutation paths emit audit logs with before/after snapshots.
- [ ] Sensitive keys are redacted in audit payloads.
- [ ] System workflows that create domain records (e.g., recurring generation) emit audit events.

## Scheduler and Background Safety
- [ ] Scheduler jobs are idempotent or explicitly blocked by scheduler policy.
- [ ] Advisory lock strategy prevents concurrent duplicate execution.
- [ ] Per-workspace error isolation and timeout handling verified.

## Operational Security
- [ ] Secrets are loaded from env (no committed credentials).
- [x] Production startup rejects default `SECRET_KEY` and default dev metrics token.
- [x] Production startup requires secure cookies.
- [x] Production startup requires rate limiting to remain enabled.
- [x] Production startup rejects in-memory rate-limit storage.
- [ ] Rate limiting is enabled for auth endpoints in non-test environments.
- [ ] Security headers middleware active in app startup path.
- [x] CI runs `pip-audit` against installed Python dependencies.
- [x] CI runs Bandit static analysis against `app/`.
- [x] CI runs TruffleHog secret scanning for verified live secrets.
- [x] Local pre-commit checks reject committed private keys.

## Verification Log (2026-06-04)
- Gate 0 security-gate milestone:
  - `uv run --with pip-audit pip-audit --progress-spinner off`
  - `uv run pre-commit run --all-files`
  - CI workflow now includes dependency audit, Bandit, and verified-secret scanning.
- Gate 0 production-config milestone:
  - `uv run pytest app/tests/test_config.py`
  - `ENV=production` fails closed for invalid/default/insecure runtime settings.

## Verification Log (2026-06-04)
- Gate 0 CSRF checks passed:
  - `uv run ruff check app/core/csrf.py app/core/dependencies.py app/auth/router.py app/platform/router.py app/tests/conftest.py app/tests/integration/test_api.py`
  - `uv run pytest app/tests/integration/test_api.py`
  - `uv run pytest app/tests/test_auth_rbac_security.py app/tests/integration/test_platform.py app/tests/integration/test_imports.py app/tests/integration/test_exports.py`

## Verification Log (2026-06-04)
- Gate 0 multipart ingress checks passed:
  - `uv run ruff check app/config.py app/core/middleware.py app/main.py app/tests/integration/test_imports.py`
  - `uv run pytest app/tests/integration/test_imports.py::test_import_rejects_oversized_multipart_before_parsing`
  - `uv run pytest app/tests/integration/test_imports.py`

## Verification Log (2026-05-28)
- Targeted isolation/authz sweep passed:
  - `app/tests/integration/test_spending.py`
  - `app/tests/integration/test_finance.py`
  - `app/tests/integration/test_investing.py`
  - `app/tests/routers/test_todo.py`
  - `app/tests/test_security_hardening.py`
