# Feature Spec: Notifications & Delivery Channel
**Status:** Implemented (Phase 1)
**Spec ID:** 015

Implementation note (2026-06-11): in-app notifications, unread counts, preferences, mark-read/mark-all-read/delete flows, budget-guardrail integration, RBAC hardening, and workspace isolation tests are implemented in `app/notifications`. Email and push remain future phases.

## 1. Overview
The scheduler (Spec 005) and budget guardrails (Spec 009) currently surface alerts by creating system todos. This is functional but invisible unless the user actively checks their task list. This spec introduces a notification registry and delivery abstraction so any workflow can trigger user-visible alerts through multiple channels.

This builds on:
- Spec 005 (scheduler): job infrastructure
- Spec 009 (budget guardrails): first workflow producing alerts
- Spec 004 (audit logging): event recording pattern

## 2. Goals
- Provide a unified notification model for all modules and workflows.
- Support multiple delivery channels over time, starting with in-app delivery.
- Allow users to configure notification preferences per category.
- Maintain a persistent notification inbox for in-app review.
- Keep the notification system decoupled from the producing modules.

## 3. Non-Goals (for this slice)
- Real-time WebSocket push (in-app notifications are poll-based in Phase 1).
- SMS delivery channel.
- Rich notification templates with dynamic layouts.
- Notification grouping or threading.
- Third-party webhook delivery (Slack, Discord, etc.).
- Batch digest mode (e.g., daily email summary of all notifications) — see Spec 016 for weekly summaries.

## 3.1 Delivery Phases

### Phase 1: In-App Notifications
- Persistent in-app inbox
- Unread counts
- Read / mark-all-read flows
- Notification preferences for in-app delivery and muting
- Integration with existing workflows such as budget guardrails

### Phase 2: Email Delivery
- Optional email delivery for selected categories
- Batched scheduler-based sending
- Delivery tracking and rate limiting

### Phase 3: Push Delivery
- Mobile/personal-device-aligned push notifications
- Delivery tracking extended to push channel once mobile infrastructure exists

## 4. Data Model

### Notification
- `id`: internal PK
- `public_id`: external UUID
- `workspace_id`: tenant FK
- `user_id`: recipient FK
- `category`: enum — see section 4.1
- `severity`: enum `info` | `warning` | `critical`
- `title`: short headline (max 200 chars)
- `body`: optional longer description (max 2000 chars)
- `module`: source module name (e.g., `spending`, `investing`, `application`)
- `entity_type`: optional related entity type (e.g., `budget`, `holding`)
- `entity_public_id`: optional related entity UUID for deep-linking
- `is_read`: boolean, default `false`
- `read_at`: nullable timestamp
- `created_at`

### 4.1 Notification Categories
- `budget_warning` — spending approaching budget limit
- `budget_breach` — spending exceeded budget
- `recurring_generated` — recurring transaction auto-created
- `recurring_failed` — recurring generation encountered an error
- `portfolio_alert` — investment threshold crossed
- `todo_reminder` — task due date approaching or overdue
- `system` — platform-level notifications

### NotificationPreference
- `id`: internal PK
- `workspace_id`: tenant FK
- `user_id`: FK
- `category`: notification category enum
- `channel_in_app`: boolean, default `true`
- `channel_email`: boolean, default `false`
- `channel_push`: boolean, default `false` (future phase)
- `is_muted`: boolean, default `false` — suppresses all channels for this category
- `created_at`, `updated_at`

Constraints:
- unique `(user_id, workspace_id, category)`

### NotificationDelivery
Tracks delivery attempts per channel:
- `id`: internal PK
- `notification_id`: FK to `notifications`
- `channel`: enum `in_app` | `email` | `push`
- `status`: enum `pending` | `delivered` | `failed`
- `attempted_at`: nullable timestamp
- `error_detail`: nullable text (for failed attempts)
- `created_at`

Phase note:
- In Phase 1, `NotificationDelivery` rows are only required for `in_app`.
- `email` delivery rows are introduced when Phase 2 is enabled.
- `push` delivery rows remain future-facing until Phase 3.

## 5. API Surface

### Notifications
- `GET /v1/notifications` — list notifications for current user/workspace
- `GET /v1/notifications/unread-count` — badge count
- `PATCH /v1/notifications/{public_id}/read` — mark as read
- `POST /v1/notifications/mark-all-read` — mark all as read
- `DELETE /v1/notifications/{public_id}` — dismiss/delete

Query parameters for list:
- `is_read` (boolean filter)
- `category` (filter by category)
- `severity` (filter by severity)
- Pagination via cursor (Spec 001 pagination pattern)

### Preferences
- `GET /v1/notifications/preferences` — list all preference settings
- `PATCH /v1/notifications/preferences/{category}` — update channel toggles for a category

Phase note:
- Phase 1 must support `channel_in_app` and `is_muted`.
- `channel_email` becomes active in Phase 2.
- `channel_push` remains reserved for Phase 3.

## 6. Notification Dispatch Service

### Internal API
Modules and workflows create notifications through an internal service, not HTTP:

```python
# app/core/notifications.py
class NotificationService:
    async def notify(
        self,
        workspace_id: int,
        user_id: int,
        category: NotificationCategory,
        severity: NotificationSeverity,
        title: str,
        body: str | None = None,
        module: str = "system",
        entity_type: str | None = None,
        entity_public_id: UUID | None = None,
    ) -> None: ...
```

### Dispatch Flow
1. Check user's `NotificationPreference` for the category.
2. If `is_muted`, skip entirely (no record created).
3. Create `Notification` row.
4. For each enabled channel, create `NotificationDelivery` row with `status=pending`.
5. Process `in_app` immediately (row creation is the delivery).
6. Queue `email` for async processing only when Phase 2 is enabled.
7. Do not enqueue `push` until Phase 3 exists.

### Email Delivery (Phase 2 - minimal)
- Scheduler job `notification_email_job` runs every 15 minutes.
- Picks up `pending` email deliveries, sends via configured SMTP/SES.
- Updates delivery status to `delivered` or `failed`.
- Rate limit: max 20 emails per user per hour.

### Push Delivery (Phase 3)
- Push channel is modeled but not implemented in this spec's initial rollout.
- Delivery rows with `channel=push` remain in `pending` until push infrastructure is added.

## 7. Integration with Existing Workflows

### Budget Guardrails (Spec 009)
After creating/updating a guardrail todo, also call:
```python
await notification_service.notify(
    workspace_id=workspace_id,
    user_id=user_id,
    category="budget_warning",  # or "budget_breach"
    severity="warning",  # or "critical"
    title=f"Budget alert: {category_name} at {pct}%",
    module="application",
    entity_type="budget",
    entity_public_id=budget_public_id,
)
```

### Recurring Transactions (Spec 013)
On generation failure, notify with `category="recurring_failed"`.

## 8. Configuration
- `NOTIFICATIONS_ENABLED`: feature flag, default `true`.
- `NOTIFICATION_EMAIL_ENABLED`: Phase 2 feature flag, default `false`.
- `NOTIFICATION_EMAIL_INTERVAL_MINUTES`: email batch frequency, default `15`.
- `NOTIFICATION_EMAIL_RATE_LIMIT_PER_HOUR`: max emails per user/hour, default `20`.
- `NOTIFICATION_EMAIL_FROM`: sender address.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`: email delivery config.

## 9. Audit Events
- `notification_created` — system creates a notification
- `notification_read` — user marks notification as read
- `notification_preferences_updated` — user changes preferences

## 10. Test Plan
- **Unit tests:**
  - Dispatch logic respects muted preferences
  - Channel selection based on preference toggles
  - Rate limiting enforcement
  - Severity/category classification
- **Integration tests:**
  - Budget guardrail triggers notification creation
  - Mark-read updates state correctly
  - Preference changes affect subsequent dispatches
  - Unread count accuracy
  - Workspace isolation

## 11. Acceptance Criteria
- Notification CRUD endpoints operational with workspace/user scoping.
- Dispatch service callable from any module/workflow.
- User preferences control which channels receive notifications.
- In-app delivery is immediate (row creation).
- Phase 1 is complete with in-app delivery only.
- Email delivery is introduced separately in Phase 2 via scheduler job.
- Unread count endpoint provides accurate badge data.
- Budget guardrails workflow emits notifications alongside todo creation.
- Audit events emitted for notification lifecycle.

## 12. Migration
- Alembic migration adds `notifications`, `notification_preferences`, `notification_deliveries` tables.
- Default preferences seeded for existing users on first access (lazy initialization).
