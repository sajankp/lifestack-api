"""Integration tests for the per-account Spending Transaction Ledger endpoint."""

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
async def test_ledger_empty_account(client: AsyncClient):
    """Ledger for an account with no transactions returns empty items and zero balances."""
    cookies = await _register_and_login(client, "ledger_empty@example.com", "ledger_empty")
    account_id = await _create_account(client, cookies, "Empty Wallet")

    res = await client.get(f"/v1/spending/accounts/{account_id}/ledger", cookies=cookies)
    assert res.status_code == 200
    data = res.json()
    assert data["total_entries"] == 0
    assert data["items"] == []
    assert data["account_public_id"] == account_id
    assert float(data["opening_balance"]) == 0.0
    assert float(data["closing_balance"]) == 0.0


@pytest.mark.asyncio
async def test_ledger_running_balance(client: AsyncClient):
    """Running balance accumulates correctly across income and expense transactions."""
    cookies = await _register_and_login(client, "ledger_running@example.com", "ledger_running")
    account_id = await _create_account(client, cookies, "Test Wallet")
    cat_id = await _first_category(client, cookies)

    for payload in [
        {"amount": "1000.00", "type": "income", "occurred_at": "2026-05-01T10:00:00Z"},
        {"amount": "300.00", "type": "expense", "occurred_at": "2026-05-02T10:00:00Z"},
        {"amount": "500.00", "type": "income", "occurred_at": "2026-05-03T10:00:00Z"},
    ]:
        r = await client.post(
            "/v1/spending/transactions",
            json={**payload, "category_id": cat_id, "account_id": account_id},
            cookies=cookies,
        )
        assert r.status_code == 201, r.text

    res = await client.get(
        f"/v1/spending/accounts/{account_id}/ledger",
        cookies=cookies,
        params={"limit": 50},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_entries"] == 3
    assert len(data["items"]) == 3
    # Desc order: most recent first (income +500 → bal 1200), then expense -300 → bal 700, then income +1000 → bal 1000
    balances = [float(item["running_balance"]) for item in data["items"]]
    assert balances[0] == 1200.0
    assert balances[1] == 700.0
    assert balances[2] == 1000.0
    assert float(data["closing_balance"]) == 1200.0


@pytest.mark.asyncio
async def test_ledger_workspace_isolation(client: AsyncClient):
    """Ledger endpoint returns 404 for an account that belongs to another workspace."""
    cookies_1 = await _register_and_login(client, "ledger_iso1@example.com", "ledger_iso1")
    account_id = await _create_account(client, cookies_1, "Private Wallet")

    cookies_2 = await _register_and_login(client, "ledger_iso2@example.com", "ledger_iso2")
    res = await client.get(f"/v1/spending/accounts/{account_id}/ledger", cookies=cookies_2)
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_ledger_pagination(client: AsyncClient):
    """Pagination parameters correctly limit returned items."""
    cookies = await _register_and_login(client, "ledger_page@example.com", "ledger_page")
    account_id = await _create_account(client, cookies, "Paginated Wallet")
    cat_id = await _first_category(client, cookies)

    for i in range(5):
        r = await client.post(
            "/v1/spending/transactions",
            json={
                "amount": f"{(i + 1) * 100}.00",
                "category_id": cat_id,
                "account_id": account_id,
                "type": "income",
                "occurred_at": f"2026-06-{(i + 1):02d}T10:00:00Z",
            },
            cookies=cookies,
        )
        assert r.status_code == 201

    res = await client.get(
        f"/v1/spending/accounts/{account_id}/ledger",
        cookies=cookies,
        params={"limit": 2, "offset": 0},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_entries"] == 5
    assert len(data["items"]) == 2

    res2 = await client.get(
        f"/v1/spending/accounts/{account_id}/ledger",
        cookies=cookies,
        params={"limit": 2, "offset": 2},
    )
    assert res2.status_code == 200
    data2 = res2.json()
    assert len(data2["items"]) == 2
    page1_ids = {item["public_id"] for item in data["items"]}
    page2_ids = {item["public_id"] for item in data2["items"]}
    assert page1_ids.isdisjoint(page2_ids)


@pytest.mark.asyncio
async def test_ledger_cross_currency_transfer_uses_net_amount_on_receiving_leg(client: AsyncClient):
    """A transfer_in ledger entry (and running balance) must reflect net_amount_received
    in the destination currency, not gross_amount (which is denominated in the source
    currency). Regression test: the ledger previously used gross_amount for both legs,
    which silently over-credited the receiving account by the fee amount whenever fees
    or an FX rate were involved — see app/spending/repository.py get_ledger_page."""
    cookies = await _register_and_login(client, "ledger_xfer@example.com", "ledger_xfer")
    usd_account = await _create_account(client, cookies, "USD Source", "USD")
    gbp_account = await _create_account(client, cookies, "GBP Destination", "GBP")

    transfer_res = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": "spending",
            "to_module": "spending",
            "from_account_id": usd_account,
            "to_account_id": gbp_account,
            "from_currency_code": "USD",
            "to_currency_code": "GBP",
            "gross_amount": "100.00",
            "fx_rate_used": "0.8000000000",
            "fx_fee_amount": "1.00",
            "platform_fee_amount": "2.00",
            "tax_amount": "0.00",
            "net_amount_received": "77.00",
            "occurred_at": "2026-06-01T10:00:00Z",
            "notes": "cross currency",
        },
        cookies=cookies,
    )
    assert transfer_res.status_code == 201, transfer_res.text

    source_ledger = await client.get(f"/v1/spending/accounts/{usd_account}/ledger", cookies=cookies)
    assert source_ledger.status_code == 200
    source_items = source_ledger.json()["items"]
    assert len(source_items) == 1
    assert source_items[0]["entry_kind"] == "transfer_out"
    assert float(source_items[0]["amount"]) == 100.00
    assert float(source_ledger.json()["closing_balance"]) == -100.00

    dest_ledger = await client.get(f"/v1/spending/accounts/{gbp_account}/ledger", cookies=cookies)
    assert dest_ledger.status_code == 200
    dest_items = dest_ledger.json()["items"]
    assert len(dest_items) == 1
    assert dest_items[0]["entry_kind"] == "transfer_in"
    assert float(dest_items[0]["amount"]) == 77.00
    assert float(dest_ledger.json()["closing_balance"]) == 77.00
