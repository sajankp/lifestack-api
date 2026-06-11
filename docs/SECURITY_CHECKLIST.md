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
- [x] Voice capture WebSocket sessions enforce frame, cumulative byte, duration, and text-size limits.
- [x] Voice provider failures return sanitized client-facing errors.

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
- [ ] Production container runs without Uvicorn reload mode.
- [ ] Production container runs as non-root `appuser`.
- [ ] Production container exposes a `/health` Docker `HEALTHCHECK`.
- [x] CI runs `pip-audit` against installed Python dependencies.
- [x] CI runs Bandit static analysis against `app/`.
- [x] CI runs TruffleHog secret scanning for verified live secrets.
- [x] Local pre-commit checks reject committed private keys.

## Verification Log (2026-06-11)
- Gate 0 auth/session follow-up checks passed:
  - `uv run pytest app/tests/test_auth_rbac_security.py::test_inactive_user_cannot_use_existing_access_token app/tests/test_auth_rbac_security.py::test_password_change_clears_current_session_cookies app/tests/test_security_hardening.py::test_refresh_token_rotation_and_reuse_detection -q`
  - `uv run pytest app/tests/test_auth_rbac_security.py app/tests/test_security_hardening.py app/tests/integration/test_api.py -q`
- Gate 0 capture/voice resource-ceiling checks passed:
  - `uv run ruff check app/capture/agent.py app/tests/capture/test_agent.py app/tests/capture/test_capture_router.py app/tests/integration/test_capture.py app/config.py`
  - `uv run pytest app/tests/capture app/tests/integration/test_capture.py -q`
- Full backend verification passed after auth/session, capture/voice, and dependency extraction changes:
  - `uv run pre-commit run --all-files`
  - `uv run pytest -q` (`271 passed`)

## Verification Log (2026-06-05)
- Container runtime hardening reconciled against `Dockerfile`:
  - `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]`
  - `USER appuser`
  - `HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 CMD curl -f http://localhost:8000/health || exit 1`

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
