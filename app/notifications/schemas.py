import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NotificationResponse(BaseModel):
    public_id: uuid.UUID
    category: str
    severity: str
    title: str
    body: str | None
    module: str
    entity_type: str | None
    entity_public_id: uuid.UUID | None
    is_read: bool
    read_at: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NotificationPreferenceResponse(BaseModel):
    category: str
    channel_in_app: bool
    channel_email: bool
    channel_push: bool
    is_muted: bool

    model_config = ConfigDict(from_attributes=True)


class NotificationPreferenceUpdate(BaseModel):
    channel_in_app: bool | None = None
    channel_email: bool | None = None
    channel_push: bool | None = None
    is_muted: bool | None = None


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionCreate(BaseModel):
    """Mirrors the browser PushSubscription.toJSON() shape."""

    endpoint: str
    keys: PushSubscriptionKeys
    device_label: str | None = None


class PushSubscriptionResponse(BaseModel):
    public_id: uuid.UUID
    endpoint_hint: str
    device_label: str | None
    is_active: bool
    last_success_at: datetime | None
    last_failure_at: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
