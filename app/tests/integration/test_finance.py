import pytest
from httpx import AsyncClient


async def _register_and_login(
    client: AsyncClient, *, email: str, username: str, password: str
) -> None:
    register_res = await client.post(
        "/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert register_res.status_code == 200
    login_res = await client.post(
        "/v1/auth/login",
        data={"username": username, "password": password},
    )
    assert login_res.status_code == 200


@pytest.mark.asyncio
async def test_finance_currencies_bootstrap_and_account_crud(client: AsyncClient):
    await _register_and_login(
        client,
        email="finance-e2e@example.com",
        username="finance-e2e",
        password="password123",
    )

    currencies_res = await client.get("/v1/finance/currencies")
    assert currencies_res.status_code == 200
    currencies = currencies_res.json()
    assert [currency["code"] for currency in currencies] == ["GBP", "INR", "USD"]

    create_res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "Primary Brokerage",
            "account_type": "brokerage",
            "default_currency_code": "usd",
        },
    )
    assert create_res.status_code == 201
    account = create_res.json()
    assert account["name"] == "Primary Brokerage"
    assert account["account_type"] == "brokerage"
    assert account["default_currency_code"] == "USD"

    list_res = await client.get("/v1/finance/accounts")
    assert list_res.status_code == 200
    listed = list_res.json()
    assert listed["total"] == 1
    assert listed["items"][0]["public_id"] == account["public_id"]

    update_res = await client.patch(
        f"/v1/finance/accounts/{account['public_id']}",
        json={
            "name": "Retirement Brokerage",
            "account_type": "wallet",
            "default_currency_code": "inr",
        },
    )
    assert update_res.status_code == 200
    updated = update_res.json()
    assert updated["name"] == "Retirement Brokerage"
    assert updated["account_type"] == "wallet"
    assert updated["default_currency_code"] == "INR"


@pytest.mark.asyncio
async def test_finance_account_validation_and_workspace_isolation(client: AsyncClient):
    await _register_and_login(
        client,
        email="finance-iso-a@example.com",
        username="finance-iso-a",
        password="password123",
    )

    create_res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "A-Brokerage",
            "account_type": "brokerage",
            "default_currency_code": "USD",
        },
    )
    assert create_res.status_code == 201
    account_id = create_res.json()["public_id"]

    duplicate_res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "A-Brokerage",
            "account_type": "brokerage",
            "default_currency_code": "USD",
        },
    )
    assert duplicate_res.status_code == 409
    assert duplicate_res.headers["content-type"].startswith("application/problem+json")

    invalid_currency_res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "Bad Currency Account",
            "account_type": "bank",
            "default_currency_code": "EUR",
        },
    )
    assert invalid_currency_res.status_code == 422
    assert invalid_currency_res.headers["content-type"].startswith("application/problem+json")

    await client.post("/v1/auth/logout")
    await _register_and_login(
        client,
        email="finance-iso-b@example.com",
        username="finance-iso-b",
        password="password123",
    )

    list_res = await client.get("/v1/finance/accounts")
    assert list_res.status_code == 200
    assert list_res.json()["items"] == []

    patch_res = await client.patch(
        f"/v1/finance/accounts/{account_id}",
        json={"name": "Should Not Work"},
    )
    assert patch_res.status_code == 404
