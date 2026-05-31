# Feature Spec: API and Database Security Remediation
**Status:** Proposed
**Spec ID:** 025

## 1. Overview
This specification details the technical designs for addressing the Critical and High-priority backend audit findings. It establishes a robust role-based access control (RBAC) mechanism, secures session/websocket authentication, validates HTTP forwarded protocols, prevents username timing attacks, sanitizes import inputs, and schedules the weekly summary workflow.

## 2. Requirements

### 2.1 Role-Based Access Control (RBAC)
- All workspace operations must be governed by user roles: `owner`, `admin`, `member`, `viewer` (defined in `WorkspaceRole`).
- **Hierarchy Ranks**:
  - `owner` (Rank 4): Full permissions, including workspace modifications and member deletion.
  - `admin` (Rank 3): Can modify settings and manage database accounts.
  - `member` (Rank 2): Standard read/write feature operations (e.g. log transactions, manage todos, create budgets).
  - `viewer` (Rank 1): Read-only (GET requests) for all modules.
- **Enforcement**:
  - A decorator/dependency `require_min_role(role)` must be applied to all mutating API endpoints.
  - Workspace memberships must be validated by checking the role in the database.

### 2.2 X-Forwarded-Proto Security & HSTS
- Trust the `X-Forwarded-Proto` header for HSTS checking only if the request originates from a trusted proxy client IP.
- Add setting `TRUSTED_PROXIES` (list of strings, e.g. `["127.0.0.1", "::1", "testclient"]`).

### 2.3 User Token Refresh & Active Status
- The token `/refresh` endpoint must check `user.is_active` before issuing new access/refresh tokens.
- Deactivated/disabled users must immediately fail to refresh.

### 2.4 Username & Password Input Validation
- Update `UserCreate` Pydantic schemas to validate fields before database insertions:
  - `username`: 3 to 50 characters, containing only letters, numbers, underscores, or hyphens (`^[a-zA-Z0-9_-]+$`).
  - `password`: at least 8 characters.

### 2.5 Timing Attack Mitigation (User Authentication)
- In `AuthService.authenticate_user`, if a username or email is not found, verify a static precomputed Argon2id dummy hash (`$argon2id$v=19$m=65536,t=3,p=4$GOmQ3l1jgCgnsSr1XaQO4A$cuP2ZOCQDzD6pisbkLxr1toLEOhywb1hu1xaLVP4v2U`) to consume constant verification time.

### 2.6 Path Traversal & Import Security
- Sanitize the `filename` attribute in `ImportBatch` and storage key paths using `Path(filename).name` to strip directory traversal payloads (`../`).

### 2.7 WebSocket Authentication Hardening
- Remove token query-parameter extraction from `/capture/agent/ws`. Accept only HttpOnly cookie `access_token` for authentication.

### 2.8 CORS Hardening
- When `allow_credentials=True`, list explicit HTTP methods (`["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]`) rather than wildcard `["*"]`.

### 2.9 Database Constraints & Migrations
- Add a unique constraint `uq_workspace_membership_workspace_user` on `WorkspaceMembership` table for `(workspace_id, user_id)` columns. Create an Alembic migration.

### 2.10 Scheduler Integration
- Schedule the proposed `weekly_summary_job` to run every Monday at 01:30 UTC. Register the job under the application lifespan using the APScheduler manager.

## 3. Implementation Details

1. **RBAC Dependency**:
   - Create `get_current_membership` in `app/core/dependencies.py` which retrieves the membership row for the current user and active workspace.
   - Implement `require_min_role(min_role: WorkspaceRole)`:
     ```python
     def require_min_role(min_role: WorkspaceRole):
         async def dependency(membership = Depends(get_current_membership)):
             if ROLE_RANK[membership.role] < ROLE_RANK[min_role]:
                 raise ForbiddenError(detail="Insufficient workspace permissions")
             return membership
         return dependency
     ```
2. **Timing Attack**:
   - Store `DUMMY_PASSWORD_HASH` statically and invoke `verify_password(password, DUMMY_PASSWORD_HASH)` inside `AuthService.authenticate_user` if user record is missing.

## 4. Testing Plan
- Unit tests verifying the custom dependency role ranks.
- Integration tests targeting the refresh tokens with disabled users.
- Verification tests checking filename normalization in import validators.
