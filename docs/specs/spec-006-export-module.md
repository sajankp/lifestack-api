# Feature Spec: Export Module
**Status:** Planned
**Spec ID:** 006

## 1. Overview
Lifestack should allow a workspace member to export personal data in machine-usable formats (JSON and CSV). Export strengthens trust, portability, and backup readiness.

## 2. Goals
- Export workspace-scoped data from todo, spending, and investing domains.
- Provide two formats: JSON (full fidelity) and CSV (analysis friendly).
- Ensure exports are auditable.

## 3. Out of Scope
- Asynchronous large-file generation queues.
- Encryption key management service (deferred from V1).

## 4. Requirements
### 4.1 API Surface
- `POST /v1/exports` create export request.
- `GET /v1/exports/{public_id}` get export metadata/status.
- `GET /v1/exports/{public_id}/download` download generated artifact.

### 4.2 Data Model
Create `exports` table:
- `id` int PK
- `public_id` UUID unique
- `workspace_id` int FK
- `requested_by` int FK user
- `format` enum: `json | csv`
- `schema_version` int
- `scope` JSONB (modules included)
- `status` enum: `pending | ready | failed`
- `storage_key` nullable string (reference to S3-compatible blob or presigned URL. Local ephemeral disk MUST NOT be used in a stateless deployment).
- `error_message` nullable string
- `created_at`, `completed_at`

### 4.3 Security and Isolation
- Export data is always filtered by active `workspace_id`.
- Requester must be authenticated and belong to workspace.
- Download endpoint must reject cross-workspace access with RFC 7807 not-found semantics.

### 4.4 Auditing
- Creating an export request writes audit event `module=export`, `action=export_request`.
- Successful artifact generation writes `action=export_generated`.

## 5. Implementation Notes
- Export orchestration lives in `app/application/workflows.py`.
- Module services/repositories MUST provide normalized row iterators (using async DB cursors) for export building to prevent OOM errors on large workspaces.
- Stage 1 can generate artifacts synchronously for small datasets, but the response MUST either be a `StreamingResponse` yielding chunks directly from the DB, or uploaded immediately to blob storage.
- Stage 1 threshold: synchronous export supports up to 5,000 records per module. Requests above this threshold must fail fast with RFC 7807 (413 or documented equivalent) until async export is introduced.
- Only one active export (`pending`) per workspace is allowed at a time.
- If a selected module is not yet implemented, export returns an empty section for that module and records the omission in metadata.

## 6. Test Plan
- Integration: user creates export and receives downloadable artifact.
- Isolation: user B cannot fetch user A workspace export.
- Failure path: generation failure sets `status=failed` with error message.

## 7. Acceptance Criteria
- Export endpoints available under `/v1`.
- JSON export includes todo, spending, investing sections when selected.
- CSV export produces module-separated CSV files or zipped bundle.
- Audit rows created for request + completion/failure.
- Integration tests cover success, isolation, and failure paths.
- Export metadata persists `schema_version` for compatibility tracking.
- Workspace export concurrency guard prevents multiple simultaneous `pending` exports.

## Observability Hooks
- Emit structured logs for export request, generation start, generation end, download served.
- Emit counters for export request outcomes by format and status.
- Emit histogram for export generation duration.
