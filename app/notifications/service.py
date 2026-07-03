import uuid

from app.core.exceptions import NotFoundError, ValidationError
from app.notifications.repository import NotificationRepository, PushSubscriptionRepository


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
    ):
        pref = await self.repository.get_preference(workspace_id, user_id, category)
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
        # False, same as the model default (spec-052).
        if pref and pref.channel_push:
            has_subscription = await self.repository.has_active_push_subscription(
                workspace_id, user_id
            )
            if has_subscription:
                await self.repository.create_pending_push_delivery(notification.id)

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
            workspace_id, user_id, endpoint, p256dh, auth, device_label
        )

    async def list_subscriptions(self, workspace_id: int, user_id: int):
        return await self.repository.list_for_user(workspace_id, user_id)

    async def unsubscribe(self, workspace_id: int, user_id: int, public_id: uuid.UUID) -> None:
        subscription = await self.repository.get_by_public_id(workspace_id, user_id, public_id)
        if not subscription:
            raise NotFoundError(detail=f"Push subscription with id {public_id} not found")
        await self.repository.delete(subscription)
