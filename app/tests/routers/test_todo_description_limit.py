"""Todo and recurring-rule descriptions accept up to 2000 characters (TEXT column)."""

import pytest
from httpx import AsyncClient

LIMIT = 2000


async def _login(client: AsyncClient, name: str) -> None:
    user = {
        "email": f"{name}@example.com",
        "username": name,
        "password": "TestPass123!",
    }
    assert (await client.post("/v1/auth/register", json=user)).status_code == 200
    login = await client.post(
        "/v1/auth/login", data={"username": user["username"], "password": user["password"]}
    )
    assert login.status_code == 200


@pytest.mark.asyncio
async def test_todo_description_round_trips_at_limit_and_rejects_over(client: AsyncClient):
    await _login(client, "tododesclimit")
    text = "x" * LIMIT

    created = await client.post("/v1/todo/", json={"title": "Long note", "description": text})
    assert created.status_code == 201
    public_id = created.json()["public_id"]

    fetched = await client.get(f"/v1/todo/{public_id}")
    assert fetched.status_code == 200
    assert fetched.json()["description"] == text

    over = await client.post(
        "/v1/todo/", json={"title": "Too long", "description": "x" * (LIMIT + 1)}
    )
    assert over.status_code == 422

    patched = await client.patch(f"/v1/todo/{public_id}", json={"description": "y" * LIMIT})
    assert patched.status_code == 200
    assert patched.json()["description"] == "y" * LIMIT

    patched_over = await client.patch(
        f"/v1/todo/{public_id}", json={"description": "y" * (LIMIT + 1)}
    )
    assert patched_over.status_code == 422


@pytest.mark.asyncio
async def test_recurring_rule_description_round_trips_at_limit_and_rejects_over(
    client: AsyncClient,
):
    await _login(client, "ruledesclimit")
    payload = {
        "title": "Long rule note",
        "priority": "medium",
        "frequency": "monthly",
        "interval": 1,
        "anchor_date": "2026-01-01",
    }

    created = await client.post("/v1/todo/recurring/", json={**payload, "description": "r" * LIMIT})
    assert created.status_code == 201
    rule = created.json()
    assert rule["description"] == "r" * LIMIT

    over = await client.post(
        "/v1/todo/recurring/", json={**payload, "description": "r" * (LIMIT + 1)}
    )
    assert over.status_code == 422

    patched = await client.patch(
        f"/v1/todo/recurring/{rule['public_id']}", json={"description": "s" * LIMIT}
    )
    assert patched.status_code == 200
    assert patched.json()["description"] == "s" * LIMIT

    patched_over = await client.patch(
        f"/v1/todo/recurring/{rule['public_id']}", json={"description": "s" * (LIMIT + 1)}
    )
    assert patched_over.status_code == 422
