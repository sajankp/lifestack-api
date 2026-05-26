from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.notifications.models import Notification, NotificationDelivery, NotificationPreference


class NotificationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_notifications(
        self,
        workspace_id: int,
        user_id: int,
        is_read: bool | None,
        category: str | None,
        severity: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Notification], int]:
        q = select(Notification).where(
            Notification.workspace_id == workspace_id, Notification.user_id == user_id
        )
        cq = (
            select(func.count())
            .select_from(Notification)
            .where(Notification.workspace_id == workspace_id, Notification.user_id == user_id)
        )
        if is_read is not None:
            q = q.where(Notification.is_read == is_read)
            cq = cq.where(Notification.is_read == is_read)
        if category:
            q = q.where(Notification.category == category)
            cq = cq.where(Notification.category == category)
        if severity:
            q = q.where(Notification.severity == severity)
            cq = cq.where(Notification.severity == severity)
        q = q.order_by(Notification.created_at.desc()).offset(offset).limit(limit)
        items = (await self.session.execute(q)).scalars().all()
        total = (await self.session.execute(cq)).scalar_one()
        return list(items), int(total)

    async def get_by_public_id(self, workspace_id: int, user_id: int, public_id):
        return (
            await self.session.execute(
                select(Notification).where(
                    Notification.workspace_id == workspace_id,
                    Notification.user_id == user_id,
                    Notification.public_id == public_id,
                )
            )
        ).scalar_one_or_none()

    async def unread_count(self, workspace_id: int, user_id: int) -> int:
        return int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Notification)
                    .where(
                        Notification.workspace_id == workspace_id,
                        Notification.user_id == user_id,
                        Notification.is_read.is_(False),
                    )
                )
            ).scalar_one()
        )

    async def mark_read(self, notification: Notification) -> Notification:
        notification.is_read = True
        notification.read_at = datetime.now(UTC)
        self.session.add(notification)
        await self.session.flush()
        return notification

    async def mark_all_read(self, workspace_id: int, user_id: int) -> int:
        res = await self.session.execute(
            update(Notification)
            .where(
                Notification.workspace_id == workspace_id,
                Notification.user_id == user_id,
                Notification.is_read.is_(False),
            )
            .values(is_read=True, read_at=datetime.now(UTC))
        )
        await self.session.flush()
        return int(res.rowcount or 0)

    async def delete_notification(self, notification: Notification) -> None:
        await self.session.execute(
            delete(NotificationDelivery).where(
                NotificationDelivery.notification_id == notification.id
            )
        )
        await self.session.delete(notification)
        await self.session.flush()

    async def get_preferences(
        self, workspace_id: int, user_id: int
    ) -> list[NotificationPreference]:
        return list(
            (
                await self.session.execute(
                    select(NotificationPreference).where(
                        NotificationPreference.workspace_id == workspace_id,
                        NotificationPreference.user_id == user_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    async def get_preference(
        self, workspace_id: int, user_id: int, category: str
    ) -> NotificationPreference | None:
        return (
            await self.session.execute(
                select(NotificationPreference).where(
                    NotificationPreference.workspace_id == workspace_id,
                    NotificationPreference.user_id == user_id,
                    NotificationPreference.category == category,
                )
            )
        ).scalar_one_or_none()

    async def upsert_preference(
        self, workspace_id: int, user_id: int, category: str, data: dict
    ) -> NotificationPreference:
        pref = await self.get_preference(workspace_id, user_id, category)
        if pref is None:
            pref = NotificationPreference(
                workspace_id=workspace_id, user_id=user_id, category=category
            )
        for k, v in data.items():
            setattr(pref, k, v)
        pref.updated_at = datetime.now(UTC)
        self.session.add(pref)
        await self.session.flush()
        return pref

    async def create_notification(self, payload: dict) -> Notification:
        n = Notification(**payload)
        self.session.add(n)
        await self.session.flush()
        self.session.add(
            NotificationDelivery(
                notification_id=n.id,
                channel="in_app",
                status="delivered",
                attempted_at=datetime.now(UTC),
            )
        )
        await self.session.flush()
        return n
