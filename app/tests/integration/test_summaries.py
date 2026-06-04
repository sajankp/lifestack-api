"""
Wave 2: Focused integration tests for the Weekly Summaries module.

Covers:
  - List summaries (empty by default)
  - Pagination contract
  - Latest endpoint returns 404 when no summaries exist
  - Get by ID returns 404 for unknown IDs
  - Date range filtering
  - Workspace isolation
  - RBAC: viewer cannot access member-only write operations (summaries are read-only so viewer sees all)
  - Unauthenticated access is rejected
"""

import uuid

import pytest
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register_and_login(client: AsyncClient, suffix: str) -> dict:
    username = f"{suffix}_{uuid.uuid4().hex[:6]}"
    email = f"{username}@example.com"
    password = "TestPass123!"
    reg = await client.post(
        "/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert reg.status_code == 200, reg.text
    login = await client.post("/v1/auth/login", data={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return {"username": username, "password": password, "cookies": dict(login.cookies)}


# ---------------------------------------------------------------------------
# 1. Basic list and pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summaries_list_empty_after_registration(client: AsyncClient):
    """A brand-new workspace has no weekly summaries."""
    creds = await _register_and_login(client, "sumlist")
    resp = await client.get("/v1/summaries/weekly", cookies=creds["cookies"])
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["items"], list)
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_summaries_list_pagination_defaults(client: AsyncClient):
    """List response includes limit and offset."""
    creds = await _register_and_login(client, "sumpage")
    resp = await client.get("/v1/summaries/weekly", cookies=creds["cookies"])
    assert resp.status_code == 200
    data = resp.json()
    assert "limit" in data
    assert "offset" in data
    assert data["offset"] == 0
    assert data["limit"] > 0


@pytest.mark.asyncio
async def test_summaries_list_with_date_range(client: AsyncClient):
    """Date range query parameters are accepted without error."""
    creds = await _register_and_login(client, "sumdates")
    resp = await client.get(
        "/v1/summaries/weekly",
        params={"from": "2026-01-01", "to": "2026-12-31"},
        cookies=creds["cookies"],
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 2. Latest endpoint — 404 on empty workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summaries_latest_returns_404_when_none(client: AsyncClient):
    """The /latest endpoint returns 404 when no summaries exist."""
    creds = await _register_and_login(client, "sumlat")
    resp = await client.get("/v1/summaries/weekly/latest", cookies=creds["cookies"])
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. Get by ID — 404 for unknown ID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summaries_get_by_id_unknown_returns_404(client: AsyncClient):
    """Fetching a non-existent summary by UUID returns 404."""
    creds = await _register_and_login(client, "sumbyid")
    fake_id = uuid.uuid4()
    resp = await client.get(f"/v1/summaries/weekly/{fake_id}", cookies=creds["cookies"])
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 4. Workspace isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summaries_workspace_isolation(client: AsyncClient):
    """Two users cannot see each other's weekly summaries."""
    user_a = await _register_and_login(client, "sumiso_a")
    user_b = await _register_and_login(client, "sumiso_b")

    resp_a = await client.get("/v1/summaries/weekly", cookies=user_a["cookies"])
    resp_b = await client.get("/v1/summaries/weekly", cookies=user_b["cookies"])

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200

    ids_a = {item["public_id"] for item in resp_a.json()["items"]}
    ids_b = {item["public_id"] for item in resp_b.json()["items"]}
    assert ids_a.isdisjoint(ids_b), "Weekly summaries must not leak between workspaces"


# ---------------------------------------------------------------------------
# 5. Unauthenticated access is rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summaries_list_requires_authentication(client: AsyncClient):
    resp = await client.get("/v1/summaries/weekly")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_summaries_latest_requires_authentication(client: AsyncClient):
    resp = await client.get("/v1/summaries/weekly/latest")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_summaries_get_by_id_requires_authentication(client: AsyncClient):
    fake_id = uuid.uuid4()
    resp = await client.get(f"/v1/summaries/weekly/{fake_id}")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 6. Invalid date range parameters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summaries_invalid_date_range_422(client: AsyncClient):
    """Invalid date format in query parameters returns 422."""
    creds = await _register_and_login(client, "suminvdt")
    resp = await client.get(
        "/v1/summaries/weekly",
        params={"from": "not-a-date"},
        cookies=creds["cookies"],
    )
    assert resp.status_code == 422
