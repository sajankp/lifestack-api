import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class Notification(SQLModel, table=True):
    __tablename__ = "notifications"
    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    category: str = Field(max_length=64)
    severity: str = Field(max_length=16)
    title: str = Field(max_length=200)
    body: str | None = Field(default=None, max_length=2000)
    module: str = Field(default="system", max_length=64)
    entity_type: str | None = Field(default=None, max_length=64)
    entity_public_id: uuid.UUID | None = Field(default=None)
    is_read: bool = Field(default=False)
    read_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )


class NotificationPreference(SQLModel, table=True):
    __tablename__ = "notification_preferences"
    id: int | None = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    category: str = Field(max_length=64)
    channel_in_app: bool = Field(default=True)
    channel_email: bool = Field(default=False)
    channel_push: bool = Field(default=False)
    is_muted: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "user_id", "category", name="uq_notification_pref"),
    )


class NotificationDelivery(SQLModel, table=True):
    __tablename__ = "notification_deliveries"
    id: int | None = Field(default=None, primary_key=True)
    notification_id: int = Field(foreign_key="notifications.id", index=True)
    channel: str = Field(max_length=16)
    status: str = Field(max_length=16, default="pending")
    attempted_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    error_detail: str | None = Field(default=None, max_length=1000)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )


class PushSubscription(SQLModel, table=True):
    """A browser-issued Web Push subscription (spec-052).

    ``endpoint`` is a capability URL — treated like a secret token: never
    logged, only ever returned to the client truncated. ``is_active`` is
    flipped false by ``push_delivery_job`` on a permanent push-service
    rejection (404/410), the standard Web Push contract for "this
    subscription no longer exists."
    """

    __tablename__ = "push_subscriptions"
    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    endpoint: str = Field(max_length=1000, unique=True)
    p256dh: str = Field(max_length=255)
    auth: str = Field(max_length=255)
    device_label: str | None = Field(default=None, max_length=100)
    is_active: bool = Field(default=True)
    last_success_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    last_failure_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
