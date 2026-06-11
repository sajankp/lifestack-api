# Feature Spec: First Scheduler Workflow - Budget Guardrails
**Status:** Implemented
**Spec ID:** 009

Implementation note (2026-06-11): the budget guardrails workflow runs through the scheduler/application workflow layer, emits idempotent system todos and notifications, and is covered by application and E2E-hook tests.

## 1. Overview
Defines the first production scheduler workflow built on Spec 005: periodic budget guardrail evaluation that creates or updates a system todo when monthly category spend breaches thresholds.

## 2. Goals
- Validate scheduler infrastructure with real business value.
- Provide proactive spending reminders.
- Ensure idempotent behavior across repeated runs.

## 3. Workflow Contract
- Job name: `budget_guardrails_job`.
- Trigger: every 6 hours (configurable).
- Scope: iterate active workspaces, isolated transaction boundary per workspace.

Per workspace:
1. Fetch current month budgets and spending totals.
2. Detect threshold crossings (default warning at 90%, critical at 100%).
3. Ensure one system todo exists/updates per breached category (`system_key` pattern).
4. Write audit row for created/updated reminders.

Todo model dependency for this spec:
- Add `system_key: str | None` to todo model.
- Enforce unique constraint on `(workspace_id, system_key)` when `system_key is not null`.

## 4. Idempotency Rules
- Re-running same window must not create duplicate todos.
- Existing guardrail todo should be updated (not recreated) when severity changes.
- If budget falls back below threshold, guardrail todo is auto-resolved in V1.

## 5. Config
- `SCHEDULER_ENABLED` (from Spec 005).
- `BUDGET_GUARDRAILS_INTERVAL_HOURS` default `6`.
- `BUDGET_WARNING_THRESHOLD` default `0.9`.
- `BUDGET_CRITICAL_THRESHOLD` default `1.0`.

## 6. Failure Handling
- One workspace failure must be logged and skipped; remaining workspaces continue.
- Job run should expose structured logs with `workspace_id`, `job_name`, `duration_ms`, `status`.

## 7. Test Plan
- Unit tests for threshold classification and idempotency key behavior.
- Integration tests for:
  - reminder creation on threshold breach,
  - no duplicates on rerun,
  - cross-workspace isolation,
  - per-workspace failure isolation.

## 8. Acceptance Criteria
- Job registered through scheduler only when `SCHEDULER_ENABLED=true`.
- Workspace-isolated commits and rollbacks verified.
- Guardrail todo creation/update idempotency covered by tests.
- Audit events emitted for reminder actions.
- Workflow-generated audit events use `module=application`, `action=budget_guardrail_triggered`.
- Dashboard summary surfaces active guardrail todo count in V1.

## 9. Observability Hooks
- Emit structured logs per workspace evaluation including threshold classification result.
- Emit counters for breaches detected, guardrail todos created, guardrail todos updated, and auto-resolutions.
- Emit trace spans for budget fetch, threshold evaluation, todo upsert, and audit write.
