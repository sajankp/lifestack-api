import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from mcp.server.auth.provider import AuthorizeError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workflows import UserRegistrationWorkflow
from app.auth.repository import UserRepository
from app.auth.schemas import (
    ForgotPasswordRequest,
    PasswordChange,
    PasswordSet,
    ResetPasswordRequest,
    TokenResponse,
    UserCreate,
    UserResponse,
    UserTimezoneUpdate,
)
from app.auth.service import AuthService
from app.config import settings
from app.core.auth import create_token, get_user_info_from_token
from app.core.csrf import clear_csrf_token, issue_csrf_token
from app.core.database.postgres import get_db_session
from app.core.dependencies import (
    get_auth_service,
    get_current_user,
    get_current_user_optional,
    get_user_registration_workflow,
    get_user_repo,
    get_workspace_service,
    limiter,
)
from app.core.exceptions import NotFoundError, UnauthorizedError
from app.mcp.repository import McpGrantRepository
from app.platform.service import WorkspaceService

router = APIRouter()


class MCPAuthorizationApproval(BaseModel):
    state: str = Field(min_length=20, max_length=256)


class McpConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    public_id: uuid.UUID
    client_name: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None


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


@router.get("/me/auth-identities")
async def get_auth_identities(
    current_user: dict = Depends(get_current_user),
    user_repo: UserRepository = Depends(get_user_repo),
    auth_service: AuthService = Depends(get_auth_service),
):
    user = await auth_service.get_user_by_id(current_user["id"])
    if not user:
        raise NotFoundError(detail="User not found")
    identities = await user_repo.list_auth_identities(current_user["id"])
    providers = {identity.provider for identity in identities}
    # Legacy OAuth columns remain supported until all deployments have migrated.
    if user.oauth_provider:
        providers.add(user.oauth_provider)
    return {"has_password": bool(user.hashed_password), "providers": sorted(providers)}


@router.get("/mcp/authorize")
async def get_mcp_authorization_request(
    request: Request, state: str = Query(min_length=20, max_length=256)
):
    """Return the display-safe details for a pending MCP authorization request."""
    provider = getattr(request.app.state, "mcp_auth", None)
    if provider is None:
        raise HTTPException(status_code=404, detail="MCP authorization is disabled")
    details = await provider.get_authorization_request(state)
    if details is None:
        raise HTTPException(status_code=400, detail="Authorization request expired")
    return details


@router.post("/mcp/authorize")
async def approve_mcp_authorization(
    request: Request,
    payload: MCPAuthorizationApproval,
    current_user: dict = Depends(get_current_user),
):
    """Approve an MCP OAuth request from the authenticated Lifestack web app."""
    provider = getattr(request.app.state, "mcp_auth", None)
    if provider is None:
        raise HTTPException(status_code=404, detail="MCP authorization is disabled")
    try:
        redirect_uri = await provider.complete_authorization(
            payload.state, int(current_user["id"]), str(current_user["sid"])
        )
    except AuthorizeError as exc:
        raise HTTPException(status_code=400, detail=exc.error_description or exc.error) from exc
    return {"redirect_uri": redirect_uri}


@router.post("/mcp/authorize/deny")
async def deny_mcp_authorization(
    request: Request,
    payload: MCPAuthorizationApproval,
    _current_user: dict = Depends(get_current_user),
):
    """Reject an MCP OAuth request from the authenticated web app."""
    provider = getattr(request.app.state, "mcp_auth", None)
    if provider is None:
        raise HTTPException(status_code=404, detail="MCP authorization is disabled")
    try:
        redirect_uri = await provider.deny_authorization(payload.state)
    except AuthorizeError as exc:
        raise HTTPException(status_code=400, detail=exc.error_description or exc.error) from exc
    return {"redirect_uri": redirect_uri}


@router.get("/mcp/connections")
async def list_mcp_connections(
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    grants = await McpGrantRepository(session).list_active_for_user(current_user["id"])
    return {
        "items": [
            McpConnectionResponse.model_validate(grant).model_dump(mode="json") for grant in grants
        ],
        "total": len(grants),
    }


@router.delete("/mcp/connections")
async def revoke_all_mcp_connections(
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    count = await McpGrantRepository(session).revoke_all_for_user(current_user["id"])
    await session.commit()
    return {"message": "MCP connections revoked", "count": count}


@router.delete("/mcp/connections/{grant_id}")
async def revoke_mcp_connection(
    grant_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repository = McpGrantRepository(session)
    grant = await repository.get_active_by_public_id(current_user["id"], grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="MCP connection not found")
    await repository.revoke(grant)
    await session.commit()
    return {"message": "MCP connection revoked"}


@router.delete("/me/auth-identities/{provider}")
async def unlink_auth_identity(
    provider: str,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    if provider not in {"google", "github"}:
        raise HTTPException(status_code=400, detail="Invalid OAuth provider")
    await auth_service.unlink_auth_identity(current_user["id"], provider)
    return {"message": f"{provider} sign-in unlinked"}


@router.patch("/me/timezone", response_model=UserResponse)
async def update_my_timezone(
    payload: UserTimezoneUpdate,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    return await auth_service.update_user_timezone(current_user["id"], payload.timezone)


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
    default_workspace = await workspace_service.ensure_default_workspace(user.id, user.username)

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

    await auth_service.create_session(
        user_id=user.id,
        sid=sid,
        expires_in=refresh_token_expires,
        initial_token=refresh_token,
    )

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
    issue_csrf_token(
        response,
        max_age=settings.REFRESH_TOKEN_EXPIRE_SECONDS if remember_me else None,
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
    def clear_cookies(resp):
        for key in ("access_token", "refresh_token", "sid"):
            resp.set_cookie(
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
        clear_csrf_token(resp)

    refresh_token = request.cookies.get("refresh_token")

    if not refresh_token:
        resp = JSONResponse(
            status_code=401,
            content={
                "type": "https://lifestack.app/errors/unauthorized",
                "code": "unauthorized",
                "title": "Unauthorized",
                "status": 401,
                "detail": "Refresh token missing",
                "hint": "Authenticate and retry the request.",
                "instance": "/v1/auth/refresh",
            },
            media_type="application/problem+json",
        )
        clear_cookies(resp)
        return resp

    try:
        _username, user_id, sid, default_workspace_id = get_user_info_from_token(
            refresh_token, expected_type="refresh"
        )
    except UnauthorizedError:
        resp = JSONResponse(
            status_code=401,
            content={
                "type": "https://lifestack.app/errors/unauthorized",
                "code": "unauthorized",
                "title": "Unauthorized",
                "status": 401,
                "detail": "Invalid refresh token",
                "hint": "Authenticate and retry the request.",
                "instance": "/v1/auth/refresh",
            },
            media_type="application/problem+json",
        )
        clear_cookies(resp)
        return resp

    # Retrieve the session
    auth_session = await auth_service.session_repo.get_active_by_sid(sid, int(user_id))
    if not auth_session:
        resp = JSONResponse(
            status_code=401,
            content={
                "type": "https://lifestack.app/errors/unauthorized",
                "code": "unauthorized",
                "title": "Unauthorized",
                "status": 401,
                "detail": "Session is no longer active",
                "hint": "Authenticate and retry the request.",
                "instance": "/v1/auth/refresh",
            },
            media_type="application/problem+json",
        )
        clear_cookies(resp)
        return resp

    user = await auth_service.get_user_by_id(int(user_id))
    if not user or not user.is_active:
        resp = JSONResponse(
            status_code=401,
            content={
                "type": "https://lifestack.app/errors/unauthorized",
                "code": "unauthorized",
                "title": "Unauthorized",
                "status": 401,
                "detail": "User account is inactive or no longer exists",
                "hint": "Authenticate and retry the request.",
                "instance": "/v1/auth/refresh",
            },
            media_type="application/problem+json",
        )
        clear_cookies(resp)
        return resp

    if default_workspace_id is None:
        default_workspace = await workspace_service.ensure_default_workspace(user.id, user.username)
        default_workspace_id = default_workspace.id

    incoming_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    now = datetime.now(UTC)

    access_token_expires = timedelta(seconds=settings.ACCESS_TOKEN_EXPIRE_SECONDS)
    refresh_token_expires = timedelta(seconds=settings.REFRESH_TOKEN_EXPIRE_SECONDS)
    new_refresh_token: str | None = None

    if incoming_hash == auth_session.current_token_hash:
        # Valid rotation path
        new_refresh_token = create_token(
            data={
                "sub": user.username,
                "sub_id": str(user.id),
                "default_workspace_id": default_workspace_id,
            },
            expires_delta=refresh_token_expires,
            sid=sid,
            token_type="refresh",
        )
        auth_session.previous_token_hash = auth_session.current_token_hash
        auth_session.current_token_hash = hashlib.sha256(
            new_refresh_token.encode("utf-8")
        ).hexdigest()
        auth_session.rotated_at = now
        auth_session.last_seen_at = now
        auth_session.expires_at = now + refresh_token_expires
        auth_service.session_repo.session.add(auth_session)
        await auth_service.session_repo.session.flush()
    elif incoming_hash == auth_session.previous_token_hash:
        # Check grace period (5 seconds)
        if auth_session.rotated_at is not None:
            elapsed = now - auth_session.rotated_at
            elapsed_seconds = elapsed.total_seconds()
        else:
            elapsed_seconds = float("inf")

        if elapsed_seconds <= 5.0:
            # Concurrent retry recovery: issue a new access token, but do not
            # overwrite the refresh token already rotated by the first request.
            auth_session.last_seen_at = now
            auth_service.session_repo.session.add(auth_session)
            await auth_service.session_repo.session.flush()
        else:
            # Replay attack outside grace period! Revoke entire session family
            auth_session.revoked_at = now
            auth_session.last_seen_at = now
            auth_service.session_repo.session.add(auth_session)
            await auth_service.session_repo.session.flush()
            resp = JSONResponse(
                status_code=401,
                content={
                    "type": "https://lifestack.app/errors/unauthorized",
                    "code": "unauthorized",
                    "title": "Unauthorized",
                    "status": 401,
                    "detail": "Replay attack detected",
                    "hint": "Authenticate and retry the request.",
                    "instance": "/v1/auth/refresh",
                },
                media_type="application/problem+json",
            )
            clear_cookies(resp)
            return resp
    else:
        # Replay attack: token hash doesn't match current or previous!
        auth_session.revoked_at = now
        auth_session.last_seen_at = now
        auth_service.session_repo.session.add(auth_session)
        await auth_service.session_repo.session.flush()
        resp = JSONResponse(
            status_code=401,
            content={
                "type": "https://lifestack.app/errors/unauthorized",
                "code": "unauthorized",
                "title": "Unauthorized",
                "status": 401,
                "detail": "Replay attack detected",
                "hint": "Authenticate and retry the request.",
                "instance": "/v1/auth/refresh",
            },
            media_type="application/problem+json",
        )
        clear_cookies(resp)
        return resp

    # Generate new access token
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

    # Set response cookies
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
    if new_refresh_token is not None:
        response.set_cookie(
            key="refresh_token",
            value=new_refresh_token,
            httponly=True,
            max_age=settings.REFRESH_TOKEN_EXPIRE_SECONDS,
            path="/",
            samesite=settings.COOKIE_SAMESITE,
            domain=settings.COOKIE_DOMAIN,
            secure=settings.COOKIE_SECURE,
        )
    response.set_cookie(
        key="sid",
        value=sid,
        httponly=True,
        max_age=settings.REFRESH_TOKEN_EXPIRE_SECONDS,
        path="/",
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        secure=settings.COOKIE_SECURE,
    )
    issue_csrf_token(response, max_age=settings.REFRESH_TOKEN_EXPIRE_SECONDS)

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
    clear_csrf_token(response)
    return {"message": "Logged out successfully"}


@router.post("/change-password")
async def change_password(
    response: Response,
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
    clear_csrf_token(response)
    return {"message": "Password changed successfully"}


@router.post("/set-password")
async def set_password(
    response: Response,
    data: PasswordSet,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    await auth_service.set_password(current_user["id"], data.new_password)
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
    clear_csrf_token(response)
    return {"message": "Password configured successfully"}


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
    clear_csrf_token(response)
    return {"message": "Logged out from all devices"}


@router.post("/forgot-password")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def forgot_password(
    request: Request,
    data: ForgotPasswordRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    await auth_service.forgot_password(data.email)
    return {"message": "If the email is registered, a password reset link has been sent."}


@router.post("/reset-password")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def reset_password(
    request: Request,
    data: ResetPasswordRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    try:
        await auth_service.reset_password(data.token, data.new_password)
    except UnauthorizedError as e:
        raise HTTPException(status_code=400, detail=e.detail) from e
    return {"message": "Password has been reset successfully."}
