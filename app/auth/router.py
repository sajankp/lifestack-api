import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import OAuth2PasswordRequestForm

from app.application.workflows import UserRegistrationWorkflow
from app.auth.schemas import PasswordChange, TokenResponse, UserCreate, UserResponse
from app.auth.service import AuthService
from app.config import settings
from app.core.auth import create_token, get_user_info_from_token
from app.core.dependencies import (
    get_auth_service,
    get_current_user,
    get_current_user_optional,
    get_user_registration_workflow,
    get_workspace_service,
    limiter,
)
from app.core.exceptions import NotFoundError, UnauthorizedError
from app.platform.service import WorkspaceService

router = APIRouter()


@router.post("/register", response_model=bool)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def create_user(
    request: Request,
    user_in: UserCreate,
    workflow: UserRegistrationWorkflow = Depends(get_user_registration_workflow),
):
    await workflow.register_user_with_workspace(user_in)
    return True


@router.get("/me", response_model=UserResponse)
async def get_me(
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    user = await auth_service.get_user_by_id(current_user["id"])
    if not user:
        raise NotFoundError(detail="User not found")
    return user


@router.post("/login", response_model=TokenResponse)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def login_for_access_token(
    request: Request,
    response: Response,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    auth_service: AuthService = Depends(get_auth_service),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
    remember_me: bool = True,
):
    user = await auth_service.authenticate_user(form_data.username, form_data.password)
    if not user:
        raise UnauthorizedError(detail="Incorrect username or password")

    # Generate a new Session ID (sid) for this login session
    sid = str(uuid.uuid4())
    refresh_token_expires = timedelta(seconds=settings.REFRESH_TOKEN_EXPIRE_SECONDS)
    await auth_service.create_session(
        user_id=user.id,
        sid=sid,
        expires_in=refresh_token_expires,
    )

    default_workspace = await workspace_service.ensure_default_workspace(user.id, user.username)

    access_token_expires = timedelta(seconds=settings.ACCESS_TOKEN_EXPIRE_SECONDS)
    access_token = create_token(
        data={
            "sub": user.username,
            "sub_id": str(user.id),
            "default_workspace_id": default_workspace.id,
        },
        expires_delta=access_token_expires,
        sid=sid,
        token_type="access",
    )
    refresh_token = create_token(
        data={
            "sub": user.username,
            "sub_id": str(user.id),
            "default_workspace_id": default_workspace.id,
        },
        expires_delta=refresh_token_expires,
        sid=sid,
        token_type="refresh",
    )

    # Set HttpOnly cookies
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_SECONDS,
        path="/",
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        secure=settings.COOKIE_SECURE,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        max_age=settings.REFRESH_TOKEN_EXPIRE_SECONDS if remember_me else None,
        path="/",
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        secure=settings.COOKIE_SECURE,
    )
    response.set_cookie(
        key="sid",
        value=sid,
        httponly=True,
        max_age=settings.REFRESH_TOKEN_EXPIRE_SECONDS if remember_me else None,
        path="/",
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        secure=settings.COOKIE_SECURE,
    )

    # Return empty tokens in body, as they are now in HttpOnly cookies
    return TokenResponse(access_token="", token_type="bearer")


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def refresh_token(
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    refresh_token = request.cookies.get("refresh_token")

    if not refresh_token:
        raise UnauthorizedError(detail="Refresh token missing")

    try:
        _username, user_id, sid, default_workspace_id = get_user_info_from_token(
            refresh_token, expected_type="refresh"
        )
    except UnauthorizedError:
        raise UnauthorizedError(detail="Invalid refresh token") from None

    await auth_service.touch_session(sid=sid, user_id=int(user_id))
    user = await auth_service.get_user_by_id(int(user_id))
    if not user or not user.is_active:
        raise UnauthorizedError(detail="User account is inactive or no longer exists")
    if default_workspace_id is None:
        default_workspace = await workspace_service.ensure_default_workspace(user.id, user.username)
        default_workspace_id = default_workspace.id

    access_token_expires = timedelta(seconds=settings.ACCESS_TOKEN_EXPIRE_SECONDS)
    access_token = create_token(
        data={
            "sub": user.username,
            "sub_id": str(user.id),
            "default_workspace_id": default_workspace_id,
        },
        expires_delta=access_token_expires,
        sid=sid,
        token_type="access",
    )

    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_SECONDS,
        path="/",
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        secure=settings.COOKIE_SECURE,
    )

    return TokenResponse(access_token="", token_type="bearer")


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    current_user: dict | None = Depends(get_current_user_optional),
    auth_service: AuthService = Depends(get_auth_service),
):
    """Logout by clearing auth cookies."""
    if current_user and "sid" in current_user:
        await auth_service.revoke_session(current_user["sid"])

    for key in ("access_token", "refresh_token", "sid"):
        response.set_cookie(
            key=key,
            value="",
            httponly=True,
            max_age=0,
            expires=0,
            path="/",
            samesite=settings.COOKIE_SAMESITE,
            domain=settings.COOKIE_DOMAIN,
            secure=settings.COOKIE_SECURE,
        )
    return {"message": "Logged out successfully"}


@router.post("/change-password")
async def change_password(
    data: PasswordChange,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    await auth_service.change_password(
        user_id=current_user["id"],
        current_password=data.current_password,
        new_password=data.new_password,
    )
    await auth_service.revoke_all_sessions(current_user["id"])
    return {"message": "Password changed successfully"}


@router.post("/logout-all")
async def logout_all(
    response: Response,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    await auth_service.revoke_all_sessions(current_user["id"])
    for key in ("access_token", "refresh_token", "sid"):
        response.set_cookie(
            key=key,
            value="",
            httponly=True,
            max_age=0,
            expires=0,
            path="/",
            samesite=settings.COOKIE_SAMESITE,
            domain=settings.COOKIE_DOMAIN,
            secure=settings.COOKIE_SECURE,
        )
    return {"message": "Logged out from all devices"}
