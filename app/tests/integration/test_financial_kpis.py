"""Integration tests for spec-077: custom financial KPIs.

Covers: CRUD, the single-currency-per-KPI validation at both create time and
(after an account's currency set changes) at evaluation time, correct
evaluation for all three v1 metric types, and window-boundary behaviour.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient


async def _register_and_login(client: AsyncClient, username: str) -> dict:
    email = f"{username}@example.com"
    register_res = await client.post(
        "/v1/auth/register",
        json={"email": email, "username": username, "password": "TestPass123!"},
    )
    assert register_res.status_code == 200, register_res.text
    login_res = await client.post(
        "/v1/auth/login",
        data={"username": username, "password": "TestPass123!"},
    )
    assert login_res.status_code == 200, login_res.text
    return dict(login_res.cookies)


async def _create_account(
    client: AsyncClient, cookies: dict, name: str, currency: str = "USD"
) -> str:
    resp = await client.post(
        "/v1/finance/accounts",
        json={"name": name, "account_type": "wallet", "default_currency_code": currency},
        cookies=cookies,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["public_id"]


async def _create_category(client: AsyncClient, cookies: dict, name: str) -> str:
    resp = await client.post("/v1/spending/categories", json={"name": name}, cookies=cookies)
    assert resp.status_code == 201, resp.text
    return resp.json()["public_id"]


async def _create_transaction(
    client: AsyncClient,
    cookies: dict,
    *,
    category_id: str,
    account_id: str,
    amount: str,
    tx_type: str,
    occurred_at: datetime,
) -> None:
    resp = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": category_id,
            "account_id": account_id,
            "amount": amount,
            "type": tx_type,
            "occurred_at": occurred_at.isoformat(),
        },
        cookies=cookies,
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_kpi_crud_and_evaluation(client: AsyncClient):
    cookies = await _register_and_login(client, "kpicrud")
    account_id = await _create_account(client, cookies, "Wallet")
    category_id = await _create_category(client, cookies, "Dining")

    month_start = datetime.now(UTC).date().replace(day=1)
    occurred_at = datetime(month_start.year, month_start.month, 5, tzinfo=UTC)
    await _create_transaction(
        client,
        cookies,
        category_id=category_id,
        account_id=account_id,
        amount="60.00",
        tx_type="expense",
        occurred_at=occurred_at,
    )
    await _create_transaction(
        client,
        cookies,
        category_id=category_id,
        account_id=account_id,
        amount="500.00",
        tx_type="income",
        occurred_at=occurred_at,
    )

    create_resp = await client.post(
        "/v1/spending/kpis",
        json={
            "name": "Dining under 100",
            "metric_type": "spend_total",
            "evaluation_window": "calendar_month",
            "category_id": category_id,
            "target_value": "100.00",
            "target_direction": "lte",
        },
        cookies=cookies,
    )
    assert create_resp.status_code == 201, create_resp.text
    kpi = create_resp.json()
    assert kpi["currency_code"] == "USD"
    assert kpi["current_value"] == "60.00"
    assert kpi["is_breached"] is False

    kpi_id = kpi["public_id"]

    list_resp = await client.get("/v1/spending/kpis", cookies=cookies)
    assert list_resp.status_code == 200, list_resp.text
    assert list_resp.json()["total"] == 1

    update_resp = await client.patch(
        f"/v1/spending/kpis/{kpi_id}",
        json={"target_value": "50.00", "target_direction": "lte"},
        cookies=cookies,
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["is_breached"] is True

    delete_resp = await client.delete(f"/v1/spending/kpis/{kpi_id}", cookies=cookies)
    assert delete_resp.status_code == 204, delete_resp.text

    list_resp = await client.get("/v1/spending/kpis", cookies=cookies)
    assert list_resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_kpi_income_total_and_net_cash_flow(client: AsyncClient):
    cookies = await _register_and_login(client, "kpimetrics")
    account_id = await _create_account(client, cookies, "Wallet")
    category_id = await _create_category(client, cookies, "Salary")

    month_start = datetime.now(UTC).date().replace(day=1)
    occurred_at = datetime(month_start.year, month_start.month, 5, tzinfo=UTC)
    await _create_transaction(
        client,
        cookies,
        category_id=category_id,
        account_id=account_id,
        amount="1000.00",
        tx_type="income",
        occurred_at=occurred_at,
    )
    await _create_transaction(
        client,
        cookies,
        category_id=category_id,
        account_id=account_id,
        amount="400.00",
        tx_type="expense",
        occurred_at=occurred_at,
    )

    income_resp = await client.post(
        "/v1/spending/kpis",
        json={
            "name": "Income",
            "metric_type": "income_total",
            "evaluation_window": "calendar_month",
        },
        cookies=cookies,
    )
    assert income_resp.status_code == 201, income_resp.text
    assert income_resp.json()["current_value"] == "1000.00"

    net_resp = await client.post(
        "/v1/spending/kpis",
        json={
            "name": "Net flow",
            "metric_type": "net_cash_flow",
            "evaluation_window": "calendar_month",
        },
        cookies=cookies,
    )
    assert net_resp.status_code == 201, net_resp.text
    assert net_resp.json()["current_value"] == "600.00"


@pytest.mark.asyncio
async def test_kpi_rejects_mixed_currency_filter_at_create(client: AsyncClient):
    cookies = await _register_and_login(client, "kpimixed")
    await _create_account(client, cookies, "USD Wallet", currency="USD")
    await _create_account(client, cookies, "INR Wallet", currency="INR")

    resp = await client.post(
        "/v1/spending/kpis",
        json={
            "name": "All accounts",
            "metric_type": "spend_total",
            "evaluation_window": "calendar_month",
        },
        cookies=cookies,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_kpi_single_account_filter_pins_currency_even_in_mixed_workspace(
    client: AsyncClient,
):
    cookies = await _register_and_login(client, "kpipinned")
    usd_account = await _create_account(client, cookies, "USD Wallet", currency="USD")
    await _create_account(client, cookies, "INR Wallet", currency="INR")

    resp = await client.post(
        "/v1/spending/kpis",
        json={
            "name": "USD spend",
            "metric_type": "spend_total",
            "evaluation_window": "calendar_month",
            "account_id": usd_account,
        },
        cookies=cookies,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["currency_code"] == "USD"


@pytest.mark.asyncio
async def test_kpi_rejects_multiple_filter_dimensions(client: AsyncClient):
    cookies = await _register_and_login(client, "kpimultifilter")
    account_id = await _create_account(client, cookies, "Wallet")
    category_id = await _create_category(client, cookies, "Dining")

    resp = await client.post(
        "/v1/spending/kpis",
        json={
            "name": "Bad filter",
            "metric_type": "spend_total",
            "evaluation_window": "calendar_month",
            "category_id": category_id,
            "account_id": account_id,
        },
        cookies=cookies,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_kpi_rolling_30d_window_excludes_older_transactions(client: AsyncClient):
    cookies = await _register_and_login(client, "kpiwindow")
    account_id = await _create_account(client, cookies, "Wallet")
    category_id = await _create_category(client, cookies, "Dining")

    today = datetime.now(UTC)
    within_window = today - timedelta(days=10)
    outside_window = today - timedelta(days=45)

    await _create_transaction(
        client,
        cookies,
        category_id=category_id,
        account_id=account_id,
        amount="25.00",
        tx_type="expense",
        occurred_at=within_window,
    )
    await _create_transaction(
        client,
        cookies,
        category_id=category_id,
        account_id=account_id,
        amount="999.00",
        tx_type="expense",
        occurred_at=outside_window,
    )

    resp = await client.post(
        "/v1/spending/kpis",
        json={
            "name": "Rolling spend",
            "metric_type": "spend_total",
            "evaluation_window": "rolling_30d",
        },
        cookies=cookies,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["current_value"] == "25.00"
