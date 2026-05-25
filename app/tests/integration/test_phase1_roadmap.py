import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient


async def _register_and_login(client: AsyncClient, suffix: str) -> dict:
    username = f"phase1_{suffix}"
    email = f"{username}@example.com"
    password = "Password123!"
    reg = await client.post(
        "/v1/auth/register", json={"email": email, "username": username, "password": password}
    )
    assert reg.status_code == 200
    login = await client.post("/v1/auth/login", data={"username": username, "password": password})
    assert login.status_code == 200
    return {"cookies": dict(login.cookies)}


@pytest.mark.asyncio
async def test_notifications_capture_summaries_and_analytics(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    other = next(c for c in cats if c["name"] == "Other")

    rec = await client.post(
        "/v1/spending/recurring",
        json={
            "category_id": other["public_id"],
            "amount": "20.00",
            "type": "expense",
            "description": "Subscription",
            "frequency": "monthly",
            "interval": 1,
            "anchor_date": datetime.now(UTC).date().isoformat(),
        },
        cookies=creds["cookies"],
    )
    assert rec.status_code == 201

    cap = await client.post(
        "/v1/capture",
        json={"text": "buy milk tomorrow", "hints": {"due_date": "tomorrow"}},
        cookies=creds["cookies"],
    )
    assert cap.status_code == 200
    assert cap.json()["module"] == "todo"

    notify = await client.get("/v1/notifications/unread-count", cookies=creds["cookies"])
    assert notify.status_code == 200

    weekly_list = await client.get("/v1/summaries/weekly", cookies=creds["cookies"])
    assert weekly_list.status_code == 200

    trend = await client.get(
        "/v1/spending/analytics/trends",
        params={
            "from": datetime.now(UTC).date().replace(day=1).isoformat(),
            "to": datetime.now(UTC).date().replace(day=1).isoformat(),
        },
        cookies=creds["cookies"],
    )
    assert trend.status_code == 200
    assert "months" in trend.json()


@pytest.mark.asyncio
async def test_investing_performance_summary_endpoint(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])

    create_account = await client.post(
        "/v1/finance/accounts",
        json={"name": "brokerage", "account_type": "brokerage", "default_currency_code": "USD"},
        cookies=creds["cookies"],
    )
    assert create_account.status_code in (201, 409)

    hold = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "AAPL",
            "account_name": "brokerage",
            "quantity": "2.00000000",
            "avg_cost": "100.00",
            "currency": "USD",
        },
        cookies=creds["cookies"],
    )
    assert hold.status_code == 201
    hid = hold.json()["public_id"]

    price = await client.post(
        "/v1/investing/prices",
        json={
            "price_date": datetime.now(UTC).date().isoformat(),
            "prices": [{"holding_public_id": hid, "unit_price": "120.00"}],
        },
        cookies=creds["cookies"],
    )
    assert price.status_code == 201

    perf = await client.get("/v1/investing/performance/summary", cookies=creds["cookies"])
    assert perf.status_code == 200
    body = perf.json()
    assert "total_value" in body
    assert "total_gain_loss" in body
