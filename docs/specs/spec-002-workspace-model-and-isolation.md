# Feature Spec: Workspace Model and Isolation
**Status:** Approved
**Spec ID:** 002

## 1. Overview

Lifestack follows a workspace-scoped architecture from day one. Even in stage 1, where a single user may have a single default workspace, business data must be accessed through `workspace_id` rather than raw `user_id` ownership checks.

This spec defines the required workspace model, request-context behavior, and the minimum integration and E2E scenarios that must pass before workspace-aware modules are considered aligned with the architecture.

## 2. Requirements

### 2.1 Workspace-Owned Data
- Business tables must carry `workspace_id`.
- Repository queries must scope reads and writes by `workspace_id`.
- `user_id` may be stored for ownership or audit purposes, but it must not replace workspace scoping.

### 2.2 Identity Model
- `users` represent authenticated people.
- `workspaces` own business data.
- `workspace_memberships` map users to workspaces and roles.
- In stage 1, each user may be provisioned with one default workspace, but this is still a real workspace model.

### 2.3 Request Context
- Auth middleware must resolve the authenticated user.
- Dependency resolution must resolve the active `workspace_id` for downstream services.
- Route handlers and services must operate on `workspace_id`.
- No module should infer tenancy by querying only `user_id`.

### 2.4 Testing Expectations
- Integration tests must prove workspace isolation explicitly.
- A second authenticated user must not be able to list, fetch, update, or delete records from the first user's workspace.
- Test database setup must use migrations (`alembic upgrade head`), not model-driven schema creation.

## 3. Stage 1 Implementation Guidance

- Stage 1 may resolve the active workspace by selecting the user's default or first available workspace.
- If the implementation uses fallback provisioning of a default workspace, that behavior must be documented and covered by tests.
- This fallback is a stage-1 convenience, not a replacement for the workspace model.

## 4. Integration Scenarios

These scenarios are the minimum integration-level handoff requirements.

### 4.1 User Registration Provisions Workspace
- Setup:
  - empty database
- Action:
  - register a new user
- Expected DB effect:
  - one `users` row exists
  - one `workspaces` row exists
  - one `workspace_memberships` row links the user to the workspace
- Expected API effect:
  - registration succeeds

### 4.2 Authenticated Request Resolves Active Workspace
- Setup:
  - user with one workspace membership
- Action:
  - authenticate and call a workspace-scoped endpoint
- Expected behavior:
  - request succeeds without supplying workspace in the route path
  - downstream repository/service receives the resolved `workspace_id`

### 4.3 Workspace Isolation on List
- Setup:
  - user A with workspace A and at least one todo in workspace A
  - user B with workspace B and at least one todo in workspace B
- Action:
  - authenticate as user A and list todos
  - authenticate as user B and list todos
- Expected behavior:
  - each user sees only records from their own workspace

### 4.4 Workspace Isolation on Direct Entity Fetch
- Setup:
  - user A owns a todo in workspace A
  - user B is authenticated in workspace B
- Action:
  - user B requests user A's todo by `public_id`
- Expected behavior:
  - API returns not found or an equivalent non-leaking response
  - no cross-workspace data is disclosed

### 4.5 Workspace Isolation on Mutation
- Setup:
  - user A owns a todo in workspace A
  - user B is authenticated in workspace B
- Action:
  - user B attempts update and delete operations on user A's todo
- Expected behavior:
  - mutation is rejected via workspace-scoped lookup failure
  - user A's record remains unchanged

### 4.6 Migration-Backed Test Bootstrapping
- Setup:
  - test database container
- Action:
  - initialize schema for integration tests
- Expected behavior:
  - schema is created via Alembic migrations
  - tests do not rely on `create_all()`

## 5. E2E Scenarios

These should be implemented in browser-level or system-level flows once the frontend and fuller app surface exist.

### 5.1 Personal Workspace First-Run Flow
- Starting state:
  - no account exists
- User flow:
  - register
  - sign in
  - create first todo
- Visible outcome:
  - todo appears in the signed-in user's workspace
- Security expectation:
  - workspace context is implicit and does not require the user to choose a tenant in stage 1

### 5.2 Cross-User Isolation
- Starting state:
  - user A and user B both exist with separate workspaces
- User flow:
  - user A creates a todo
  - user B signs in and visits todo views
- Visible outcome:
  - user B cannot see user A's todo
- Security expectation:
  - no direct URL or cached navigation path exposes the other workspace's data

### 5.3 Cross-User Mutation Rejection
- Starting state:
  - user A has existing todo data
  - user B is authenticated separately
- User flow:
  - user B attempts to reach or mutate user A's record through any exposed UI path
- Visible outcome:
  - action fails cleanly
- Security expectation:
  - no unauthorized mutation occurs

## 6. Acceptance Criteria

- Workspace-aware modules scope repository access by `workspace_id`.
- Request dependencies resolve an active workspace for authenticated requests.
- Integration tests prove two-user workspace isolation.
- Test setup is migration-backed.
- Stage-1 default workspace behavior is documented if used.
