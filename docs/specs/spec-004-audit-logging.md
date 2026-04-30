# Spec 004: Audit Logging

**Status:** Planned
**Spec ID:** 004

## Problem Statement
The Lifestack API needs a way to track data mutations (creates, updates, deletes, and other actions like completions) across modules (todo, spending, etc.) to form an immutable history of events. This is critical for the "personal OS" trust factor, security monitoring, and eventually restoring or analyzing historical user actions.

## Proposed Solution
Introduce a centralized, append-only `AuditLog` table using Postgres. Audit writes should occur within the same transaction boundary as the business changes so that the audit entry completely shares the success or failure of the mutation. The payload structure should be flexible enough to handle different module definitions (so we will use `JSONB` for details).

## Data Model Changes
Add `AuditLog` model to `app/core/audit.py` mapping to `audit_logs` table:
- `id` (int, primary key)
- `public_id` (uuid, index, unique)
- `workspace_id` (int, index) — tied to the active workspace.
- `actor_id` (int) — the user who performed the action.
- `action` (str) — e.g., "create", "update", "delete", "complete".
- `module` (str) — e.g., "todo", "spending", "investing".
- `entity_type` (str) — e.g., "todo", "spending_category", "spending_transaction", "budget".
- `entity_id` (int) — internal ID of the affected record.
- `details` (JSONB) — dictionary of any additional changes / metadata. MUST be passed through a strict redaction layer to strip PII and credentials before insertion.
- `timestamp` (timezone-aware datetime, indexed)

## Event Contract (Stage 1 Minimum)
Each audit event must include the following `details` keys:
- `entity_public_id` (str) — public UUID of the affected entity.
- `before` (object | null) — previous selected fields snapshot for update/delete actions.
- `after` (object | null) — resulting selected fields snapshot for create/update/complete actions.
- `changed_fields` (list[str]) — fields changed by the action (empty list allowed for actions with no field diff).
- `request_id` (str | null) — request correlation id when available.

Action-level requirements:
- `create`: `after` is required, `before` must be null.
- `update`: both `before` and `after` are required.
- `delete`: `before` is required, `after` must be null.
- `complete`: both `before` and `after` are required.

The contract above is the minimum shared shape. Module-specific keys are allowed as additive fields.

Rationale for identity fields:
- Keep both `entity_id` (fast local joins) and `details.entity_public_id` (external identity portability).

## Immutability and Write Boundaries
- Audit rows are append-only; application code must not update or delete `audit_logs` rows after insert.
- Audit writes must occur in the same DB transaction as the business mutation.
- If the business transaction rolls back, the paired audit row must not persist.
- Audit logger usage is at Service/Workflow boundaries, not hidden inside generic repository helpers.
- **Redaction Layer:** Before serializing the `details` JSONB payload, the logger MUST apply an allowlist or explicit redactor to ensure passwords, auth tokens, exact financial account numbers, or API keys are never written to the audit log (preventing toxic data spills).
- Stage 1 enforcement choice is explicit: append-only is enforced in application code. DB triggers/role-level hardening are deferred.

## API Changes
No external REST API endpoints need to be exposed for reading audit logs right now (unless requested later for an Audit UI). This spec focuses on the internal dependency and persistence model.

## Implementation Plan
1. Create `app/core/audit.py` with `AuditLog` class (SQLModel) and `AuditLogger` utility class.
2. Generate Alembic migration for `audit_logs` table.
3. Update specific service methods (like `TodoService.create_todo`) to inject an `AuditLogger` and register a mutation securely in the same `transaction.commit()` sequence.
   - For now, maybe just test integration in `todo` module as proof-of-concept.

## Test Strategy
- **Unit Tests:** Verify `AuditLogger.log` adds an event to the session context correctly without committing prematurely.
- **Integration Tests:** Ensure an action (e.g. creating a todo) results in an `audit_logs` row linked by the same `workspace_id` and that the commit behaves atomically.

## Acceptance Criteria
- `audit_logs` migration is present and applied by Alembic.
- At least one mutation path in `todo` writes an audit row with required `details` keys.
- A failed mutation transaction produces no audit row.
- Audit rows are never updated/deleted by application paths.
- Integration tests cover one success case and one rollback case.
- Retention policy is explicitly out of scope for V1 (no archival/pruning in this slice).

## Observability Hooks
- Emit `audit_log_written` and `audit_log_rollback_discarded` structured events.
- Add counters for audit writes by `module` and `action`.
- Trace spans should include audit write operations as child spans of business mutations.
