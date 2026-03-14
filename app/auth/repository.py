from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auth.models import User
from app.auth.schemas import UserCreate
from app.core.auth import hash_password


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
