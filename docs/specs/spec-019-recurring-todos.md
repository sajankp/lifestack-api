# Spec 019 - Recurring Todos (Phase 1.1)

Status: Proposed
Owner: Backend + Frontend
Last updated: 2026-05-27

## Goal
Allow users to define recurring todo rules that automatically generate todo items on schedule.

## Scope
- Recurring rule CRUD under `todo` module.
- Supported frequencies: `daily | weekly | monthly | yearly`.
- Scheduler generation of due todos with catch-up behavior.
- Todo create UI supports explicit due date.
- Todo UI supports creating/listing/deleting recurring rules.

## Out of scope
- Complex recurrence exceptions (weekdays, holidays, custom calendars).
- Per-rule time-of-day and timezone overrides.
- Notification channel fanout from recurring rule generation.

## Backend Contract
- `GET /v1/todo/recurring/`
- `POST /v1/todo/recurring/`
- `PATCH /v1/todo/recurring/{rule_id}`
- `DELETE /v1/todo/recurring/{rule_id}`

Rule payload:
- `title`, `description`, `priority`
- `frequency`, `interval`
- `anchor_date`, `end_date`, `is_active`

Scheduler behavior:
- For each active rule where `next_due_date <= today`, generate todo rows.
- Advance `next_due_date` by frequency + interval until it is in the future.
- Deactivate rule when advanced date exceeds `end_date`.

## Acceptance Criteria
- Creating a recurring rule returns persisted rule with `next_due_date`.
- Scheduler run creates due todos and advances rules deterministically.
- Todo create UI can send `due_date`.
- Todo page exposes recurring rule management without breaking existing task flows.
