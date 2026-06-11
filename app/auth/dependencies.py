from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.repository import AuthSessionRepository, UserRepository
from app.auth.service import AuthService
from app.config import settings
from app.core.auth import get_user_info_from_token
from app.core.csrf import MUTATING_METHODS, validate_cookie_csrf_token
from app.core.database.postgres import get_db_session
from app.core.exceptions import CSRFFailedError, UnauthorizedError


async def get_user_repo(session: AsyncSession = Depends(get_db_session)) -> UserRepository:
    return UserRepository(session)


async def get_auth_session_repo(
    session: AsyncSession = Depends(get_db_session),
) -> AuthSessionRepository:
    return AuthSessionRepository(session)


async def get_auth_service(
    repo: UserRepository = Depends(get_user_repo),
    session_repo: AuthSessionRepository = Depends(get_auth_session_repo),
) -> AuthService:
    return AuthService(repo, session_repo)


async def get_current_user(
    request: Request,
    auth_session_repo: AuthSessionRepository = Depends(get_auth_session_repo),
    user_repo: UserRepository = Depends(get_user_repo),
) -> dict:
    token = request.cookies.get("access_token")
    token_from_cookie = token is not None

    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header:
            auth_parts = auth_header.split()
            if len(auth_parts) == 2 and auth_parts[0].lower() == "bearer":
                token = auth_parts[1]
            else:
                raise UnauthorizedError(detail="Invalid authorization header")

    if not token:
        raise UnauthorizedError(detail="Not authenticated")

    username, user_id, sid, default_workspace_id = get_user_info_from_token(token)

    try:
        uid = int(user_id)
    except (ValueError, TypeError) as exc:
        raise UnauthorizedError(detail="Could not validate credentials") from exc

    user = await user_repo.get_by_id(uid)
    if not user or not user.is_active:
        raise UnauthorizedError(detail="User account is inactive or no longer exists")

    if token_from_cookie and request.method in MUTATING_METHODS:
        origin = request.headers.get("Origin")
        referer = request.headers.get("Referer")

        source = origin or referer
        if not source:
            raise CSRFFailedError(
                detail="Origin or Referer header is required for cookie-authenticated requests"
            )

        try:
            normalized_source = settings._normalize_origin(source)
        except ValueError:
            raise CSRFFailedError(
                detail=f"{'Origin' if origin else 'Referer'} header is invalid"
            ) from None

        if (
            not settings.csrf_trusted_origins
            or normalized_source not in settings.csrf_trusted_origins
        ):
            source_name = "Origin" if origin else "Referer"
            raise CSRFFailedError(
                detail=f"{source_name} is not allowed for cookie-authenticated requests"
            )
        validate_cookie_csrf_token(request)

    auth_session = await auth_session_repo.get_active_by_sid(sid, uid)
    if not auth_session:
        raise UnauthorizedError(detail="Session is no longer active")

    request.state.user_id = uid
    request.state.username = username
    request.state.sid = sid
    request.state.default_workspace_id = default_workspace_id

    return {
        "id": uid,
        "username": username,
        "sid": sid,
        "default_workspace_id": default_workspace_id,
    }


async def get_current_user_optional(
    request: Request,
    auth_session_repo: AuthSessionRepository = Depends(get_auth_session_repo),
    user_repo: UserRepository = Depends(get_user_repo),
) -> dict | None:
    """Soft authentication dependency that returns None instead of raising 401."""
    try:
        return await get_current_user(request, auth_session_repo, user_repo)
    except UnauthorizedError:
        return None
