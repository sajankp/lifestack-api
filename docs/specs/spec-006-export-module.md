# Feature Spec: Export Module
**Status:** Approved
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
- Export orchestration lives in `app/application/workflows.py` and `app/exports/service.py`.
- Module services/repositories MUST provide normalized row iterators (using async DB cursors) for export building to prevent OOM errors on large workspaces.
- To prevent high Python RAM usage (OOM errors), export generation must progressively stream database records into a temporary file on disk using SQLAlchemy's async stream interfaces, rather than building the complete payload structure in memory:
  - For JSON: Stream items chunk-by-chunk and format raw JSON segments progressively.
  - For CSV: Stream items and write zipped CSVs to the temporary file.
- The finalized temporary file is then transferred to the configured storage backend:
  - `"db"`: Falling back to saving the file bytes in the DB `artifact_blob` for backward compatibility.
  - `"local"`: Writing to the designated `/var/lib/lifestack/exports/` directory.
  - `"s3"`: Uploading the file object directly to R2/S3.
- Downloads are served efficiently using `FileResponse` for local files (using kernel-level sendfile) and streaming chunk-by-chunk for S3/DB assets.
- If storage uploads fail or config is missing, generation must fail-closed and return a structured API error.
- Stage 1 threshold: synchronous export supports up to 5,000 records per module. Requests above this threshold must fail fast with RFC 7807 (413 or documented equivalent) until async export is introduced.
- Only one active export (`pending`) per workspace is allowed at a time.
- If a selected module is not yet implemented, export returns an empty section for that module and records the omission in metadata.

## 6. Test Plan
- Integration: user creates export and receives downloadable artifact.
- Isolation: user B cannot fetch user A workspace export.
- Failure path: generation failure sets `status=failed` with error message.
- Storage verification: test that mock uploads to local disk and S3 succeed.
- Expiration verification: test that expired exports are cleaned up/marked expired.

## 7. Acceptance Criteria (Hardened)
- Export endpoints available under `/v1`.
- JSON/CSV generation is progressive and doesn't materialize all rows in RAM.
- Configuration parameters defined for:
  - `EXPORT_STORAGE_BACKEND` (choices: `db`, `local`, `s3`)
  - `EXPORT_LOCAL_PATH`
  - `EXPORT_TTL_DAYS`
- Concurrency and fail-closed storage validation are implemented.
- Daily TTL cleanup job runs under advisory lock `EXPORT_CLEANUP_LOCK_KEY = 1005` to identify exports older than `EXPORT_TTL_DAYS` and mark them `expired`. Physical files are deleted if `EXPORT_CLEANUP_DELETE_FILES` is enabled.

## 8. Future Evolution
- V1 intentionally favors a simple personal-OS-friendly path: synchronous generation for small datasets with a strict row cap.
- A future version may move export generation to a background workflow with explicit lifecycle states such as `pending`, `generating`, `ready`, and `failed`.
- Ready/failure notifications may later be surfaced through the notification system, starting with in-app delivery and optionally email when that delivery phase exists.
- List/history endpoints may later adopt fuller pagination controls as export volume becomes more operationally significant.

## Observability Hooks
- Emit structured logs for export request, generation start, generation end, download served.
- Emit counters for export request outcomes by format and status.
- Emit histogram for export generation duration.
