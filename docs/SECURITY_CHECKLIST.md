# Security Checklist (Phase 1/1.1)

This checklist is for release-hardening and regression review on `main`.

## Auth and Session
- [ ] JWT cookie auth flows (`/auth/login`, `/auth/refresh`, `/auth/logout`) validated.
- [ ] Session revocation tested (revoked session cannot call protected endpoints).
- [ ] CSRF origin checks enforced on cookie-authenticated mutations.

## Authorization and Tenant Isolation
- [ ] Cross-workspace access attempts return non-disclosing errors (`404` where applicable).
- [ ] Write endpoints reject foreign `public_id` references across workspaces.
- [ ] Capture, notification, summary, finance, spending, todo, and investing routes are workspace-scoped.

## Input Validation and Error Safety
- [ ] Request validation returns RFC 7807 problem responses.
- [ ] Invalid frequency/interval/date inputs are rejected with `4xx`, not `5xx`.
- [ ] Scheduler/workflow loops have non-advancing-date safety guards.

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
- [ ] Rate limiting is enabled for auth endpoints in non-test environments.
- [ ] Security headers middleware active in app startup path.

## Verification Log (2026-05-28)
- Targeted isolation/authz sweep passed:
  - `app/tests/integration/test_spending.py`
  - `app/tests/integration/test_finance.py`
  - `app/tests/integration/test_investing.py`
  - `app/tests/routers/test_todo.py`
  - `app/tests/test_security_hardening.py`
