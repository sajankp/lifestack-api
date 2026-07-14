import uuid

from app.core.exceptions import NotFoundError, ValidationError
from app.notifications.models import NotificationPreference
from app.notifications.repository import NotificationRepository, PushSubscriptionRepository

# Sentinel distinguishing "caller didn't pre-fetch a preference" from a
# legitimately-absent preference row (None). Lets batch callers (e.g. a
# per-workspace reminder loop) pass an already-looked-up preference/
# subscription-status per user instead of re-querying them once per notify().
_UNSET = object()


class NotificationService:
    def __init__(self, repository: NotificationRepository):
        self.repository = repository

    async def notify(
        self,
        workspace_id: int,
        user_id: int,
        category: str,
        severity: str,
        title: str,
        body: str | None = None,
        module: str = "system",
        entity_type: str | None = None,
        entity_public_id=None,
        *,
        preference: NotificationPreference | None = _UNSET,  # type: ignore[assignment]
        has_push_subscription: bool | None = None,
    ):
        if preference is _UNSET:
            preference = await self.repository.get_preference(workspace_id, user_id, category)
        pref = preference
        if pref and pref.is_muted:
            return None
        notification = await self.repository.create_notification({
            "workspace_id": workspace_id,
            "user_id": user_id,
            "category": category,
            "severity": severity,
            "title": title,
            "body": body,
            "module": module,
            "entity_type": entity_type,
            "entity_public_id": entity_public_id,
        })

        # Push is opt-in: no preference row means channel_push defaults to
        # False, same as the model default (spec-052) — EXCEPT the
        # "briefing" category (spec-067 owner decision), which defaults to
        # push-ON absent an explicit row when the user already has an
        # active push subscription (subscribing already expressed intent;
        # an explicit preference row, muted or not, always wins over this
        # default). Every other category is unaffected.
        should_push = bool(pref and pref.channel_push)
        if pref is None and category == "briefing":
            if has_push_subscription is None:
                has_push_subscription = await self.repository.has_active_push_subscription(
                    workspace_id, user_id
                )
            should_push = bool(has_push_subscription)

        if should_push:
            if has_push_subscription is None:
                has_push_subscription = await self.repository.has_active_push_subscription(
                    workspace_id, user_id
                )
            if has_push_subscription:
                await self.repository.create_pending_push_delivery(notification.id)

        # Email is opt-in like push (spec-052 channel_email defaults False,
        # spec-081 wires delivery). No briefing-category default here — that
        # default is push-specific (see should_push above).
        if pref and pref.channel_email:
            await self.repository.create_pending_email_delivery(notification.id)

        return notification

    async def list_notifications(
        self, workspace_id: int, user_id: int, is_read, category, severity, limit, offset
    ):
        return await self.repository.list_notifications(
            workspace_id, user_id, is_read, category, severity, limit, offset
        )

    async def unread_count(self, workspace_id: int, user_id: int) -> int:
        return await self.repository.unread_count(workspace_id, user_id)

    async def mark_read(self, workspace_id: int, user_id: int, public_id):
        n = await self.repository.get_by_public_id(workspace_id, user_id, public_id)
        if not n:
            raise NotFoundError(detail=f"Notification with id {public_id} not found")
        return await self.repository.mark_read(n)

    async def mark_all_read(self, workspace_id: int, user_id: int) -> int:
        return await self.repository.mark_all_read(workspace_id, user_id)

    async def dismiss(self, workspace_id: int, user_id: int, public_id) -> None:
        n = await self.repository.get_by_public_id(workspace_id, user_id, public_id)
        if not n:
            raise NotFoundError(detail=f"Notification with id {public_id} not found")
        await self.repository.delete_notification(n)

    async def list_recent_unread(
        self, workspace_id: int, user_id: int, category: str, since, limit: int = 5
    ):
        return await self.repository.list_recent_unread(
            workspace_id, user_id, category, since, limit
        )

    async def get_preferences(self, workspace_id: int, user_id: int):
        return await self.repository.get_preferences(workspace_id, user_id)

    async def update_preference(self, workspace_id: int, user_id: int, category: str, data: dict):
        return await self.repository.upsert_preference(workspace_id, user_id, category, data)


class PushSubscriptionService:
    def __init__(self, repository: PushSubscriptionRepository):
        self.repository = repository

    async def subscribe(
        self,
        workspace_id: int,
        user_id: int,
        endpoint: str,
        p256dh: str,
        auth: str,
        device_label: str | None,
    ):
        existing = await self.repository.get_by_endpoint(endpoint)
        if existing is not None and (
            existing.workspace_id != workspace_id or existing.user_id != user_id
        ):
            # An endpoint is a browser+origin capability URL, not a per-user
            # secret, but re-registering it for a different workspace/user
            # would silently move push delivery to the wrong account.
            raise ValidationError(detail="This push endpoint is already registered")
        return await self.repository.upsert(
            workspace_id, user_id, endpoint, p256dh, auth, device_label, existing=existing
        )

    async def list_subscriptions(self, workspace_id: int, user_id: int):
        return await self.repository.list_for_user(workspace_id, user_id)

    async def unsubscribe(self, workspace_id: int, user_id: int, public_id: uuid.UUID) -> None:
        subscription = await self.repository.get_by_public_id(workspace_id, user_id, public_id)
        if not subscription:
            raise NotFoundError(detail=f"Push subscription with id {public_id} not found")
        await self.repository.delete(subscription)
