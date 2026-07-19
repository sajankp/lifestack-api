import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.notifications.models import (
    Notification,
    NotificationDelivery,
    NotificationPreference,
    PushSubscription,
)


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

    async def list_recent_unread(
        self, workspace_id: int, user_id: int, category: str, since: datetime, limit: int = 5
    ) -> list[Notification]:
        """Unread notifications of a category created since ``since`` — the
        morning briefing's "fresh insights" line (spec-067) surfaces
        spec-058 insight notifications within their 48h freshness window."""
        result = await self.session.execute(
            select(Notification)
            .where(
                Notification.workspace_id == workspace_id,
                Notification.user_id == user_id,
                Notification.category == category,
                Notification.is_read.is_(False),
                Notification.created_at >= since,
            )
            .order_by(Notification.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

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

    async def get_preferences_for_users(
        self, workspace_id: int, user_ids: set[int], category: str
    ) -> dict[int, NotificationPreference]:
        """Batch equivalent of ``get_preference`` for a set of users — one
        query instead of one per user (used by per-workspace loops)."""
        if not user_ids:
            return {}
        rows = (
            (
                await self.session.execute(
                    select(NotificationPreference).where(
                        NotificationPreference.workspace_id == workspace_id,
                        NotificationPreference.user_id.in_(user_ids),
                        NotificationPreference.category == category,
                    )
                )
            )
            .scalars()
            .all()
        )
        return {pref.user_id: pref for pref in rows}

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

    async def has_active_push_subscription(self, workspace_id: int, user_id: int) -> bool:
        result = await self.session.execute(
            select(PushSubscription.id)
            .where(
                PushSubscription.workspace_id == workspace_id,
                PushSubscription.user_id == user_id,
                PushSubscription.is_active,
            )
            .limit(1)
        )
        return result.scalar() is not None

    async def users_with_active_push_subscription(
        self, workspace_id: int, user_ids: set[int]
    ) -> set[int]:
        """Batch equivalent of ``has_active_push_subscription`` for a set of
        users — one query instead of one per user."""
        if not user_ids:
            return set()
        result = await self.session.execute(
            select(PushSubscription.user_id.distinct()).where(
                PushSubscription.workspace_id == workspace_id,
                PushSubscription.user_id.in_(user_ids),
                PushSubscription.is_active,
            )
        )
        return set(result.scalars().all())

    async def create_pending_push_delivery(self, notification_id: int) -> NotificationDelivery:
        delivery = NotificationDelivery(
            notification_id=notification_id, channel="push", status="pending"
        )
        self.session.add(delivery)
        await self.session.flush()
        return delivery

    async def list_pending_push_deliveries(
        self, limit: int
    ) -> list[tuple[NotificationDelivery, Notification]]:
        result = await self.session.execute(
            select(NotificationDelivery, Notification)
            .join(Notification, Notification.id == NotificationDelivery.notification_id)
            .where(NotificationDelivery.channel == "push", NotificationDelivery.status == "pending")
            .order_by(NotificationDelivery.created_at.asc())
            .limit(limit)
        )
        return [(delivery, notification) for delivery, notification in result.all()]

    async def create_pending_email_delivery(self, notification_id: int) -> NotificationDelivery:
        delivery = NotificationDelivery(
            notification_id=notification_id, channel="email", status="pending"
        )
        self.session.add(delivery)
        await self.session.flush()
        return delivery

    async def list_pending_email_deliveries(
        self, limit: int
    ) -> list[tuple[NotificationDelivery, Notification]]:
        result = await self.session.execute(
            select(NotificationDelivery, Notification)
            .join(Notification, Notification.id == NotificationDelivery.notification_id)
            .where(
                NotificationDelivery.channel == "email", NotificationDelivery.status == "pending"
            )
            .order_by(NotificationDelivery.created_at.asc())
            .limit(limit)
        )
        return [(delivery, notification) for delivery, notification in result.all()]

    async def mark_delivery(
        self, delivery: NotificationDelivery, status: str, error_detail: str | None = None
    ) -> NotificationDelivery:
        """Mutates the already session-tracked ``delivery`` in place; caller
        flushes once after the batch (see ``deliver_pending_push_notifications``)
        instead of round-tripping per row."""
        delivery.status = status
        delivery.attempted_at = datetime.now(UTC)
        delivery.error_detail = error_detail
        return delivery


class PushSubscriptionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_endpoint(self, endpoint: str) -> PushSubscription | None:
        return (
            await self.session.execute(
                select(PushSubscription).where(PushSubscription.endpoint == endpoint)
            )
        ).scalar_one_or_none()

    async def upsert(
        self,
        workspace_id: int,
        user_id: int,
        endpoint: str,
        p256dh: str,
        auth: str,
        device_label: str | None,
        existing: PushSubscription | None = None,
    ) -> PushSubscription:
        """Create a new subscription, or reactivate/refresh one already
        registered for this endpoint — re-subscribing the same browser must
        not create a duplicate row. Pass ``existing`` if the caller already
        looked it up, to avoid querying for it twice."""
        if existing is None:
            existing = await self.get_by_endpoint(endpoint)
        if existing is not None:
            existing.workspace_id = workspace_id
            existing.user_id = user_id
            existing.p256dh = p256dh
            existing.auth = auth
            existing.device_label = device_label
            existing.is_active = True
            existing.updated_at = datetime.now(UTC)
            self.session.add(existing)
            await self.session.flush()
            return existing

        subscription = PushSubscription(
            workspace_id=workspace_id,
            user_id=user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            device_label=device_label,
        )
        self.session.add(subscription)
        await self.session.flush()
        return subscription

    async def list_for_user(self, workspace_id: int, user_id: int) -> list[PushSubscription]:
        return list(
            (
                await self.session.execute(
                    select(PushSubscription)
                    .where(
                        PushSubscription.workspace_id == workspace_id,
                        PushSubscription.user_id == user_id,
                    )
                    .order_by(PushSubscription.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

    async def list_active_for_user(self, workspace_id: int, user_id: int) -> list[PushSubscription]:
        return list(
            (
                await self.session.execute(
                    select(PushSubscription).where(
                        PushSubscription.workspace_id == workspace_id,
                        PushSubscription.user_id == user_id,
                        PushSubscription.is_active,
                    )
                )
            )
            .scalars()
            .all()
        )

    async def list_active_for_users(
        self, pairs: set[tuple[int, int]]
    ) -> dict[tuple[int, int], list[PushSubscription]]:
        """Batch form of ``list_active_for_user`` for N notifications fanning
        out over M distinct (workspace, user) recipients in one round trip."""
        if not pairs:
            return {}
        result = await self.session.execute(
            select(PushSubscription).where(
                tuple_(PushSubscription.workspace_id, PushSubscription.user_id).in_(list(pairs)),
                PushSubscription.is_active,
            )
        )
        grouped: dict[tuple[int, int], list[PushSubscription]] = {}
        for subscription in result.scalars().all():
            key = (subscription.workspace_id, subscription.user_id)
            grouped.setdefault(key, []).append(subscription)
        return grouped

    async def get_by_public_id(
        self, workspace_id: int, user_id: int, public_id: uuid.UUID
    ) -> PushSubscription | None:
        return (
            await self.session.execute(
                select(PushSubscription).where(
                    PushSubscription.workspace_id == workspace_id,
                    PushSubscription.user_id == user_id,
                    PushSubscription.public_id == public_id,
                )
            )
        ).scalar_one_or_none()

    async def delete(self, subscription: PushSubscription) -> None:
        await self.session.delete(subscription)
        await self.session.flush()

    async def mark_success(self, subscription: PushSubscription) -> None:
        """Mutates in place; caller flushes once after the batch (see
        ``deliver_pending_push_notifications``)."""
        subscription.last_success_at = datetime.now(UTC)

    async def mark_failure(self, subscription: PushSubscription, *, deactivate: bool) -> None:
        """Mutates in place; caller flushes once after the batch (see
        ``deliver_pending_push_notifications``)."""
        subscription.last_failure_at = datetime.now(UTC)
        if deactivate:
            subscription.is_active = False
