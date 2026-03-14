from fastapi import Depends, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.repository import UserRepository
from app.auth.service import AuthService
from app.core.database.postgres import get_db_session

limiter = Limiter(key_func=get_remote_address)


async def get_user_repo(session: AsyncSession = Depends(get_db_session)) -> UserRepository:
    return UserRepository(session)


async def get_auth_service(repo: UserRepository = Depends(get_user_repo)) -> AuthService:
    return AuthService(repo)


async def get_current_user(request: Request) -> dict:
    if not hasattr(request.state, "user_id") or not request.state.user_id:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"id": request.state.user_id, "username": request.state.username}
