# Feature Spec: Bulk Import via CSV Templates
**Status:** Partially Implemented
**Spec ID:** 020

Implementation note (2026-06-11): CSV templates, validate-preview-commit flows, batch history/detail, rollback/delete, source metadata for spending transactions, budgets, and holdings, storage configuration, and integration tests are implemented in `app/imports`. The spec remains partial because true streaming/very-large-file guarantees and later import modules are still future work.

## 1. Overview
Manual data entry is a major onboarding bottleneck for spending, budgets, and investing. Users often have historical data in Excel/Sheets. This spec introduces downloadable CSV templates and a fail-all bulk import pipeline that validates first, then writes atomically.

This builds on:
- Spec 003 (spending module)
- Spec 006 (export module)
- Spec 008/011/012 (investing flows)
- Spec 004 (audit logging)
- Spec 002 (workspace isolation)

## 2. Goals
- Provide template-driven bulk creation for high-volume modules.
- Keep Stage 1 implementation CSV-first (Excel-compatible).
- Enforce **fail-all-if-any-error** semantics.
- Keep memory footprint minimal for very large files.
- Track upload/import metadata per user/workspace.
- Support config-dependent file persistence (S3-compatible if configured; fallback local/no-persist mode).

## 3. Non-Goals (Stage 1)
- Native `.xlsx` parsing.
- Partial-success imports.
- Background queue with async job workers.
- Auto category/account creation from unknown values.
- Complex merge/upsert semantics.

## 4. Stage 1 Scope
Enabled modules:
1. Spending transactions
2. Spending budgets
3. Investing holdings

Optional (deferred):
- Recurring transactions
- Cash balances

## 5. Product Rules
1. **Fail-all semantics:** if any row fails validation, no rows are persisted.
2. **Atomic write:** valid file writes in a single DB transaction.
3. **Two-step flow:** validate preview, then confirm import.
4. **Workspace isolation:** all references resolved only within caller workspace.

## 6. API Surface

### 6.1 Template download
- `GET /v1/import/templates/{module}`
- `module in {spending-transactions, spending-budgets, investing-holdings}`
- Returns CSV file with header row + sample rows.

### 6.2 Upload + validate
- `POST /v1/imports`
- Multipart form:
  - `module`: string enum
  - `file`: CSV
- Behavior:
  - Streams file
  - Parses/validates rows incrementally
  - Stores normalized validation snapshot
  - Creates import batch record in `validated` or `failed_validation` status
- Response:
  - `import_public_id`
  - `total_rows`
  - `valid_rows`
  - `error_count`
  - row-level error summary (capped, e.g. first 200)

### 6.3 Confirm import
- `POST /v1/imports/{import_public_id}/commit`
- Preconditions:
  - batch status = `validated`
  - no validation errors
- Behavior:
  - single transaction writes all rows
  - status -> `completed` or `failed_commit`
- Response:
  - inserted counts by entity
  - batch status

### 6.4 List imports
- `GET /v1/imports`
- Filters:
  - `module`
  - `status`
  - pagination

### 6.5 Get import detail
- `GET /v1/imports/{import_public_id}`
- Returns status, stats, validation summary, storage metadata.

## 7. Data Model

### 7.1 ImportBatch
- `id`, `public_id`
- `workspace_id`
- `user_id`
- `module` (enum)
- `status` (enum):
  - `uploaded`
  - `validated`
  - `failed_validation`
  - `committing`
  - `completed`
  - `failed_commit`
- `filename`
- `content_type`
- `file_size_bytes`
- `file_sha256`
- `storage_backend` (`none|local|s3`)
- `storage_key` (nullable)
- `total_rows`
- `valid_rows`
- `error_rows`
- `started_at`, `validated_at`, `committed_at`
- `created_at`, `updated_at`

### 7.2 ImportError
- `id`
- `import_batch_id`
- `row_number`
- `field_name` (nullable)
- `error_code`
- `message`
- `raw_value` (nullable)

### 7.3 ImportPreviewRow (optional table, Stage 1 suggested)
Stores normalized parsed rows for deterministic commit.
- `id`
- `import_batch_id`
- `row_number`
- `payload_json`

## 8. CSV Template Contracts

### 8.1 Spending transactions
Columns:
- `occurred_at` (ISO datetime or date)
- `type` (`income|expense`)
- `amount` (decimal)
- `category` (category name or category public_id)
- `description` (optional)

### 8.2 Spending budgets
Columns:
- `month_start` (`YYYY-MM-01`)
- `category` (name or public_id)
- `amount` (decimal)

### 8.3 Investing holdings
Columns:
- `symbol`
- `account_name`
- `quantity`
- `avg_cost`
- `currency`

## 9. Validation Rules
- Required columns must exist exactly.
- Unknown extra columns allowed only if explicitly configured; default reject.
- Row-level checks:
  - enum values valid
  - dates parseable
  - decimals > 0 where required
  - referenced category/account exists in workspace
- Cross-row checks:
  - duplicate budget `(category, month_start)` conflicts
  - duplicate row hash (same file) may be warning/error by module
- Hard cap:
  - max rows per import configurable (e.g. 100k)

## 10. Memory and Streaming Requirements
- Use streaming parser (async chunk read + line iteration).
- Never load full file into memory for parse/validation.
- Write parsed rows/errors incrementally.
- Use async generators (`yield`) in parser pipeline to process rows lazily.
- Commit phase reads normalized preview rows in chunks.

Implementation guideline:
- Parser pipeline shape:
  - `async def iter_csv_rows(file) -> AsyncIterator[Row]`
  - `async def validate_rows(rows) -> AsyncIterator[ValidatedRow|RowError]`
  - collector persists summaries incrementally.

## 11. File Storage Strategy (Config-Dependent)

### 11.1 Config
- `IMPORT_STORAGE_BACKEND=none|local|s3` (default `none`)
- `IMPORT_LOCAL_PATH` (for local backend)
- `IMPORT_S3_ENDPOINT`
- `IMPORT_S3_BUCKET`
- `IMPORT_S3_REGION`
- `IMPORT_S3_ACCESS_KEY`
- `IMPORT_S3_SECRET_KEY`
- `IMPORT_S3_FORCE_PATH_STYLE` (bool)

### 11.2 Behavior
- `none`:
  - process stream in-memory
  - keep metadata + preview/error tables only
  - no source file retained
- `local`:
  - persist original upload to local disk
- `s3`:
  - persist original upload to S3-compatible object store
  - save object key in batch

## 12. Security and Compliance
- Enforce auth + workspace scope on all import endpoints.
- Limit MIME types and extension (`text/csv`, `.csv`).
- Virus scanning hook optional (future).
- Truncate error payloads to avoid log/DB abuse.
- Record audit events:
  - `import_uploaded`
  - `import_validated`
  - `import_failed_validation`
  - `import_committed`
  - `import_failed_commit`

## 13. Performance and Limits
- Target 100k-row CSV without memory blowup.
- Default row limit configurable.
- Validation error response capped to protect payload size.
- Commit writes batched but still inside one transaction for fail-all guarantee.

## 14. Test Plan
- Unit tests:
  - CSV streaming parser
  - row validators per module
  - fail-all state transitions
  - storage backend routing logic
- Integration tests:
  - valid import creates rows atomically
  - invalid single row causes zero writes
  - cross-workspace references rejected
  - `none/local/s3` backend behavior
  - large-file simulation does not exhaust memory

## 15. Acceptance Criteria
- Templates downloadable for all Stage 1 modules.
- Upload+validate works with row-level error reporting.
- Commit writes atomically only when validation is clean.
- Any validation error results in zero entity writes.
- Import batch history is visible and scoped per workspace.
- Storage backend is configurable; no code change required to switch.

## 16. Migration
- Add `import_batches`, `import_errors`, and optional `import_preview_rows` tables.
- No backfill required.

## 17. Stage 2+ Extensions
- Native `.xlsx` template/import support.
- Background import workers + progress polling.
- Partial-success mode as optional strategy.
- Smart mapping UI for column aliases.
