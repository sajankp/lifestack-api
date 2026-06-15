import asyncio
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import structlog

from app.auth.models import AuthSession, PasswordResetToken, User
from app.auth.repository import AuthSessionRepository, PasswordResetTokenRepository, UserRepository
from app.auth.schemas import UserCreate
from app.config import settings
from app.core.auth import hash_password, verify_password
from app.core.exceptions import ConflictError, UnauthorizedError


class AuthService:
    DUMMY_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$GOmQ3l1jgCgnsSr1XaQO4A$cuP2ZOCQDzD6pisbkLxr1toLEOhywb1hu1xaLVP4v2U"

    def __init__(
        self,
        user_repo: UserRepository,
        session_repo: AuthSessionRepository,
        reset_token_repo: PasswordResetTokenRepository | None = None,
    ):
        self.user_repo = user_repo
        self.session_repo = session_repo
        self.reset_token_repo = reset_token_repo or PasswordResetTokenRepository(user_repo.session)
        self.logger = structlog.get_logger(__name__)

    async def register_user(self, user_create: UserCreate) -> User:
        existing_user = await self.user_repo.get_by_email(user_create.email)
        existing_user_name = await self.user_repo.get_by_username(user_create.username)
        if existing_user or existing_user_name:
            raise ConflictError(
                detail="Registration failed. Invalid or taken username/email.",
                title="Registration Failed",
            )

        new_user = await self.user_repo.create(user_create)
        return new_user

    async def get_user_by_id(self, user_id: int) -> User | None:
        return await self.user_repo.get_by_id(user_id)

    async def authenticate_user(self, username_or_email: str, password: str) -> User | None:
        user = await self.user_repo.get_by_username(username_or_email)
        if not user:
            # Maybe try email
            user = await self.user_repo.get_by_email(username_or_email)

        if not user:
            verify_password(password, self.DUMMY_PASSWORD_HASH)
            return None

        if not user.is_active:
            verify_password(password, user.hashed_password)
            return None

        is_valid, _ = verify_password(password, user.hashed_password)
        if not is_valid:
            return None

        return user

    async def create_session(
        self, user_id: int, sid: str, expires_in: timedelta, initial_token: str | None = None
    ) -> AuthSession:
        # Enforce max active sessions
        active_sessions = await self.session_repo.get_active_sessions_by_user_id(user_id)
        if len(active_sessions) >= settings.MAX_ACTIVE_SESSIONS_PER_USER:
            num_to_revoke = len(active_sessions) - settings.MAX_ACTIVE_SESSIONS_PER_USER + 1
            for i in range(num_to_revoke):
                sess = active_sessions[i]
                sess.revoked_at = datetime.now(UTC)
                self.session_repo.session.add(sess)
            await self.session_repo.session.flush()

        token_hash = (
            hashlib.sha256(initial_token.encode("utf-8")).hexdigest() if initial_token else None
        )

        auth_session = AuthSession(
            user_id=user_id,
            sid=sid,
            expires_at=datetime.now(UTC) + expires_in,
            current_token_hash=token_hash,
            rotated_at=datetime.now(UTC),
        )
        return await self.session_repo.create(auth_session)

    async def validate_session(self, sid: str, user_id: int) -> AuthSession:
        auth_session = await self.session_repo.get_active_by_sid(sid, user_id)
        if not auth_session:
            raise UnauthorizedError(detail="Session is no longer active")
        return auth_session

    async def touch_session(self, sid: str, user_id: int) -> AuthSession:
        auth_session = await self.validate_session(sid, user_id)
        return await self.session_repo.touch(auth_session)

    async def revoke_session(self, sid: str) -> bool:
        return await self.session_repo.revoke_by_sid(sid)

    async def change_password(self, user_id: int, current_password: str, new_password: str) -> None:
        user = await self.get_user_by_id(user_id)
        if not user:
            raise UnauthorizedError(detail="User not found")
        is_valid, _ = verify_password(current_password, user.hashed_password)
        if not is_valid:
            raise UnauthorizedError(detail="Incorrect current password")

        user.hashed_password = hash_password(new_password)
        self.user_repo.session.add(user)
        await self.user_repo.session.flush()

    async def revoke_all_sessions(self, user_id: int) -> int:
        return await self.session_repo.revoke_all_by_user_id(user_id)

    async def forgot_password(self, email: str) -> None:
        user = await self.user_repo.get_by_email(email)
        if not user or not user.is_active:
            dummy_token = secrets.token_urlsafe(32)
            hashlib.sha256(dummy_token.encode("utf-8")).hexdigest()
            await asyncio.sleep(0.05)
            # Prevent email enumeration by failing silently with a log
            self.logger.info(
                "Password reset requested for non-existent or inactive email", email=email
            )
            return

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = datetime.now(UTC) + timedelta(minutes=15)

        reset_token = PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        await self.reset_token_repo.create(reset_token)

        if settings.ENV in ("local", "test"):
            reset_url = f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?token={token}"
            self.logger.info("Password Reset Link Generated", email=email, reset_url=reset_url)
        else:
            self.logger.info("Password reset email triggered", email=email)

    async def reset_password(self, token: str, new_password: str) -> None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        reset_token = await self.reset_token_repo.get_by_hash(token_hash)

        if (
            not reset_token
            or reset_token.used_at is not None
            or reset_token.expires_at < datetime.now(UTC)
        ):
            raise UnauthorizedError(detail="Invalid or expired password reset token.")

        user = await self.get_user_by_id(reset_token.user_id)
        if not user or not user.is_active:
            raise UnauthorizedError(detail="Invalid or expired password reset token.")

        user.hashed_password = hash_password(new_password)
        reset_token.used_at = datetime.now(UTC)

        self.user_repo.session.add(user)
        self.reset_token_repo.session.add(reset_token)
        await self.user_repo.session.flush()
        if self.reset_token_repo.session is not self.user_repo.session:
            await self.reset_token_repo.session.flush()

        # Invalidate all active sessions for this user
        if user.id is not None:
            await self.revoke_all_sessions(user.id)
