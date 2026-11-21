"""Todo and recurring-rule descriptions accept up to 20,000 characters (TEXT column),
titles up to 200, and todos carry an optional category label."""

import pytest
from httpx import AsyncClient

DESCRIPTION_LIMIT = 20000
TITLE_LIMIT = 200


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
    text = "x" * DESCRIPTION_LIMIT

    created = await client.post("/v1/todo/", json={"title": "Long note", "description": text})
    assert created.status_code == 201
    public_id = created.json()["public_id"]

    fetched = await client.get(f"/v1/todo/{public_id}")
    assert fetched.status_code == 200
    assert fetched.json()["description"] == text

    over = await client.post(
        "/v1/todo/", json={"title": "Too long", "description": "x" * (DESCRIPTION_LIMIT + 1)}
    )
    assert over.status_code == 422

    patched = await client.patch(
        f"/v1/todo/{public_id}", json={"description": "y" * DESCRIPTION_LIMIT}
    )
    assert patched.status_code == 200
    assert patched.json()["description"] == "y" * DESCRIPTION_LIMIT

    patched_over = await client.patch(
        f"/v1/todo/{public_id}", json={"description": "y" * (DESCRIPTION_LIMIT + 1)}
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

    created = await client.post(
        "/v1/todo/recurring/", json={**payload, "description": "r" * DESCRIPTION_LIMIT}
    )
    assert created.status_code == 201
    rule = created.json()
    assert rule["description"] == "r" * DESCRIPTION_LIMIT

    over = await client.post(
        "/v1/todo/recurring/", json={**payload, "description": "r" * (DESCRIPTION_LIMIT + 1)}
    )
    assert over.status_code == 422

    patched = await client.patch(
        f"/v1/todo/recurring/{rule['public_id']}", json={"description": "s" * DESCRIPTION_LIMIT}
    )
    assert patched.status_code == 200
    assert patched.json()["description"] == "s" * DESCRIPTION_LIMIT

    patched_over = await client.patch(
        f"/v1/todo/recurring/{rule['public_id']}",
        json={"description": "s" * (DESCRIPTION_LIMIT + 1)},
    )
    assert patched_over.status_code == 422


@pytest.mark.asyncio
async def test_todo_title_round_trips_at_limit_and_rejects_over(client: AsyncClient):
    await _login(client, "todotitlelimit")
    title = "t" * TITLE_LIMIT

    created = await client.post("/v1/todo/", json={"title": title})
    assert created.status_code == 201
    public_id = created.json()["public_id"]

    fetched = await client.get(f"/v1/todo/{public_id}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == title

    over = await client.post("/v1/todo/", json={"title": "t" * (TITLE_LIMIT + 1)})
    assert over.status_code == 422

    patched = await client.patch(f"/v1/todo/{public_id}", json={"title": "u" * TITLE_LIMIT})
    assert patched.status_code == 200
    assert patched.json()["title"] == "u" * TITLE_LIMIT


@pytest.mark.asyncio
async def test_todo_category_create_update_and_list_filter(client: AsyncClient):
    await _login(client, "todocategory")

    finance = await client.post("/v1/todo/", json={"title": "Buy gold", "category": "Finance"})
    assert finance.status_code == 201
    finance_id = finance.json()["public_id"]
    assert finance.json()["category"] == "Finance"

    uncategorised = await client.post("/v1/todo/", json={"title": "Buy milk"})
    assert uncategorised.status_code == 201
    assert uncategorised.json()["category"] is None

    # Read back intact
    fetched = await client.get(f"/v1/todo/{finance_id}")
    assert fetched.status_code == 200
    assert fetched.json()["category"] == "Finance"

    # Update category (set and clear)
    updated = await client.patch(f"/v1/todo/{finance_id}", json={"category": "Finance: bullion"})
    assert updated.status_code == 200
    assert updated.json()["category"] == "Finance: bullion"

    cleared = await client.patch(f"/v1/todo/{finance_id}", json={"category": None})
    assert cleared.status_code == 200
    assert cleared.json()["category"] is None

    # Re-set Finance for the filter assertion
    await client.patch(f"/v1/todo/{finance_id}", json={"category": "Finance"})

    # Filter by category
    res = await client.get("/v1/todo/?category=Finance")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["public_id"] == finance_id
    assert body["items"][0]["category"] == "Finance"

    # Unknown category returns empty
    res = await client.get("/v1/todo/?category=DoesNotExist")
    assert res.status_code == 200
    assert res.json()["total"] == 0
