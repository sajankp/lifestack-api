# Feature Spec: Recurring Transactions & Subscriptions
**Status:** Proposed
**Spec ID:** 013

## 1. Overview
The spending module (Spec 003) currently requires manual entry for every transaction. In practice, a large portion of personal spending is predictable: rent, subscriptions, salary, insurance, utilities. This spec adds recurring transaction definitions that automatically generate spending entries on schedule.

This builds on:
- Spec 003 (spending module): categories, transactions, budgets
- Spec 005 (scheduler): APScheduler infrastructure
- Spec 009 (budget guardrails): first scheduler workflow pattern

## 2. Goals
- Allow users to define recurring income and expense patterns once.
- Automatically generate transactions on their schedule without manual intervention.
- Reduce friction for budget accuracy — recurring costs are pre-populated each period.
- Provide visibility into upcoming committed spend for budget planning.
- Maintain audit trail for auto-generated transactions.

## 3. Non-Goals (for this slice)
- Automatic detection/suggestion of recurring patterns from transaction history.
- Bank feed or statement import integration.
- Variable-amount recurring transactions (e.g., utility bills that change monthly).
- Retroactive generation of missed transactions for periods before the recurrence was defined.
- Multi-currency recurring transactions (uses workspace base currency).

## 4. Data Model

### RecurringTransaction
- `id`: internal PK
- `public_id`: external UUID
- `workspace_id`: tenant FK
- `user_id`: creator FK
- `category_id`: FK to spending category in same workspace
- `amount`: `NUMERIC(12, 2)`, positive value
- `type`: enum `income` | `expense`
- `description`: transaction description applied to generated entries
- `frequency`: enum `daily` | `weekly` | `monthly` | `yearly`
- `interval`: integer (e.g., `2` with `weekly` = every 2 weeks), default `1`
- `anchor_date`: date representing the first occurrence or reference point
- `next_due_date`: date of the next pending generation
- `end_date`: optional date after which no more transactions are generated
- `is_active`: boolean, default `true`
- `last_generated_at`: nullable timestamp of most recent generation
- `created_at`, `updated_at`

Constraints:
- `amount > 0`
- `interval >= 1`
- `end_date > anchor_date` when `end_date` is set
- `category_id` must belong to the same `workspace_id`
- unique `(workspace_id, public_id)`

### Generated Transaction Linkage
Generated transactions use the existing `spending_transactions` table with an additional nullable column:
- `recurring_transaction_id`: FK to `recurring_transactions.id`, nullable

This allows:
- Distinguishing auto-generated from manual transactions.
- Tracing lineage back to the recurrence definition.
- Users can still edit or delete individual generated transactions without affecting the recurrence.

## 5. API Surface

### Recurring Transactions
- `GET /v1/spending/recurring` — list active recurring definitions for the workspace
- `POST /v1/spending/recurring` — create a new recurring transaction
- `GET /v1/spending/recurring/{public_id}` — get single recurring definition
- `PATCH /v1/spending/recurring/{public_id}` — update (amount, description, frequency, end_date, is_active)
- `DELETE /v1/spending/recurring/{public_id}` — soft-delete (set `is_active = false`)

Query parameters for list:
- `type` (income/expense)
- `category_id`
- `is_active` (default `true`)

### Upcoming Preview
- `GET /v1/spending/recurring/upcoming?days=30` — returns projected transactions for the next N days based on active recurrences (read-only, no DB writes)

## 6. Scheduler Job

### Job: `recurring_transactions_job`
- **Trigger:** daily at 00:15 UTC (configurable via `RECURRING_TXN_GENERATION_HOUR`)
- **Scope:** iterate active workspaces with active recurring definitions

Per workspace:
1. Query all `recurring_transactions` where `is_active = true` and `next_due_date <= today` and (`end_date` is null or `end_date >= next_due_date`).
2. For each due recurrence:
   a. Insert a `spending_transactions` row with `recurring_transaction_id` set.
   b. Advance `next_due_date` to next occurrence based on `frequency` + `interval`.
   c. Update `last_generated_at`.
   d. If new `next_due_date > end_date`, set `is_active = false`.
3. Emit audit event per generated transaction.
4. Commit per workspace (isolated failure boundary per Spec 005 pattern).

### Catch-up Behavior
If the scheduler was down for multiple days:
- On next run, generate all overdue transactions (one per missed period).
- Cap catch-up at 90 days to prevent runaway generation from misconfigured recurrences.
- Log a warning if catch-up exceeds 7 days.

## 7. Configuration
- `RECURRING_TXN_GENERATION_HOUR`: hour (UTC) to run generation job, default `0`.
- `RECURRING_TXN_CATCHUP_LIMIT_DAYS`: max days of catch-up generation, default `90`.

## 8. Interaction with Budget Guardrails
Generated transactions contribute to monthly spend totals like any other transaction. The budget guardrails job (Spec 009) already aggregates all transactions — no special handling needed. The `upcoming` preview endpoint helps users anticipate guardrail breaches before they happen.

## 9. Audit Events
- `recurring_transaction_created` — user creates a recurrence
- `recurring_transaction_updated` — user modifies a recurrence
- `recurring_transaction_deactivated` — user or system deactivates
- `recurring_transaction_generated` — system auto-generates a transaction (module: `application`)

## 10. Test Plan
- **Unit tests:**
  - Next-due-date advancement for each frequency + interval combination
  - Catch-up generation with multiple missed periods
  - End-date boundary (deactivation when exhausted)
  - Upcoming preview calculation
- **Integration tests:**
  - Full generation cycle: define recurrence → run job → verify transaction created
  - Idempotency: running job twice on same day produces no duplicates
  - Cross-workspace isolation
  - Category validation (reject cross-workspace category)
  - Catch-up cap enforcement

## 11. Acceptance Criteria
- Recurring transaction CRUD endpoints operational with workspace scoping.
- Scheduler job generates transactions daily for all due recurrences.
- Generated transactions are linked back to their recurrence definition.
- Catch-up logic handles missed days without runaway generation.
- Upcoming preview returns projected entries without side effects.
- Audit events emitted for all mutations and generations.
- Budget guardrails naturally incorporate generated transactions.

## 12. Migration
- Alembic migration adds `recurring_transactions` table.
- Alembic migration adds nullable `recurring_transaction_id` FK to `spending_transactions`.
- No data backfill required (new feature, no existing recurrences).
