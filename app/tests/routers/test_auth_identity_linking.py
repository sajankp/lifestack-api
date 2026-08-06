from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from starlette.responses import RedirectResponse

from app.auth.models import User, UserAuthIdentity
from app.core.database import postgres


async def _seed_oauth_user(email: str, username: str, provider: str, subject: str) -> None:
    async with postgres.async_session_maker() as session:
        user = User(
            email=email,
            username=username,
            hashed_password="",
            oauth_provider=provider,
            oauth_sub=subject,
            timezone="UTC",
        )
        session.add(user)
        await session.flush()
        session.add(UserAuthIdentity(user_id=user.id, provider=provider, subject=subject))
        await session.commit()


def _oauth_client(subject: str, email: str) -> MagicMock:
    client = MagicMock()
    client.authorize_access_token = AsyncMock(
        return_value={"userinfo": {"sub": subject, "email": email}}
    )
    client.authorize_redirect = AsyncMock(return_value=RedirectResponse("https://provider.test"))
    client.get = AsyncMock(
        side_effect=[
            SimpleNamespace(json=lambda: {"id": subject, "login": subject}),
            SimpleNamespace(json=lambda: [{"email": email, "primary": True}]),
        ]
    )
    return client


@pytest.mark.asyncio
async def test_existing_oauth_user_can_link_second_provider_and_list_both(client: AsyncClient):
    await _seed_oauth_user("link@example.com", "link-user", "google", "google-link-sub")
    google_client = _oauth_client("google-link-sub", "link@example.com")
    github_client = _oauth_client("github-link-sub", "link@example.com")

    with patch(
        "app.auth.oauth._get_oauth_client",
        side_effect=lambda provider: google_client if provider == "google" else github_client,
    ):
        login = await client.get("/v1/auth/oauth/google/callback", follow_redirects=False)
        assert login.status_code == 307
        start_link = await client.get("/v1/auth/oauth/github/link", follow_redirects=False)
        assert start_link.status_code == 307
        linked = await client.get("/v1/auth/oauth/github/callback", follow_redirects=False)
        assert linked.status_code == 307

    identities = await client.get("/v1/auth/me/auth-identities")
    assert identities.status_code == 200
    assert identities.json()["providers"] == ["github", "google"]

    unlinked = await client.delete("/v1/auth/me/auth-identities/github")
    assert unlinked.status_code == 200
    remaining = await client.get("/v1/auth/me/auth-identities")
    assert remaining.json()["providers"] == ["google"]
    last_method = await client.delete("/v1/auth/me/auth-identities/google")
    assert last_method.status_code == 401


@pytest.mark.asyncio
async def test_new_oauth_identity_email_collision_returns_conflict(client: AsyncClient):
    async with postgres.async_session_maker() as session:
        session.add(
            User(
                email="collision@example.com",
                username="collision-user",
                hashed_password="not-empty",
            )
        )
        await session.commit()

    client_mock = _oauth_client("github-collision-sub", "collision@example.com")
    with patch("app.auth.oauth._get_oauth_client", return_value=client_mock):
        response = await client.get("/v1/auth/oauth/github/callback")
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_oauth_only_user_can_set_password_and_login(client: AsyncClient):
    await _seed_oauth_user("password@example.com", "password-user", "google", "google-password-sub")
    oauth_client = _oauth_client("google-password-sub", "password@example.com")
    with patch("app.auth.oauth._get_oauth_client", return_value=oauth_client):
        login = await client.get("/v1/auth/oauth/google/callback", follow_redirects=False)
    assert login.status_code == 307

    set_password = await client.post(
        "/v1/auth/set-password", json={"new_password": "PasswordSetup123!"}
    )
    assert set_password.status_code == 200
    password_login = await client.post(
        "/v1/auth/login",
        data={"username": "password@example.com", "password": "PasswordSetup123!"},
    )
    assert password_login.status_code == 200
