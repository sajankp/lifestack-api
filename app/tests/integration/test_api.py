import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_check(client: AsyncClient):
    """Verify that the health check endpoint returns ok."""
    response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_auth_registration_and_login(client: AsyncClient):
    """Verify that we can register and login."""
    # Register
    register_response = await client.post(
        "/v1/auth/register",
        json={"email": "test@example.com", "username": "testuser", "password": "TestPass123!"},
    )
    assert register_response.status_code == 200
    assert register_response.json() is True

    # Login
    login_response = await client.post(
        "/v1/auth/login", data={"username": "testuser", "password": "TestPass123!"}
    )
    assert login_response.status_code == 200
    assert "access_token" in login_response.cookies
    assert "refresh_token" in login_response.cookies


@pytest.mark.asyncio
async def test_duplicate_registration_returns_problem_details(client: AsyncClient):
    payload = {
        "email": "duplicate@example.com",
        "username": "duplicateuser",
        "password": "TestPass123!",
    }
    first = await client.post("/v1/auth/register", json=payload)
    assert first.status_code == 200

    second = await client.post("/v1/auth/register", json=payload)
    assert second.status_code == 409
    assert second.headers["content-type"].startswith("application/problem+json")
    body = second.json()
    assert body["type"] == "https://lifestack.app/errors/conflict"
    assert body["title"] == "Email Already Registered"
    assert body["status"] == 409


@pytest.mark.asyncio
async def test_logout_revokes_server_side_session(client: AsyncClient):
    await client.post(
        "/v1/auth/register",
        json={
            "email": "session@example.com",
            "username": "sessionuser",
            "password": "TestPass123!",
        },
    )
    login_response = await client.post(
        "/v1/auth/login", data={"username": "sessionuser", "password": "TestPass123!"}
    )
    assert login_response.status_code == 200

    access_token = login_response.cookies["access_token"]
    refresh_token = login_response.cookies["refresh_token"]

    me_before_logout = await client.get("/v1/auth/me")
    assert me_before_logout.status_code == 200

    logout_response = await client.post("/v1/auth/logout")
    assert logout_response.status_code == 200

    me_after_logout = await client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert me_after_logout.status_code == 401
    assert me_after_logout.json()["detail"] == "Session is no longer active"

    refresh_after_logout = await client.post(
        "/v1/auth/refresh", cookies={"refresh_token": refresh_token}
    )
    assert refresh_after_logout.status_code == 401
    assert refresh_after_logout.json()["detail"] == "Session is no longer active"


@pytest.mark.asyncio
async def test_cookie_authenticated_mutation_rejects_untrusted_origin(client: AsyncClient):
    await client.post(
        "/v1/auth/register",
        json={
            "email": "csrf@example.com",
            "username": "csrfuser",
            "password": "TestPass123!",
        },
    )
    login_response = await client.post(
        "/v1/auth/login", data={"username": "csrfuser", "password": "TestPass123!"}
    )
    assert login_response.status_code == 200

    response = await client.post(
        "/v1/auth/logout",
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "https://lifestack.app/errors/csrf-check-failed"
    assert body["title"] == "CSRF Check Failed"


@pytest.mark.asyncio
async def test_todo_protected(client: AsyncClient):
    """Verify that the todo endpoint is protected."""
    response = await client.get("/v1/todo/")
    assert response.status_code == 401
    assert response.json()["title"] == "Unauthorized"
