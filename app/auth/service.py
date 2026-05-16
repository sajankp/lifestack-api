from datetime import UTC, datetime, timedelta

from app.auth.models import AuthSession, User
from app.auth.repository import AuthSessionRepository, UserRepository
from app.auth.schemas import UserCreate
from app.core.auth import verify_password
from app.core.exceptions import ConflictError, UnauthorizedError


class AuthService:
    def __init__(self, user_repo: UserRepository, session_repo: AuthSessionRepository):
        self.user_repo = user_repo
        self.session_repo = session_repo

    async def register_user(self, user_create: UserCreate) -> User:
        existing_user = await self.user_repo.get_by_email(user_create.email)
        if existing_user:
            raise ConflictError(detail="Email already registered", title="Email Already Registered")
        existing_user_name = await self.user_repo.get_by_username(user_create.username)
        if existing_user_name:
            raise ConflictError(detail="Username already taken", title="Username Taken")

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
                return None

        if not user.is_active:
            return None

        is_valid, _ = verify_password(password, user.hashed_password)
        if not is_valid:
            return None

        return user

    async def create_session(self, user_id: int, sid: str, expires_in: timedelta) -> AuthSession:
        auth_session = AuthSession(
            user_id=user_id,
            sid=sid,
            expires_at=datetime.now(UTC) + expires_in,
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
