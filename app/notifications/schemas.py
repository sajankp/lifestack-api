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
