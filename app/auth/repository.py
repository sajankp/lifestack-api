from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthSession, PasswordResetToken, User, UserAuthIdentity
from app.auth.schemas import UserCreate
from app.core.auth import hash_password
from app.core.repository import BaseRepository


class UserAuthIdentityStore(Protocol):
    """Repository contract required by OAuth linking flows."""

    async def get_by_oauth_identity(self, provider: str, subject: str) -> User | None: ...

    async def get_by_id(self, user_id: int) -> User | None: ...

    async def get_auth_identity(self, provider: str, subject: str) -> UserAuthIdentity | None: ...

    async def get_auth_identity_for_user(
        self, user_id: int, provider: str
    ) -> UserAuthIdentity | None: ...

    async def add_auth_identity(
        self, user_id: int, provider: str, subject: str
    ) -> UserAuthIdentity: ...


class UserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_email(self, email: str) -> User | None:
        statement = select(User).where(User.email == email)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> User | None:
        statement = select(User).where(User.username == username)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_oauth_identity(self, provider: str, subject: str) -> User | None:
        statement = select(User).where(
            User.oauth_provider == provider,
            User.oauth_sub == subject,
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_auth_identity(self, provider: str, subject: str) -> UserAuthIdentity | None:
        statement = select(UserAuthIdentity).where(
            UserAuthIdentity.provider == provider,
            UserAuthIdentity.subject == subject,
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_auth_identity_for_user(
        self, user_id: int, provider: str
    ) -> UserAuthIdentity | None:
        statement = select(UserAuthIdentity).where(
            UserAuthIdentity.user_id == user_id,
            UserAuthIdentity.provider == provider,
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def add_auth_identity(
        self, user_id: int, provider: str, subject: str
    ) -> UserAuthIdentity:
        identity = UserAuthIdentity(user_id=user_id, provider=provider, subject=subject)
        self.session.add(identity)
        await self.session.flush()
        await self.session.refresh(identity)
        return identity

    async def list_auth_identities(self, user_id: int) -> list[UserAuthIdentity]:
        statement = select(UserAuthIdentity).where(UserAuthIdentity.user_id == user_id)
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def delete_auth_identity(self, user_id: int, provider: str) -> UserAuthIdentity | None:
        identity = await self.get_auth_identity_for_user(user_id, provider)
        if identity is None:
            return None
        await self.session.delete(identity)
        await self.session.flush()
        return identity

    async def get_by_id(self, user_id: int) -> User | None:
        statement = select(User).where(User.id == user_id)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def create(self, user_create: UserCreate) -> User:
        db_user = User(
            email=user_create.email,
            username=user_create.username,
            hashed_password=hash_password(user_create.password),
        )
        self.session.add(db_user)
        await self.session.flush()
        await self.session.refresh(db_user)
        return db_user

    async def save(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)
        return user


class AuthSessionRepository(BaseRepository[AuthSession]):
    async def get_active_by_sid(self, sid: str, user_id: int | None = None) -> AuthSession | None:
        statement = select(AuthSession).where(
            AuthSession.sid == sid,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > datetime.now(UTC),
        )
        if user_id is not None:
            statement = statement.where(AuthSession.user_id == user_id)

        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def revoke_by_sid(self, sid: str) -> bool:
        auth_session = await self.get_active_by_sid(sid)
        if not auth_session:
            return False

        now = datetime.now(UTC)
        auth_session.revoked_at = now
        auth_session.last_seen_at = now
        self.session.add(auth_session)
        await self.session.flush()
        return True

    async def touch(self, auth_session: AuthSession) -> AuthSession:
        auth_session.last_seen_at = datetime.now(UTC)
        self.session.add(auth_session)
        await self.session.flush()
        await self.session.refresh(auth_session)
        return auth_session

    async def revoke_all_by_user_id(self, user_id: int) -> int:
        result = await self.session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        await self.session.flush()
        return result.rowcount

    async def get_active_sessions_by_user_id(self, user_id: int) -> Sequence[AuthSession]:
        statement = (
            select(AuthSession)
            .where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > datetime.now(UTC),
            )
            .order_by(AuthSession.last_seen_at.asc())
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def delete_expired_and_revoked_sessions(self) -> int:
        now = datetime.now(UTC)
        statement = delete(AuthSession).where(
            (AuthSession.expires_at < now) | AuthSession.revoked_at.is_not(None)
        )
        result = await self.session.execute(statement)
        await self.session.flush()
        return result.rowcount or 0


class PasswordResetTokenRepository(BaseRepository[PasswordResetToken]):
    async def get_by_hash(self, token_hash: str) -> PasswordResetToken | None:
        statement = select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()
