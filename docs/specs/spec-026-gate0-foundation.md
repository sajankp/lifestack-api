# Feature Spec: Gate 0 — Foundation Hardening
**Status:** Approved
**Spec ID:** 026

## 1. Overview

Gate 0 is the pre-requisite milestone before any new modules (health, documents, MCP, agent access) are built. It makes the current product secure, trustworthy, testable, and clean enough to support future high-trust personal data.

This spec covers all five waves from the `09-next-execution-plan.md`:

1. **Wave 1** — Security and Trust (P0)
2. **Wave 2** — Reliability and Test Confidence (P1)
3. **Wave 3** — Product UX Cleanup (P1)
4. **Wave 4** — Data Lifecycle and Ownership (P1)
5. **Wave 5** — Finance Correctness (P1/P2)

---

## 2. Wave 1: Security and Trust

### 2.1 Password Policy Validation

Strengthen `UserCreate` and `PasswordChange` Pydantic schemas with a password complexity validator:
- Minimum 8 characters (already enforced)
- At least one uppercase letter
- At least one lowercase letter
- At least one digit
- At least one special character (`!@#$%^&*()_+-=[]{}|;:,.<>?`)

Apply the same validator to both registration and password change.

### 2.2 Inactive Workspace Access Rule

`get_current_workspace_id` resolves and stores the workspace. Add a check: if `workspace.is_active == False`, raise `ForbiddenError("Workspace is inactive")`.

Add a `get_workspace_active` method to `WorkspaceRepository` returning the full workspace row.

### 2.3 Block Inactive Users on Refresh

The `/auth/refresh` endpoint already checks `user.is_active`. Document and add explicit integration test to prove inactive users cannot refresh tokens.

Also validate that the refreshed workspace is still active.

### 2.4 RBAC on Notifications and Dashboard Routers

`notifications/router.py` uses `get_current_workspace_id` but does not call `require_min_role`. Add:
- All mutating endpoints (PATCH, POST, DELETE): `require_min_role("member")`
- Read endpoints: no minimum role required (viewers can read)

`dashboard/router.py` — audit for RBAC, add where appropriate.

### 2.5 RBAC Tests

Prove member vs admin vs viewer differences:
- `viewer` cannot call mutating endpoints (expect 403)
- `member` can call member-level mutating endpoints (expect 2xx)
- `admin` can call admin-level finance endpoints (expect 2xx)
- `member` cannot call admin-level finance endpoints (expect 403)

### 2.6 X-Forwarded-Proto / HSTS (Already Implemented)

The middleware already enforces `TRUSTED_PROXIES`. Test verifies behavior. No code change needed — add explicit test for spoofed header from untrusted client.

---

## 3. Wave 2: Reliability and Test Confidence

### 3.1 Focused Test Suites

- `app/tests/notifications/` — test RBAC (viewer vs member), mute, mark-all-read
- `app/tests/capture/` — expand WebSocket auth tests, role restriction
- `app/tests/summaries/` — test role filtering (viewer can list, viewer cannot write if applicable)
- Cross-workspace integrity: two users cannot see each other's notifications, summaries, captures

### 3.2 Dependency Audit Gate

Add `pip-audit` to CI/pre-commit via a safety check in `.pre-commit-config.yaml`.

---

## 4. Wave 3: Product UX Cleanup

### 4.1 lifestack-web

- Responsive mobile navigation (hamburger menu below 768px breakpoint)
- Show real workspace name and user role in the UI header
- Visible error states for voice/capture WebSocket failures
- Better empty states with a clear primary action
- Route-level loading skeletons using CSS animations
- Continue decomposing large page components

---

## 5. Wave 4: Data Lifecycle and Ownership

### 5.1 Import Rollback / Delete

Add `DELETE /v1/imports/{batch_id}` endpoint that:
- Marks the import batch as deleted
- Cascades soft-delete to imported transaction records where `source_import_id` matches
- Returns count of deleted records

### 5.2 Export TTL / Delete

Add `DELETE /v1/exports/{export_id}` endpoint that:
- Deletes or marks the export row as expired
- Removes the physical file if local backend

### 5.3 Source Metadata Conventions

Add `source_type` enum field to transactions: `manual`, `imported`, `synced`, `assistant`.
Add Alembic migration.

Initial implementation scope:
- Manual spending transactions default to `source_type=manual`.
- Imported spending transactions use `source_type=imported`, retain an internal `source_import_id`, and expose `source_type` in transaction responses.
- `source_ref` is reserved for stable external references such as import row keys, future health/device sync IDs, document extraction references, or assistant run IDs.
- Ordinary client-created manual transaction requests must not be allowed to spoof source metadata.

---

## 6. Wave 5: Finance Correctness

### 6.1 Transfer Arithmetic Validation

In `CapitalTransferService.create_transfer`, validate:
- `from_account_id != to_account_id`
- `amount > 0`
- If same currency: `from_amount == to_amount`
- If different currencies: `fx_rate` must be provided and positive

### 6.2 Investing Bulk Price Submission Bounds

In `HoldingService.submit_prices` (or equivalent), validate:
- Each price is `> 0` and `<= 1_000_000` per unit
- Reject batches over 500 items

---

## 7. Test Strategy

Each Wave 1 security test must:
1. Set up the precondition (register, set role, deactivate)
2. Attempt the operation
3. Assert expected HTTP status and RFC 7807 response format

Tests are written first (Red phase) then implementation makes them Green.

---

## 8. Acceptance Criteria (Gate 0 Done)

- [ ] RBAC and inactive access rules enforced and tested
- [ ] Auth: password policy, password change, logout-all-sessions, safe refresh
- [ ] Existing test suite remains green
- [ ] New test suites for notifications, capture, summaries
- [ ] Mobile navigation usable at 375px–768px widths
- [ ] Import delete and export delete work end-to-end
- [ ] Finance transfer arithmetic validated
- [ ] Source metadata field on transactions
- [ ] READMEs up to date
