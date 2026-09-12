"""Integration tests for spending account ledger pagination and running balance continuity."""

from decimal import Decimal

import pytest
from httpx import AsyncClient


async def _register_and_login(client: AsyncClient, email: str, username: str) -> None:
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


async def _create_account(client: AsyncClient, name: str, account_type: str = "bank") -> str:
    res = await client.post(
        "/v1/finance/accounts",
        json={"name": name, "account_type": account_type, "default_currency_code": "USD"},
    )
    assert res.status_code == 201, res.text
    return res.json()["public_id"]


async def _first_category(client: AsyncClient) -> str:
    res = await client.get("/v1/spending/categories")
    assert res.status_code == 200
    return res.json()["items"][0]["public_id"]


@pytest.mark.asyncio
async def test_ledger_pagination_running_balance_continuity(client: AsyncClient) -> None:
    """Verify that ledger pagination correctly maintains running balance continuity

    across pages with mixed transactions and capital transfers on same timestamps.
    """
    await _register_and_login(client, "ledger_pg_user@example.com", "ledger_pg_user")
    acc1_id = await _create_account(client, "Primary Checking")
    acc2_id = await _create_account(client, "Savings")
    cat_id = await _first_category(client)

    # Insert 6 transactions and transfers with specific ordering
    # Chronological timeline (oldest to newest):
    # 1. 2026-08-01 10:00:00: Income transaction +1000.00 -> balance 1000.00
    # 2. 2026-08-02 12:00:00: Expense transaction -200.00 -> balance 800.00
    # 3. 2026-08-02 12:00:00: Transfer out to Savings -300.00 -> balance 500.00 (same timestamp)
    # 4. 2026-08-03 09:00:00: Income transaction +500.00 -> balance 1000.00
    # 5. 2026-08-04 15:00:00: Transfer in from Savings +150.00 -> balance 1150.00
    # 6. 2026-08-05 18:00:00: Expense transaction -50.00 -> balance 1100.00

    # 1. Income +1000
    res = await client.post(
        "/v1/spending/transactions",
        json={
            "amount": "1000.00",
            "type": "income",
            "account_id": acc1_id,
            "category_id": cat_id,
            "occurred_at": "2026-08-01T10:00:00Z",
            "description": "Salary",
        },
    )
    assert res.status_code == 201

    # 2. Expense -200
    res = await client.post(
        "/v1/spending/transactions",
        json={
            "amount": "200.00",
            "type": "expense",
            "account_id": acc1_id,
            "category_id": cat_id,
            "occurred_at": "2026-08-02T12:00:00Z",
            "description": "Groceries",
        },
    )
    assert res.status_code == 201

    # 3. Transfer out -300
    res = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": "spending",
            "to_module": "spending",
            "from_account_id": acc1_id,
            "to_account_id": acc2_id,
            "gross_amount": "300.00",
            "net_amount_received": "300.00",
            "from_currency_code": "USD",
            "to_currency_code": "USD",
            "occurred_at": "2026-08-02T12:00:00Z",
            "notes": "Savings transfer",
        },
    )
    assert res.status_code == 201

    # 4. Income +500
    res = await client.post(
        "/v1/spending/transactions",
        json={
            "amount": "500.00",
            "type": "income",
            "account_id": acc1_id,
            "category_id": cat_id,
            "occurred_at": "2026-08-03T09:00:00Z",
            "description": "Consulting",
        },
    )
    assert res.status_code == 201

    # 5. Transfer in +150
    res = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": "spending",
            "to_module": "spending",
            "from_account_id": acc2_id,
            "to_account_id": acc1_id,
            "gross_amount": "150.00",
            "net_amount_received": "150.00",
            "from_currency_code": "USD",
            "to_currency_code": "USD",
            "occurred_at": "2026-08-04T15:00:00Z",
            "notes": "Return savings",
        },
    )
    assert res.status_code == 201

    # 6. Expense -50
    res = await client.post(
        "/v1/spending/transactions",
        json={
            "amount": "50.00",
            "type": "expense",
            "account_id": acc1_id,
            "category_id": cat_id,
            "occurred_at": "2026-08-05T18:00:00Z",
            "description": "Coffee",
        },
    )
    assert res.status_code == 201

    # Fetch full ledger on 1 page (limit=10)
    res = await client.get(f"/v1/spending/accounts/{acc1_id}/ledger?limit=10&offset=0")
    assert res.status_code == 200
    full_ledger = res.json()
    assert full_ledger["total_entries"] == 6
    assert Decimal(str(full_ledger["closing_balance"])) == Decimal("1100.00")
    assert Decimal(str(full_ledger["opening_balance"])) == Decimal("0.00")

    full_balances = [Decimal(str(item["running_balance"])) for item in full_ledger["items"]]
    # In desc order:
    # 6. Expense -50: balance = 1100.00
    # 5. Transfer in +150: balance = 1150.00
    # 4. Income +500: balance = 1000.00
    # 3. Transaction/transfer on 2026-08-02
    # 2. Transaction/transfer on 2026-08-02
    # 1. Income +1000: balance = 1000.00
    assert full_balances[0] == Decimal("1100.00")
    assert full_balances[1] == Decimal("1150.00")
    assert full_balances[2] == Decimal("1000.00")
    assert full_balances[-1] == Decimal("1000.00")

    # Now fetch page-by-page with limit=2 (3 pages total)
    # Page 1 (offset=0, limit=2) -> items 0, 1
    p1 = (await client.get(f"/v1/spending/accounts/{acc1_id}/ledger?limit=2&offset=0")).json()
    assert len(p1["items"]) == 2
    assert Decimal(str(p1["items"][0]["running_balance"])) == full_balances[0]
    assert Decimal(str(p1["items"][1]["running_balance"])) == full_balances[1]
    assert Decimal(str(p1["closing_balance"])) == full_balances[0]

    # Page 2 (offset=2, limit=2) -> items 2, 3
    p2 = (await client.get(f"/v1/spending/accounts/{acc1_id}/ledger?limit=2&offset=2")).json()
    assert len(p2["items"]) == 2
    assert Decimal(str(p2["items"][0]["running_balance"])) == full_balances[2]
    assert Decimal(str(p2["items"][1]["running_balance"])) == full_balances[3]
    # Page 1 opening balance should match Page 2 closing balance!
    assert Decimal(str(p1["opening_balance"])) == Decimal(str(p2["closing_balance"]))

    # Page 3 (offset=4, limit=2) -> items 4, 5
    p3 = (await client.get(f"/v1/spending/accounts/{acc1_id}/ledger?limit=2&offset=4")).json()
    assert len(p3["items"]) == 2
    assert Decimal(str(p3["items"][0]["running_balance"])) == full_balances[4]
    assert Decimal(str(p3["items"][1]["running_balance"])) == full_balances[5]
    # Page 2 opening balance should match Page 3 closing balance!
    assert Decimal(str(p2["opening_balance"])) == Decimal(str(p3["closing_balance"]))
    assert Decimal(str(p3["opening_balance"])) == Decimal("0.00")
