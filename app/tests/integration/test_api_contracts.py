from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient


async def _register_and_login(
    client: AsyncClient, *, email: str, username: str, password: str
) -> None:
    register = await client.post(
        "/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert register.status_code == 200

    login = await client.post(
        "/v1/auth/login",
        data={"username": username, "password": password},
    )
    assert login.status_code == 200
    assert "access_token" in login.cookies
    assert "refresh_token" in login.cookies


def _assert_problem_contract(body: dict) -> None:
    assert "type" in body
    assert "code" in body
    assert "title" in body
    assert "status" in body
    assert "detail" in body
    assert "hint" in body
    assert "instance" in body


@pytest.mark.asyncio
async def test_auth_contract_and_error_envelope(client: AsyncClient):
    suffix = uuid4().hex[:8]
    email = f"contract-auth-{suffix}@example.com"
    username = f"contract_auth_{suffix}"
    password = "Password123!"

    await _register_and_login(client, email=email, username=username, password=password)

    me = await client.get("/v1/auth/me")
    assert me.status_code == 200
    me_body = me.json()
    assert set(me_body.keys()) == {"public_id", "email", "username", "is_active"}
    assert me_body["email"] == email
    assert me_body["username"] == username

    logout = await client.post("/v1/auth/logout")
    assert logout.status_code == 200

    me_after = await client.get("/v1/auth/me")
    assert me_after.status_code == 401
    me_after_body = me_after.json()
    _assert_problem_contract(me_after_body)
    assert me_after_body["code"] == "unauthorized"

    invalid_register = await client.post(
        "/v1/auth/register",
        json={"email": "not-an-email", "username": "x", "password": "y"},
    )
    assert invalid_register.status_code == 422
    invalid_body = invalid_register.json()
    _assert_problem_contract(invalid_body)
    assert invalid_body["code"] == "validation_error"
    assert isinstance(invalid_body.get("errors"), list)
    assert invalid_body["errors"], "Expected field-level validation errors for invalid payload."


@pytest.mark.asyncio
async def test_spending_category_and_recurring_contracts(client: AsyncClient):
    suffix = uuid4().hex[:8]
    await _register_and_login(
        client,
        email=f"contract-spending-{suffix}@example.com",
        username=f"contract_spending_{suffix}",
        password="Password123!",
    )

    categories_res = await client.get("/v1/spending/categories")
    assert categories_res.status_code == 200
    categories_body = categories_res.json()
    assert {"items", "total", "limit", "offset"}.issubset(categories_body.keys())
    assert categories_body["items"], "Expected seeded categories for selector usage."
    first_category = categories_body["items"][0]
    assert {"public_id", "name", "is_system"}.issubset(first_category.keys())

    recurring_payload = {
        "category_id": first_category["public_id"],
        "amount": "14.99",
        "type": "expense",
        "description": f"Contract recurring {suffix}",
        "frequency": "monthly",
        "interval": 1,
        "anchor_date": datetime.now(UTC).date().isoformat(),
    }
    create_recurring = await client.post("/v1/spending/recurring", json=recurring_payload)
    assert create_recurring.status_code == 201
    recurring_body = create_recurring.json()
    assert recurring_body["category_id"] == first_category["public_id"]
    assert recurring_body["description"] == recurring_payload["description"]
    recurring_id = recurring_body["public_id"]

    patch_recurring = await client.patch(
        f"/v1/spending/recurring/{recurring_id}",
        json={"amount": "19.99"},
    )
    assert patch_recurring.status_code == 200
    assert patch_recurring.json()["amount"] == "19.99"

    deactivate_recurring = await client.delete(f"/v1/spending/recurring/{recurring_id}")
    assert deactivate_recurring.status_code == 204


@pytest.mark.asyncio
async def test_investing_account_currency_selector_contracts(client: AsyncClient):
    suffix = uuid4().hex[:8]
    await _register_and_login(
        client,
        email=f"contract-investing-{suffix}@example.com",
        username=f"contract_investing_{suffix}",
        password="Password123!",
    )

    currencies_res = await client.get("/v1/finance/currencies")
    assert currencies_res.status_code == 200
    currencies = currencies_res.json()
    assert isinstance(currencies, list)
    assert currencies, "Expected at least one enabled currency for selector usage."
    assert {"code", "name", "symbol", "minor_unit", "is_active"}.issubset(currencies[0].keys())

    create_account = await client.post(
        "/v1/finance/accounts",
        json={
            "name": f"Contract Brokerage {suffix}",
            "account_type": "brokerage",
            "default_currency_code": "usd",
        },
    )
    assert create_account.status_code == 201
    account_body = create_account.json()
    assert {"public_id", "name", "account_type", "default_currency_code", "is_active"}.issubset(
        account_body.keys()
    )
    assert account_body["default_currency_code"] == "USD"

    create_holding = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "AAPL",
            "account_name": account_body["name"],
            "quantity": "2.50000000",
            "avg_cost": "180.00",
            "currency": "usd",
        },
    )
    assert create_holding.status_code == 201
    holding_body = create_holding.json()
    assert holding_body["account_name"] == account_body["name"]
    assert holding_body["currency"] == "USD"

    create_cash = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_name": account_body["name"],
            "balance": "1500.00",
            "currency": "USD",
            "as_of": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        },
    )
    assert create_cash.status_code == 201
    cash_body = create_cash.json()
    assert cash_body["account_name"] == account_body["name"]
    assert cash_body["currency"] == "USD"


@pytest.mark.asyncio
async def test_finance_settings_contract_with_user_overrides(client: AsyncClient):
    suffix = uuid4().hex[:8]
    await _register_and_login(
        client,
        email=f"contract-finance-settings-{suffix}@example.com",
        username=f"contract_finance_settings_{suffix}",
        password="Password123!",
    )

    update_workspace = await client.patch(
        "/v1/finance/settings",
        json={
            "reporting_currency_code": "USD",
            "currency_display_preference": "code",
        },
    )
    assert update_workspace.status_code == 200
    workspace_body = update_workspace.json()
    assert {
        "reporting_currency_code",
        "currency_display_preference",
        "updated_at",
    }.issubset(workspace_body.keys())
    assert workspace_body["reporting_currency_code"] == "USD"
    assert workspace_body["currency_display_preference"] == "code"

    get_user_settings = await client.get("/v1/finance/settings/user")
    assert get_user_settings.status_code == 200
    user_settings_body = get_user_settings.json()
    assert {
        "reporting_currency_override_code",
        "currency_display_preference_override",
        "workspace_reporting_currency_code",
        "workspace_currency_display_preference",
        "effective_reporting_currency_code",
        "effective_currency_display_preference",
        "updated_at",
    }.issubset(user_settings_body.keys())
    assert user_settings_body["effective_reporting_currency_code"] == "USD"
    assert user_settings_body["effective_currency_display_preference"] == "code"

    update_user_settings = await client.patch(
        "/v1/finance/settings/user",
        json={
            "reporting_currency_override_code": "INR",
            "currency_display_preference_override": "symbol",
        },
    )
    assert update_user_settings.status_code == 200
    updated_user_settings_body = update_user_settings.json()
    assert updated_user_settings_body["reporting_currency_override_code"] == "INR"
    assert updated_user_settings_body["currency_display_preference_override"] == "symbol"
    assert updated_user_settings_body["effective_reporting_currency_code"] == "INR"
    assert updated_user_settings_body["effective_currency_display_preference"] == "symbol"
