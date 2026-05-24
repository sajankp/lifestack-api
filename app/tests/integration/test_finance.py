from datetime import UTC, datetime

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


@pytest.mark.asyncio
async def test_finance_settings_fx_and_transfers_flow(client: AsyncClient):
    await _register_and_login(
        client,
        email="finance-transfer@example.com",
        username="finance-transfer",
        password="password123",
    )

    from_account = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "Budget Bank",
            "account_type": "bank",
            "default_currency_code": "USD",
        },
    )
    assert from_account.status_code == 201
    to_account = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "Global Brokerage",
            "account_type": "brokerage",
            "default_currency_code": "GBP",
        },
    )
    assert to_account.status_code == 201

    setting_res = await client.patch(
        "/v1/finance/settings",
        json={"reporting_currency_code": "USD"},
    )
    assert setting_res.status_code == 200
    assert setting_res.json()["reporting_currency_code"] == "USD"

    fx_upsert = await client.post(
        "/v1/finance/fx-rates",
        json={
            "base_currency_code": "GBP",
            "quote_currency_code": "USD",
            "rate": "1.2500000000",
            "as_of": datetime.now(UTC).isoformat(),
            "fetched_at": datetime.now(UTC).isoformat(),
            "source": "test-seed",
        },
    )
    assert fx_upsert.status_code == 405

    fx_get = await client.get("/v1/finance/fx-rates", params={"base": "GBP", "quote": "USD"})
    assert fx_get.status_code == 404

    transfer_res = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": "spending",
            "to_module": "investing",
            "from_account_id": from_account.json()["public_id"],
            "to_account_id": to_account.json()["public_id"],
            "from_currency_code": "USD",
            "to_currency_code": "GBP",
            "gross_amount": "1000.00",
            "fx_rate_used": "0.8000000000",
            "fx_fee_amount": "5.00",
            "platform_fee_amount": "2.00",
            "tax_amount": "1.00",
            "net_amount_received": "792.00",
            "occurred_at": datetime.now(UTC).isoformat(),
            "notes": "Initial transfer",
        },
    )
    assert transfer_res.status_code == 201
    transfer_id = transfer_res.json()["public_id"]

    list_transfers = await client.get("/v1/finance/transfers")
    assert list_transfers.status_code == 200
    assert list_transfers.json()["total"] == 1

    get_transfer = await client.get(f"/v1/finance/transfers/{transfer_id}")
    assert get_transfer.status_code == 200
    assert get_transfer.json()["net_amount_received"] == "792.00"
