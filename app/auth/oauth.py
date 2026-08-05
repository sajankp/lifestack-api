"""OAuth authentication endpoints for Google and GitHub."""

import uuid
from datetime import timedelta
from typing import Annotated

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth.repository import UserRepository
from app.auth.service import AuthService
from app.config import settings
from app.core.auth import create_token
from app.core.csrf import issue_csrf_token
from app.core.dependencies import (
    get_auth_service,
    get_user_repo,
    get_workspace_service,
)
from app.platform.service import WorkspaceService

router = APIRouter(prefix="/oauth", tags=["oauth"])

# Configure OAuth clients
oauth = OAuth()
oauth.register(
    name="google",
    client_id=settings.GOOGLE_CLIENT_ID,
    client_secret=settings.GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)
oauth.register(
    name="github",
    client_id=settings.GITHUB_CLIENT_ID,
    client_secret=settings.GITHUB_CLIENT_SECRET,
    access_token_url="https://github.com/login/oauth/access_token",
    authorize_url="https://github.com/login/oauth/authorize",
    api_base_url="https://api.github.com/",
    client_kwargs={"scope": "read:user user:email"},
)


def _get_oauth_client(provider: str):
    """Get the OAuth client for the given provider."""
    if provider not in ("google", "github"):
        raise HTTPException(status_code=400, detail="Invalid OAuth provider")
    return oauth.create_client(provider)


@router.get("/{provider}")
async def oauth_login(request: Request, provider: str):
    """Initiate OAuth login flow for the given provider."""
    client = _get_oauth_client(provider)
    redirect_uri = request.url_for("oauth_callback", provider=provider)
    return await client.authorize_redirect(request, str(redirect_uri))


@router.get("/{provider}/callback", name="oauth_callback")
async def oauth_callback(
    request: Request,
    provider: str,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    user_repo: Annotated[UserRepository, Depends(get_user_repo)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
):
    """Handle OAuth callback from provider."""
    client = _get_oauth_client(provider)

    try:
        token = await client.authorize_access_token(request)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth authorization failed: {str(e)}") from e

    # Get user info from provider
    if provider == "google":
        user_info = token.get("userinfo")
        if not user_info:
            resp = await client.get("userinfo")
            user_info = resp.json()
        oauth_sub = user_info["sub"]
        email = user_info["email"]
        username = email.split("@")[0]
    else:  # github
        resp = await client.get("user", token=token)
        user_info = resp.json()
        oauth_sub = str(user_info["id"])
        # GitHub may not expose email publicly
        email_resp = await client.get("user/emails", token=token)
        emails = email_resp.json()
        primary_email = next((e["email"] for e in emails if e["primary"]), None)
        email = primary_email or f"{user_info['login']}@github.local"
        username = user_info["login"]

    # Provider identity is stable even when the provider-side email changes.
    user = await user_repo.get_by_oauth_identity(provider, oauth_sub)
    if user is None:
        existing_email_user = await user_repo.get_by_email(email)
        if existing_email_user is not None:
            raise HTTPException(
                status_code=400,
                detail="Account exists with different login method. Use original login method.",
            )
        user = await auth_service.create_oauth_user(
            email=email,
            username=username,
            oauth_provider=provider,
            oauth_sub=oauth_sub,
        )

    # user.id is guaranteed to be set after creation/lookup
    assert user.id is not None
    # Same flow as password login - generate SID, session, JWTs
    sid = str(uuid.uuid4())
    default_workspace = await workspace_service.ensure_default_workspace(user.id, user.username)

    refresh_token_expires = timedelta(seconds=settings.REFRESH_TOKEN_EXPIRE_SECONDS)
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
        user_id=user.id, sid=sid, expires_in=refresh_token_expires, initial_token=refresh_token
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

    frontend_url = settings.FRONTEND_URL.rstrip("/")
    response = RedirectResponse(url=f"{frontend_url}/?auth=success")

    # Set cookies on the response that will actually be returned.
    cookie_kwargs = {
        "httponly": True,
        "max_age": settings.ACCESS_TOKEN_EXPIRE_SECONDS,
        "path": "/",
        "samesite": settings.COOKIE_SAMESITE,
        "domain": settings.COOKIE_DOMAIN,
        "secure": settings.COOKIE_SECURE,
    }
    response.set_cookie(key="access_token", value=access_token, **cookie_kwargs)

    cookie_kwargs["max_age"] = settings.REFRESH_TOKEN_EXPIRE_SECONDS
    response.set_cookie(key="refresh_token", value=refresh_token, **cookie_kwargs)
    response.set_cookie(key="sid", value=sid, **cookie_kwargs)
    issue_csrf_token(response, max_age=settings.REFRESH_TOKEN_EXPIRE_SECONDS)

    return response
