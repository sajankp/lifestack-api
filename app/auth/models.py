import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    email: str = Field(index=True, unique=True, max_length=255)
    username: str = Field(index=True, unique=True, max_length=50)
    hashed_password: str
    is_active: bool = Field(default=True)
    timezone: str | None = Field(default=None, max_length=64)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )


class AuthSession(SQLModel, table=True):
    __tablename__ = "auth_sessions"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    sid: str = Field(index=True, unique=True, max_length=64)
    expires_at: datetime = Field(sa_type=sa.DateTime(timezone=True), index=True)
    revoked_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    current_token_hash: str | None = Field(default=None, max_length=64)
    previous_token_hash: str | None = Field(default=None, max_length=64)
    rotated_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    last_seen_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )


class PasswordResetToken(SQLModel, table=True):
    __tablename__ = "password_reset_tokens"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    token_hash: str = Field(index=True, unique=True, max_length=64)
    expires_at: datetime = Field(sa_type=sa.DateTime(timezone=True), index=True)
    used_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
