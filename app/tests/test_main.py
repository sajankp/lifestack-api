import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app, create_app


@pytest.mark.asyncio
async def test_health_check():
    """Verify that the health check endpoint returns 200 OK."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "version": settings.VERSION}


@pytest.mark.asyncio
async def test_docs_reachable():
    """Verify that the OpenAPI docs are reachable."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/docs")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_versioned_openapi_reachable():
    """Verify that the versioned OpenAPI schema stays publicly reachable."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"{settings.API_V1_STR}/openapi.json")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_redoc_reachable_outside_production():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/redoc")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_api_documentation_is_disabled_in_production(monkeypatch):
    monkeypatch.setattr(settings, "ENV", "production")
    production_app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=production_app),
        base_url="http://test",
    ) as client:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/redoc")).status_code == 404
        assert (await client.get(f"{settings.API_V1_STR}/openapi.json")).status_code == 404
        assert (await client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_security_headers_middleware_trusted_proxy():
    """Verify HSTS headers behavior for trusted/untrusted proxies on X-Forwarded-Proto."""
    original_proxies = settings.TRUSTED_PROXIES
    original_cookie_secure = settings.COOKIE_SECURE
    settings.COOKIE_SECURE = False  # Ensure HSTS depends purely on proxy trust

    # 1. Untrusted client IP (not in TRUSTED_PROXIES) with X-Forwarded-Proto: https
    settings.TRUSTED_PROXIES = ["1.2.3.4"]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health", headers={"x-forwarded-proto": "https"})
            assert response.status_code == 200
            assert "strict-transport-security" not in response.headers
    finally:
        settings.TRUSTED_PROXIES = original_proxies
        settings.COOKIE_SECURE = original_cookie_secure

    # 2. Trusted client IP (in TRUSTED_PROXIES) with X-Forwarded-Proto: https
    settings.TRUSTED_PROXIES = ["127.0.0.1"]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health", headers={"x-forwarded-proto": "https"})
            assert response.status_code == 200
            assert "strict-transport-security" in response.headers
            assert (
                response.headers["strict-transport-security"]
                == "max-age=31536000; includeSubDomains"
            )
    finally:
        settings.TRUSTED_PROXIES = original_proxies
        settings.COOKIE_SECURE = original_cookie_secure
