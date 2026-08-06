from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from app.auth.models import User
from app.auth.oauth import oauth_callback, oauth_login
from app.auth.service import AuthService
from app.config import settings


@pytest.mark.asyncio
async def test_create_oauth_user_saves_model_without_password_schema_conversion():
    user_repo = MagicMock()
    user_repo.get_by_username = AsyncMock(return_value=None)
    user_repo.save = AsyncMock(side_effect=lambda user: user)
    user_repo.add_auth_identity = AsyncMock()
    service = AuthService(user_repo, MagicMock())

    user = await service.create_oauth_user(
        email="oauth@example.com",
        username="oauth-user",
        oauth_provider="google",
        oauth_sub="google-subject",
    )

    user_repo.save.assert_awaited_once_with(user)
    assert user.hashed_password == ""
    assert user.oauth_provider == "google"
    assert user.oauth_sub == "google-subject"


@pytest.mark.asyncio
async def test_oauth_callback_uses_provider_identity_and_sets_cookies_on_redirect():
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/v1/auth/oauth/google/callback",
        "headers": [],
        "query_string": b"",
        "scheme": "https",
        "server": ("api.example.com", 443),
        "client": ("127.0.0.1", 1234),
    })
    oauth_client = MagicMock()
    oauth_client.authorize_access_token = AsyncMock(
        return_value={
            "userinfo": {
                "sub": "google-subject",
                "email": "new-email@example.com",
            }
        }
    )
    existing_user = User(
        id=42,
        email="old-email@example.com",
        username="existing-user",
        hashed_password="",
        oauth_provider="google",
        oauth_sub="google-subject",
    )
    user_repo = MagicMock()
    user_repo.get_by_oauth_identity = AsyncMock(return_value=existing_user)
    user_repo.get_auth_identity = AsyncMock(return_value=None)
    user_repo.get_by_email = AsyncMock()
    auth_service = MagicMock()
    auth_service.create_session = AsyncMock()
    auth_service.create_oauth_user = AsyncMock()
    workspace_service = MagicMock()
    workspace_service.ensure_default_workspace = AsyncMock(return_value=SimpleNamespace(id=7))

    with patch("app.auth.oauth._get_oauth_client", return_value=oauth_client):
        redirect = await oauth_callback(
            request=request,
            provider="google",
            auth_service=auth_service,
            user_repo=user_repo,
            workspace_service=workspace_service,
        )

    user_repo.get_by_oauth_identity.assert_awaited_once_with("google", "google-subject")
    user_repo.get_by_email.assert_not_awaited()
    auth_service.create_oauth_user.assert_not_awaited()
    assert redirect.status_code == 307
    set_cookie_headers = [
        value.decode() for key, value in redirect.raw_headers if key.lower() == b"set-cookie"
    ]
    assert any(header.startswith("access_token=") for header in set_cookie_headers)
    assert any(header.startswith("refresh_token=") for header in set_cookie_headers)
    assert any(header.startswith("sid=") for header in set_cookie_headers)
    assert any(header.startswith("csrf_token=") for header in set_cookie_headers)


@pytest.mark.asyncio
async def test_oauth_login_uses_explicit_provider_redirect_uri():
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/v1/auth/oauth/google",
        "headers": [(b"host", b"internal-api:8000")],
        "query_string": b"",
        "scheme": "http",
        "server": ("internal-api", 8000),
        "client": ("127.0.0.1", 1234),
    })
    oauth_client = MagicMock()
    oauth_client.authorize_redirect = AsyncMock(return_value="redirect-response")
    original_redirect_uri = settings.GOOGLE_REDIRECT_URI
    settings.GOOGLE_REDIRECT_URI = "https://lifestack-api.example.com/v1/auth/oauth/google/callback"
    try:
        with patch("app.auth.oauth._get_oauth_client", return_value=oauth_client):
            result = await oauth_login(request, "google")
    finally:
        settings.GOOGLE_REDIRECT_URI = original_redirect_uri

    assert result == "redirect-response"
    oauth_client.authorize_redirect.assert_awaited_once_with(
        request,
        "https://lifestack-api.example.com/v1/auth/oauth/google/callback",
    )


def test_user_model_enforces_unique_oauth_identity():
    unique_column_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in User.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert ("oauth_provider", "oauth_sub") in unique_column_sets
