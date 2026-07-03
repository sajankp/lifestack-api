# Spec-052: Web Push Notification Delivery

**Created:** 2026-07-03
**Status:** Approved (implementation) — 2026-07-03
**Depends on:** existing notifications module (specs 019/020 era), scheduler foundation (`app/application/jobs.py`), existing PWA shell (`lifestack-web/public/sw.js`)

---

## Problem

The notification model was built for multi-channel delivery, but only the in-app channel
exists — a notification reaches the user only if they already have the app open, which
defeats the purpose of a reminder:

- `NotificationPreference` already carries a `channel_push` flag (default `False`) per
  (user, category), and `NotificationDelivery` already tracks per-channel delivery attempts
  with `status`/`attempted_at`/`error_detail` — the schema anticipated push from day one.
- `NotificationRepository.create_notification` writes exactly one delivery row per
  notification: `channel="in_app", status="delivered"`. No other channel is ever attempted.
- The web app is already an installable PWA: `public/manifest.webmanifest` is linked from
  `index.html`, and `public/sw.js` (registered in production by `src/pwa.ts`) does app-shell
  caching — but has no `push` or `notificationclick` handlers.
- The product roadmap explicitly parks "push delivery" in the Notifications backlog and
  lists "push notifications for reminders and summaries" under Track 1 (mobile companion).

Concrete failure this causes: a recurring medication todo (`RecurringTodoRule`, e.g.
`daily` with `interval=2` and a `due_time`) generates the todo rows correctly, but nothing
ever tells the user on their phone or tablet. The reminder exists only as a row they must
remember to go look at.

There is a second, quieter gap: **no notification source exists for due todos at all.**
The only `NotificationService.notify` callers today are the weekly-summary path
(`app/summaries/service.py`, `app/application/jobs.py` weekly-summary job). Even with a
working push channel, a due todo would produce no notification to push. Delivery and a
todo-due source have to land together for the feature to mean anything.

## Solution

Three pieces, all riding existing rails: a push-subscription store, a push delivery job
draining the existing `NotificationDelivery` queue, and a todo-due reminder job as the
first notification source that makes push worth having.

### New model: `push_subscriptions`

| Column | Type | Notes |
|---|---|---|
| id | PK | internal |
| public_id | UUID | external identifier |
| workspace_id | FK → workspaces | workspace-scoped like every business table |
| user_id | FK → users | subscription owner |
| endpoint | text, unique | the browser-issued push URL; unique per subscription |
| p256dh | varchar(255) | client public key (from `PushSubscription.getKey`) |
| auth | varchar(255) | client auth secret |
| device_label | varchar(100), nullable | user-agent-derived hint ("Chrome on Android") for the settings UI |
| is_active | bool, default true | flipped false on permanent push-service rejection (404/410) |
| last_success_at / last_failure_at | timestamptz, nullable | delivery health |
| created_at / updated_at | timestamptz | |

The `endpoint` is effectively a capability URL — treat it like a token: never logged
(invariant: sensitive data never appears in logs), returned to the client only as a
truncated display hint.

### Config (`app/config.py`, per the adding-a-setting checklist)

| Setting | Default | Notes |
|---|---|---|
| `VAPID_PUBLIC_KEY` | `None` | push disabled when unset — safe default is feature-off |
| `VAPID_PRIVATE_KEY` | `None` | server-side only, never sent to the client beyond the public key (invariant 8: third-party keys never client-side… the *public* key is by design public) |
| `VAPID_SUBJECT` | `None` | `mailto:` contact required by the Web Push spec |
| `PUSH_DELIVERY_INTERVAL_MINUTES` | `1` | delivery job cadence |
| `TODO_REMINDER_INTERVAL_MINUTES` | `5` | reminder-source job cadence |

No `_check_production_defaults` entry — push is optional; when the keys are unset the
subscription endpoints return 503 with a clear detail and the jobs no-op. Production values
go in `.env.production` and are documented in `docs/PRODUCTION_DEPLOYMENT.md` in the same
pass (runbook rule). Backend dependency: `pywebpush`.

### API (`app/notifications/router.py`)

- `GET /notifications/push/vapid-public-key` → `{key}` (503 if unconfigured).
- `POST /notifications/push-subscriptions` — body is the serialized browser
  `PushSubscription` (`endpoint`, `keys.p256dh`, `keys.auth`) plus optional `device_label`.
  Upserts by `endpoint` (re-subscribing the same browser must not duplicate); reactivates
  an inactive row for the same endpoint. → `201`.
- `GET /notifications/push-subscriptions` — the caller's subscriptions (truncated
  endpoint, device_label, health fields) for the settings UI.
- `DELETE /notifications/push-subscriptions/{public_id}` → `204`.

All workspace-scoped through the standard router → service → repository layering.

### Delivery: enqueue on create, drain by job

1. **Enqueue** — `create_notification` gains one step: after writing the `in_app` delivery
   row, if the user's `NotificationPreference` for that category has `channel_push=True`,
   is not muted, and the user has ≥1 active subscription, also write
   `NotificationDelivery(channel="push", status="pending")`. No preference row means no
   push (opt-in, matching the existing `channel_push=False` default).
2. **Drain** — new `push_delivery_job` in `app/application/jobs.py`, following the house
   pattern exactly (own advisory lock key, `pg_try_advisory_xact_lock`, error isolation
   per row, idempotent): select pending push deliveries, send the notification's
   title/body/entity link to each active subscription of the target user via `pywebpush`,
   then mark the delivery `sent` or `failed` with `error_detail`. A `404`/`410` from the
   push service means the subscription is gone — set `is_active=False` on it (standard
   Web Push contract) and continue. Status transitions make re-runs safe: only `pending`
   rows are picked up.

One delivery row fans out to all of the user's active subscriptions (phone + tablet + desktop);
per-subscription outcomes fold into the single row's status (`sent` if any endpoint accepted,
`failed` with detail if all failed). Per-endpoint delivery rows are deliberately out of scope —
`NotificationDelivery` models channels, not devices.

### First real source: todo-due reminders

New `todo_reminder_job` (same jobs.py pattern, own lock key): find incomplete todos with
`due_date` within the look-ahead window (now → now + interval) whose reminder has not been
sent, and create a `Notification` via the existing `NotificationService.notify`
(`category="todo_reminder"`, `module="todo"`, `entity_type="todo"`,
`entity_public_id=todo.public_id`). Push enqueueing then happens for free via the delivery
step above.

Dedup is the one design point: a `reminded_at: datetime | None` column on `todos` (set when
the reminder notification is created) makes the job idempotent without a joins-based
"notification already exists" probe, and naturally re-arms if the user moves `due_date`
later (reset `reminded_at` on due-date change in the todo update path).

Recurring todos already generate their instances with a timezone-aware `due_date` composed
from the rule's `due_time` + `timezone` (`app/application/workflows.py`), so medication
reminders at the right local time need **no recurrence changes** — this spec plus an
existing `daily/interval=2` rule with a `due_time` is the complete medication workflow.

### Frontend (`lifestack-web`)

- Extend the existing `public/sw.js` with `push` (show notification from payload JSON) and
  `notificationclick` (focus an open client or open the entity URL) handlers. The service
  worker and registration flow already exist — no new PWA scaffolding.
- Notification settings surface (wherever `NotificationPreference` toggles live or a new
  settings section): request `Notification.requestPermission()`, subscribe via
  `PushManager.subscribe({userVisibleOnly: true, applicationServerKey})` with the VAPID
  public key fetched from the API, POST the subscription, list/revoke existing
  subscriptions. Surface the per-category `channel_push` toggles that already exist in the
  preferences API.
- Graceful degradation: hide the push UI when `!('PushManager' in window)`; show the
  install-first hint on iOS Safari when running in-browser (push requires the PWA to be
  added to the home screen, iOS 16.4+). Android Chrome and desktop browsers work directly.

### Worked flow (medication reminder, end to end)

1. User has `RecurringTodoRule` "Take medication", `daily`, `interval=2`, `due_time=09:00`,
   `timezone=Asia/Kolkata`; enables push for category `todo_reminder` on their phone
   (installed PWA).
2. Recurring-todo job generates the todo with `due_date=2026-07-05T03:30:00Z` (09:00 IST).
3. `todo_reminder_job` (within its window before/at due) creates the notification, sets
   `reminded_at`; `create_notification` enqueues a pending push delivery.
4. `push_delivery_job` sends via `pywebpush`; the phone shows "Take medication" even with
   the browser closed; tapping it opens the todo.

## Backend impact (`lifestack-api`)

- `app/notifications/models.py`: `PushSubscription` model. `app/todo/models.py`:
  `reminded_at` column.
- `app/notifications/repository.py` / `service.py` / `router.py` / `schemas.py`:
  subscription CRUD, VAPID key endpoint, push-enqueue step in `create_notification`.
- `app/application/jobs.py`: `push_delivery_job`, `todo_reminder_job` (+ two advisory lock
  keys, registration per `docs/JOBS.md`, documented there in the same pass).
- `app/config.py`: the five settings above.
- `alembic/versions/`: next free number at implementation time (0037 is claimed by
  spec-051, currently in implementation — take 0038+ by merge order): `push_subscriptions`
  table + `todos.reminded_at`, clean downgrade.
- `pyproject.toml`: `pywebpush`.

## Out of scope

- **Email delivery** — separate channel, separate infrastructure decision (roadmap keeps it
  sequenced with notification strategy).
- **Real-time in-app transport** (SSE/WebSocket for the bell icon) — the in-app channel
  stays poll-based; this spec is about closed-app delivery.
- **Notification grouping/digests** — roadmap backlog, unchanged.
- **Native mobile push (FCM/APNs)** — Track 1 territory; web push is deliberately the
  cheap precursor.
- **Per-device delivery rows** — `NotificationDelivery` stays per-channel (see Delivery).
- **New recurrence semantics** — spec-053 (calendar recurrence modes) is independent; this
  spec works with recurrence exactly as it exists today.

## Golden test scenarios (required before merge)

1. **Subscription lifecycle** — subscribe (201), re-subscribe same endpoint (upsert, no
   duplicate), list shows truncated endpoint, delete (204); all workspace-scoped (a second
   workspace's user cannot see or delete it).
2. **Enqueue honors preference** — notify with `channel_push=False` (or no active
   subscription) → only the `in_app` delivery row; with `channel_push=True` + active
   subscription → an additional `pending` push row.
3. **Delivery job** — with `pywebpush` mocked: pending → `sent` on success; push-service
   410 → delivery `failed` + subscription `is_active=False`; re-run picks up nothing
   (idempotent).
4. **Todo reminder source** — todo due inside the window gets exactly one notification
   across two job runs (`reminded_at` dedup); moving `due_date` later re-arms; completed
   todos are skipped.
5. **Unconfigured VAPID** — subscription endpoints 503, jobs no-op cleanly.
