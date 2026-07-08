from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_dashboard_summary_empty_workspace(client: AsyncClient):
    # Register and login user
    user = {"email": "dash1@example.com", "username": "dash1", "password": "TestPass123!"}
    register_res = await client.post("/v1/auth/register", json=user)
    assert register_res.status_code == 200
    login_res = await client.post(
        "/v1/auth/login", data={"username": user["username"], "password": user["password"]}
    )
    assert login_res.status_code == 200

    # Fetch dashboard summary
    summary_res = await client.get("/v1/dashboard/summary")
    assert summary_res.status_code == 200

    summary = summary_res.json()
    assert "todos" in summary
    assert summary["todos"]["open_count"] == 0
    assert summary["todos"]["overdue_count"] == 0

    assert "spending" in summary
    assert summary["spending"]["month_spent"] == "0"

    assert "investing" in summary
    assert summary["investing"]["portfolio_value"] == "0.00"
    assert summary["investing"]["invested_value"] == "0.00"
    assert summary["investing"]["cash_total"] == "0.00"
    assert summary["investing"]["total_gain_loss"] == "0.00"
    assert summary["investing"]["daily_change"] is None

    assert "system" in summary
    assert "generated_at" in summary["system"]


@pytest.mark.asyncio
async def test_dashboard_summary_with_data(client: AsyncClient):
    # Register and login user
    user = {"email": "dash2@example.com", "username": "dash2", "password": "TestPass123!"}
    register_res = await client.post("/v1/auth/register", json=user)
    assert register_res.status_code == 200
    login_res = await client.post(
        "/v1/auth/login", data={"username": user["username"], "password": user["password"]}
    )
    assert login_res.status_code == 200

    # Create a todo
    todo_data = {"title": "Dashboard Todo", "description": "Test", "priority": "high"}
    todo_res = await client.post("/v1/todo/", json=todo_data)
    assert todo_res.status_code == 201

    # Fetch categories to get a valid category_id
    cat_res = await client.get("/v1/spending/categories")
    assert cat_res.status_code == 200
    categories = cat_res.json()["items"]
    category_id = categories[0]["public_id"]

    # Create an account
    account_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Wallet", "account_type": "wallet", "default_currency_code": "USD"},
    )
    assert account_res.status_code == 201, account_res.text
    account_id = account_res.json()["public_id"]

    # Create a spending transaction
    spending_data = {
        "amount": "15.50",
        "description": "Lunch",
        "category_id": category_id,
        "account_id": account_id,
        "type": "expense",
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    spend_res = await client.post("/v1/spending/transactions", json=spending_data)
    assert spend_res.status_code == 201

    # Create a budget for this month
    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    budget_res = await client.post(
        "/v1/spending/budgets",
        json={
            "category_id": category_id,
            "amount": "100.00",
            "start_month": month_start.date().isoformat(),
        },
    )
    assert budget_res.status_code == 201

    # Fetch dashboard summary
    summary_res = await client.get("/v1/dashboard/summary")
    assert summary_res.status_code == 200

    summary = summary_res.json()
    assert summary["todos"]["open_count"] == 1
    assert summary["spending"]["month_spent"] == "15.50"


@pytest.mark.asyncio
async def test_dashboard_workspace_isolation(client: AsyncClient):
    # User 1 creates data
    user1 = {"email": "dash_iso1@example.com", "username": "dash_iso1", "password": "TestPass123!"}
    await client.post("/v1/auth/register", json=user1)
    await client.post(
        "/v1/auth/login", data={"username": user1["username"], "password": user1["password"]}
    )

    await client.post("/v1/todo/", json={"title": "U1 Todo", "priority": "low"})

    # User 2 logs in
    await client.post("/v1/auth/logout")
    user2 = {"email": "dash_iso2@example.com", "username": "dash_iso2", "password": "TestPass123!"}
    await client.post("/v1/auth/register", json=user2)
    await client.post(
        "/v1/auth/login", data={"username": user2["username"], "password": user2["password"]}
    )

    # Fetch dashboard summary for User 2
    summary_res = await client.get("/v1/dashboard/summary")
    assert summary_res.status_code == 200
    summary = summary_res.json()

    assert summary["todos"]["open_count"] == 0


@pytest.mark.asyncio
async def test_briefing_all_clear_on_empty_workspace(client: AsyncClient):
    user = {"email": "briefing1@example.com", "username": "briefing1", "password": "TestPass123!"}
    await client.post("/v1/auth/register", json=user)
    await client.post(
        "/v1/auth/login", data={"username": user["username"], "password": user["password"]}
    )

    res = await client.get("/v1/dashboard/briefing")
    assert res.status_code == 200
    body = res.json()
    assert body["all_clear"] is True
    assert body["lines"] == []
    assert "generated_at" in body
    assert "reporting_currency" in body


@pytest.mark.asyncio
async def test_briefing_surfaces_overdue_todo(client: AsyncClient):
    user = {"email": "briefing2@example.com", "username": "briefing2", "password": "TestPass123!"}
    await client.post("/v1/auth/register", json=user)
    await client.post(
        "/v1/auth/login", data={"username": user["username"], "password": user["password"]}
    )

    past_due = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    todo_res = await client.post(
        "/v1/todo/",
        json={"title": "Overdue thing", "priority": "high", "due_date": past_due},
    )
    assert todo_res.status_code == 201

    res = await client.get("/v1/dashboard/briefing")
    assert res.status_code == 200
    body = res.json()
    assert body["all_clear"] is False
    overdue_lines = [line for line in body["lines"] if line["source"]["route"] == "/todo"]
    assert len(overdue_lines) == 1
    assert overdue_lines[0]["severity"] == "critical"
    assert "Overdue thing" in overdue_lines[0]["text"]


@pytest.mark.asyncio
async def test_briefing_workspace_isolation(client: AsyncClient):
    user1 = {
        "email": "briefing_iso1@example.com",
        "username": "briefing_iso1",
        "password": "TestPass123!",
    }
    await client.post("/v1/auth/register", json=user1)
    await client.post(
        "/v1/auth/login", data={"username": user1["username"], "password": user1["password"]}
    )
    past_due = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    await client.post(
        "/v1/todo/", json={"title": "U1 overdue", "priority": "high", "due_date": past_due}
    )

    await client.post("/v1/auth/logout")
    user2 = {
        "email": "briefing_iso2@example.com",
        "username": "briefing_iso2",
        "password": "TestPass123!",
    }
    await client.post("/v1/auth/register", json=user2)
    await client.post(
        "/v1/auth/login", data={"username": user2["username"], "password": user2["password"]}
    )

    res = await client.get("/v1/dashboard/briefing")
    assert res.status_code == 200
    assert res.json()["all_clear"] is True
