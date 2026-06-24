"""Integration tests for the derived spending account balance endpoint."""

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


async def _create_account(client, cookies, name, currency="USD"):
    res = await client.post(
        "/v1/finance/accounts",
        json={"name": name, "account_type": "wallet", "default_currency_code": currency},
        cookies=cookies,
    )
    assert res.status_code == 201, res.text
    return res.json()["public_id"]


async def _first_category(client, cookies):
    res = await client.get("/v1/spending/categories", cookies=cookies)
    assert res.status_code == 200
    return res.json()["items"][0]["public_id"]


@pytest.mark.asyncio
async def test_account_balance_no_transactions(client: AsyncClient):
    """Balance endpoint returns zero for an account with no spending transactions."""
    cookies = await _register_and_login(client, "bal_zero@example.com", "bal_zero")
    account_id = await _create_account(client, cookies, "Zero Balance Account")

    res = await client.get(f"/v1/finance/accounts/{account_id}/balance", cookies=cookies)
    assert res.status_code == 200
    data = res.json()
    assert data["account_public_id"] == account_id
    assert float(data["spending_balance"]) == 0.0
    assert data["transaction_count"] == 0
    assert data["first_transaction_at"] is None
    assert data["last_transaction_at"] is None


@pytest.mark.asyncio
async def test_account_balance_with_transactions(client: AsyncClient):
    """Balance = total income - total expenses for the account."""
    cookies = await _register_and_login(client, "bal_tx@example.com", "bal_tx")
    account_id = await _create_account(client, cookies, "Active Account")
    cat_id = await _first_category(client, cookies)

    for payload in [
        {"amount": "2000.00", "type": "income", "occurred_at": "2026-05-01T10:00:00Z"},
        {"amount": "800.00", "type": "expense", "occurred_at": "2026-05-10T10:00:00Z"},
        {"amount": "400.00", "type": "expense", "occurred_at": "2026-05-15T10:00:00Z"},
    ]:
        r = await client.post(
            "/v1/spending/transactions",
            json={**payload, "category_id": cat_id, "account_id": account_id},
            cookies=cookies,
        )
        assert r.status_code == 201, r.text

    res = await client.get(f"/v1/finance/accounts/{account_id}/balance", cookies=cookies)
    assert res.status_code == 200
    data = res.json()
    # 2000 - 800 - 400 = 800
    assert float(data["spending_balance"]) == 800.0
    assert data["transaction_count"] == 3
    assert data["first_transaction_at"] is not None
    assert data["last_transaction_at"] is not None


@pytest.mark.asyncio
async def test_account_balance_workspace_isolation(client: AsyncClient):
    """Balance endpoint returns 404 for an account in another workspace."""
    cookies_1 = await _register_and_login(client, "bal_iso1@example.com", "bal_iso1")
    account_id = await _create_account(client, cookies_1, "Isolated Account")

    cookies_2 = await _register_and_login(client, "bal_iso2@example.com", "bal_iso2")
    res = await client.get(f"/v1/finance/accounts/{account_id}/balance", cookies=cookies_2)
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_account_balance_net_zero(client: AsyncClient):
    """Account with equal income and expenses returns a balance of zero."""
    cookies = await _register_and_login(client, "bal_net@example.com", "bal_net")
    account_id = await _create_account(client, cookies, "Net Zero Account")
    cat_id = await _first_category(client, cookies)

    for payload in [
        {"amount": "500.00", "type": "income", "occurred_at": "2026-06-01T10:00:00Z"},
        {"amount": "500.00", "type": "expense", "occurred_at": "2026-06-02T10:00:00Z"},
    ]:
        r = await client.post(
            "/v1/spending/transactions",
            json={**payload, "category_id": cat_id, "account_id": account_id},
            cookies=cookies,
        )
        assert r.status_code == 201, r.text

    res = await client.get(f"/v1/finance/accounts/{account_id}/balance", cookies=cookies)
    assert res.status_code == 200
    assert float(res.json()["spending_balance"]) == 0.0
