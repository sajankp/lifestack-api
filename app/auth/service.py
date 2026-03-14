from fastapi import status

from app.auth.models import User
from app.auth.repository import UserRepository
from app.auth.schemas import UserCreate
from app.core.auth import verify_password
from app.core.exceptions import APIError


class AuthService:
    def __init__(self, user_repo: UserRepository):
        self.user_repo = user_repo

    async def register_user(self, user_create: UserCreate) -> User:
        existing_user = await self.user_repo.get_by_email(user_create.email)
        if existing_user:
            raise APIError(
                type_str="conflict",
                title="Email Already Registered",
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already registered",
            )
        existing_user_name = await self.user_repo.get_by_username(user_create.username)
        if existing_user_name:
            raise APIError(
                type_str="conflict",
                title="Username Taken",
                status_code=status.HTTP_409_CONFLICT,
                detail="Username already taken",
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
                return None

        is_valid, _ = verify_password(password, user.hashed_password)
        if not is_valid:
            return None

        return user
