import pytest
from httpx import AsyncClient


async def _register_and_login(client: AsyncClient, email: str, username: str) -> dict:
    register_res = await client.post(
        "/v1/auth/register",
        json={"email": email, "username": username, "password": "TestPass123!"},
    )
    assert register_res.status_code == 200
    login_res = await client.post(
        "/v1/auth/login",
        data={"username": username, "password": "TestPass123!"},
    )
    assert login_res.status_code == 200
    return dict(login_res.cookies)


async def _create_account(client: AsyncClient, cookies: dict) -> str:
    response = await client.post(
        "/v1/finance/accounts",
        json={"name": "Wallet", "account_type": "wallet", "default_currency_code": "USD"},
        cookies=cookies,
    )
    assert response.status_code == 201, response.text
    return response.json()["public_id"]


@pytest.mark.asyncio
async def test_category_breakdown_endpoint(client: AsyncClient):
    cookies = await _register_and_login(client, "breakdown@example.com", "breakdown")
    account_id = await _create_account(client, cookies)

    # 1. Get default category public IDs
    list_res = await client.get("/v1/spending/categories", cookies=cookies)
    assert list_res.status_code == 200
    cats = list_res.json()["items"]
    assert len(cats) >= 2
    cat_1_id = cats[0]["public_id"]
    cat_2_id = cats[1]["public_id"]

    # 2. Create some transactions
    await client.post(
        "/v1/spending/transactions",
        json={
            "amount": 100.00,
            "category_id": cat_1_id,
            "account_id": account_id,
            "type": "expense",
            "occurred_at": "2026-06-05T12:00:00Z",
            "description": "Food expense",
        },
        cookies=cookies,
    )
    await client.post(
        "/v1/spending/transactions",
        json={
            "amount": 200.00,
            "category_id": cat_2_id,
            "account_id": account_id,
            "type": "expense",
            "occurred_at": "2026-06-06T12:00:00Z",
            "description": "Travel expense",
        },
        cookies=cookies,
    )
    # Income transaction (should not be in breakdown for expense type)
    await client.post(
        "/v1/spending/transactions",
        json={
            "amount": 500.00,
            "category_id": cat_1_id,
            "account_id": account_id,
            "type": "income",
            "occurred_at": "2026-06-07T12:00:00Z",
            "description": "Salary",
        },
        cookies=cookies,
    )

    # 3. Fetch breakdown for expense
    res = await client.get(
        "/v1/spending/analytics/breakdown",
        params={"from": "2026-06-01", "to": "2026-06-10", "type": "expense"},
        cookies=cookies,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == "300.00"
    assert len(data["categories"]) == 2

    # Check order (highest amount first)
    assert data["categories"][0]["category_id"] == cat_2_id
    assert data["categories"][0]["amount"] == "200.00"
    assert data["categories"][0]["pct_of_total"] == pytest.approx(66.666, abs=1e-2)

    assert data["categories"][1]["category_id"] == cat_1_id
    assert data["categories"][1]["amount"] == "100.00"
    assert data["categories"][1]["pct_of_total"] == pytest.approx(33.333, abs=1e-2)


@pytest.mark.asyncio
async def test_budget_performance_endpoint(client: AsyncClient):
    cookies = await _register_and_login(client, "budgetperf@example.com", "budgetperf")
    account_id = await _create_account(client, cookies)

    # 1. Get a category ID
    list_res = await client.get("/v1/spending/categories", cookies=cookies)
    category_id = list_res.json()["items"][0]["public_id"]

    # 2. Create budget for the month
    await client.post(
        "/v1/spending/budgets",
        json={"amount": 100.00, "category_id": category_id, "month_start": "2026-06-01"},
        cookies=cookies,
    )

    # 3. Create expense transaction
    await client.post(
        "/v1/spending/transactions",
        json={
            "amount": 95.00,
            "category_id": category_id,
            "account_id": account_id,
            "type": "expense",
            "occurred_at": "2026-06-05T12:00:00Z",
        },
        cookies=cookies,
    )

    # 4. Fetch budget performance
    res = await client.get(
        "/v1/spending/analytics/budget-performance",
        params={"from": "2026-06-01", "to": "2026-06-01"},
        cookies=cookies,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["totals"]["total_budgeted"] == "100.00"
    assert data["totals"]["total_actual"] == "95.00"
    assert data["totals"]["overall_utilization_pct"] == 95.0

    # Verify category item details
    category_item = data["categories"][0]
    assert category_item["category_id"] == category_id
    assert category_item["actual_amount"] == "95.00"
    assert category_item["budget_amount"] == "100.00"
    assert category_item["utilization_pct"] == 95.0
    assert category_item["remaining"] == "5.00"
    assert category_item["status"] == "warning"  # 95% is between 90-100%


@pytest.mark.asyncio
async def test_savings_rate_endpoint(client: AsyncClient):
    cookies = await _register_and_login(client, "savings@example.com", "savings")
    account_id = await _create_account(client, cookies)

    # 1. Get a category ID
    list_res = await client.get("/v1/spending/categories", cookies=cookies)
    category_id = list_res.json()["items"][0]["public_id"]

    # 2. Add income and expense in June 2026
    await client.post(
        "/v1/spending/transactions",
        json={
            "amount": 1000.00,
            "category_id": category_id,
            "account_id": account_id,
            "type": "income",
            "occurred_at": "2026-06-05T12:00:00Z",
        },
        cookies=cookies,
    )
    await client.post(
        "/v1/spending/transactions",
        json={
            "amount": 400.00,
            "category_id": category_id,
            "account_id": account_id,
            "type": "expense",
            "occurred_at": "2026-06-10T12:00:00Z",
        },
        cookies=cookies,
    )

    # 3. Add income and expense in July 2026
    await client.post(
        "/v1/spending/transactions",
        json={
            "amount": 2000.00,
            "category_id": category_id,
            "account_id": account_id,
            "type": "income",
            "occurred_at": "2026-07-05T12:00:00Z",
        },
        cookies=cookies,
    )
    await client.post(
        "/v1/spending/transactions",
        json={
            "amount": 1500.00,
            "category_id": category_id,
            "account_id": account_id,
            "type": "expense",
            "occurred_at": "2026-07-15T12:00:00Z",
        },
        cookies=cookies,
    )

    # 4. Fetch savings rate
    res = await client.get(
        "/v1/spending/analytics/savings-rate",
        params={"from": "2026-06-01", "to": "2026-07-01"},
        cookies=cookies,
    )
    assert res.status_code == 200
    data = res.json()
    assert len(data["months"]) == 2

    # June
    assert data["months"][0]["month"] == "2026-06"
    assert data["months"][0]["income"] == "1000.00"
    assert data["months"][0]["expense"] == "400.00"
    assert data["months"][0]["savings"] == "600.00"
    assert data["months"][0]["savings_rate_pct"] == 60.0

    # July
    assert data["months"][1]["month"] == "2026-07"
    assert data["months"][1]["income"] == "2000.00"
    assert data["months"][1]["expense"] == "1500.00"
    assert data["months"][1]["savings"] == "500.00"
    assert data["months"][1]["savings_rate_pct"] == 25.0

    # Totals
    assert data["period_totals"]["total_income"] == "3000.00"
    assert data["period_totals"]["total_expense"] == "1900.00"
    assert data["period_totals"]["total_savings"] == "1100.00"
    assert data["period_totals"]["average_savings_rate_pct"] == pytest.approx(36.666, abs=1e-2)


@pytest.mark.asyncio
async def test_analytics_date_range_validation(client: AsyncClient):
    cookies = await _register_and_login(client, "range@example.com", "range")

    # Limit is 24 months, request > 24 months
    res = await client.get(
        "/v1/spending/analytics/savings-rate",
        params={"from": "2024-01-01", "to": "2026-06-01"},
        cookies=cookies,
    )
    assert res.status_code == 422
    assert "Date range cannot exceed 24 months" in res.json()["detail"]
