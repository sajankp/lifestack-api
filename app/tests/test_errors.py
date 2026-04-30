import pytest
from fastapi import APIRouter, Depends, Request
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.auth.repository import AuthSessionRepository
from app.core.auth import create_token
from app.core.dependencies import get_current_user, limiter
from app.core.exceptions import NotFoundError
from app.main import create_app


class Item(BaseModel):
    name: str


@pytest.fixture
def error_app(monkeypatch):
    async def _fake_get_active_by_sid(self, sid: str, user_id: int | None = None):
        return object()

    monkeypatch.setattr(AuthSessionRepository, "get_active_by_sid", _fake_get_active_by_sid)

    app = create_app()
    error_router = APIRouter()

    @error_router.get("/test-404")
    async def trigger_404():
        raise NotFoundError(
            detail="The requested resource could not be found.",
        )

    @error_router.post("/test-422")
    async def trigger_422(item: Item):
        return item

    @error_router.get("/test-500")
    async def trigger_500():
        raise ValueError("Something went terribly wrong!")

    @error_router.get("/test-auth")
    async def trigger_auth(user=Depends(get_current_user)):
        return {"ok": True}

    app.include_router(error_router)
    return app


@pytest.fixture
def auth_headers():
    token = create_token(
        data={"sub": "tester", "sub_id": "1"},
        sid="test-session",
        token_type="access",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def rate_limited_app(monkeypatch):
    async def _fake_get_active_by_sid(self, sid: str, user_id: int | None = None):
        return object()

    monkeypatch.setattr(AuthSessionRepository, "get_active_by_sid", _fake_get_active_by_sid)

    limiter.enabled = True
    limiter._storage.reset()

    app = create_app()
    rate_router = APIRouter()

    @rate_router.get("/test-429")
    @limiter.limit("1/minute")
    async def trigger_429(request: Request):
        return {"ok": True}

    app.include_router(rate_router)
    yield app
    limiter._storage.reset()


@pytest.mark.asyncio
async def test_api_error_returns_rfc7807(error_app, auth_headers):
    async with AsyncClient(
        transport=ASGITransport(app=error_app), base_url="http://test"
    ) as client:
        response = await client.get("/test-404", headers=auth_headers)
        assert response.status_code == 404
        data = response.json()
        assert data["type"] == "https://lifestack.app/errors/not-found"
        assert data["title"] == "Not Found"
        assert data["status"] == 404
        assert data["detail"] == "The requested resource could not be found."
        assert data["instance"] == "/test-404"


@pytest.mark.asyncio
async def test_validation_error_returns_rfc7807(error_app, auth_headers):
    async with AsyncClient(
        transport=ASGITransport(app=error_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/test-422", json={"wrong_field": "value"}, headers=auth_headers
        )
        assert response.status_code == 422
        data = response.json()
        assert data["type"] == "https://lifestack.app/errors/validation-error"
        assert data["title"] == "Request Validation Error"
        assert data["status"] == 422
        assert "detail" in data
        assert data["instance"] == "/test-422"
        assert "errors" in data
        assert len(data["errors"]) > 0


@pytest.mark.asyncio
async def test_unhandled_exception_returns_rfc7807(error_app, auth_headers):
    async with AsyncClient(
        transport=ASGITransport(app=error_app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/test-500", headers=auth_headers)
        assert response.status_code == 500
        data = response.json()
        assert data["type"] == "https://lifestack.app/errors/internal-server-error"
        assert data["title"] == "Internal Server Error"
        assert data["status"] == 500
        assert data["instance"] == "/test-500"
        assert "detail" in data


@pytest.mark.asyncio
async def test_invalid_token_returns_problem_details(error_app):
    async with AsyncClient(
        transport=ASGITransport(app=error_app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/test-auth", headers={"Authorization": "Bearer invalid-token"})
        assert response.status_code == 401
        data = response.json()
        assert data["type"] == "https://lifestack.app/errors/unauthorized"
        assert data["title"] == "Unauthorized"
        assert data["status"] == 401
        assert data["instance"] == "/test-auth"
        assert "detail" in data


@pytest.mark.asyncio
async def test_rate_limit_returns_problem_details(rate_limited_app, auth_headers):
    async with AsyncClient(
        transport=ASGITransport(app=rate_limited_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        first = await client.get("/test-429", headers=auth_headers)
        assert first.status_code == 200

        second = await client.get("/test-429", headers=auth_headers)
        assert second.status_code == 429
        assert second.headers["content-type"].startswith("application/problem+json")
        data = second.json()
        assert data["type"] == "https://lifestack.app/errors/rate-limit-exceeded"
        assert data["title"] == "Rate Limit Exceeded"
        assert data["status"] == 429
        assert data["instance"] == "/test-429"
