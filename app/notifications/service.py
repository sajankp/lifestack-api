import html as html_mod

from app.core.exceptions import NotFoundError
from app.notifications.repository import NotificationRepository


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
        # Sanitize free-text fields to prevent stored XSS
        safe_title = html_mod.escape(title)
        safe_body = html_mod.escape(body) if body is not None else None
        return await self.repository.create_notification({
            "workspace_id": workspace_id,
            "user_id": user_id,
            "category": category,
            "severity": severity,
            "title": safe_title,
            "body": safe_body,
            "module": module,
            "entity_type": entity_type,
            "entity_public_id": entity_public_id,
        })

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
