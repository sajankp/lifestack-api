from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.auth.models import User
from app.core.database import postgres
from app.imports.models import ImportBatch, ImportModule
from app.platform.models import WorkspaceMembership
from app.spending.models import (
    SpendingCategory,
    SpendingTransaction,
    TransactionSourceType,
    TransactionType,
)


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


@pytest.mark.asyncio
async def test_category_router_endpoints(client: AsyncClient):
    cookies = await _register_and_login(client, "spendingcat@example.com", "spendingcat")

    # 1. List default categories
    list_res = await client.get("/v1/spending/categories", cookies=cookies)
    assert list_res.status_code == 200
    items = list_res.json()["items"]
    assert len(items) > 0  # Should have default system categories

    # 2. Create custom category
    create_res = await client.post(
        "/v1/spending/categories",
        json={"name": "Gaming", "color": "#ff0000", "icon": "gamepad"},
        cookies=cookies,
    )
    assert create_res.status_code == 201
    custom_cat = create_res.json()
    assert custom_cat["name"] == "Gaming"
    assert custom_cat["is_system"] is False
    custom_cat_id = custom_cat["public_id"]

    # 3. Delete custom category
    del_res = await client.delete(f"/v1/spending/categories/{custom_cat_id}", cookies=cookies)
    assert del_res.status_code == 204

    # 4. Check deleted category is gone
    get_res = await client.get("/v1/spending/categories", cookies=cookies)
    items_after = get_res.json()["items"]
    assert not any(item["public_id"] == custom_cat_id for item in items_after)


@pytest.mark.asyncio
async def test_transaction_router_endpoints(client: AsyncClient):
    cookies = await _register_and_login(client, "spendingtx@example.com", "spendingtx")

    # Get a category ID
    list_res = await client.get("/v1/spending/categories", cookies=cookies)
    category_id = list_res.json()["items"][0]["public_id"]

    # Create an account
    account_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Wallet", "account_type": "wallet", "default_currency_code": "USD"},
        cookies=cookies,
    )
    assert account_res.status_code == 201, account_res.text
    account_id = account_res.json()["public_id"]

    # 1. Create a transaction
    tx_data = {
        "amount": 42.50,
        "category_id": category_id,
        "account_id": account_id,
        "type": "expense",
        "description": "Lunch with team",
        "occurred_at": "2026-06-05T12:00:00Z",
    }
    create_res = await client.post("/v1/spending/transactions", json=tx_data, cookies=cookies)
    assert create_res.status_code == 201
    tx = create_res.json()
    assert tx["amount"] == "42.50"
    assert tx["description"] == "Lunch with team"
    tx_id = tx["public_id"]

    # 2. List transactions
    list_tx = await client.get("/v1/spending/transactions", cookies=cookies)
    assert list_tx.status_code == 200
    assert list_tx.json()["total"] == 1
    assert list_tx.json()["items"][0]["public_id"] == tx_id

    # 3. Patch transaction
    patch_res = await client.patch(
        f"/v1/spending/transactions/{tx_id}",
        json={"amount": 45.00, "description": "Lunch with team updated"},
        cookies=cookies,
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["amount"] == "45.00"
    assert patch_res.json()["description"] == "Lunch with team updated"

    # 4. Delete transaction
    del_res = await client.delete(f"/v1/spending/transactions/{tx_id}", cookies=cookies)
    assert del_res.status_code == 204


@pytest.mark.asyncio
async def test_patch_import_sourced_transaction(client: AsyncClient):
    """Regression test: PATCHing a transaction created via import must not crash
    while resolving its source import batch (app/spending/service.py update_transaction_with_details).
    """
    cookies = await _register_and_login(client, "spendingimport@example.com", "spendingimport")

    list_res = await client.get("/v1/spending/categories", cookies=cookies)
    category_id = list_res.json()["items"][0]["public_id"]

    async with postgres.async_session_maker() as session:
        user = (
            await session.execute(select(User).where(User.username == "spendingimport"))
        ).scalar_one()
        membership = (
            await session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
            )
        ).scalar_one()
        workspace_id = membership.workspace_id

        import_batch = ImportBatch(
            workspace_id=workspace_id,
            user_id=user.id,
            module=ImportModule.spending_transactions,
            filename="statement.csv",
            file_sha256="a" * 64,
        )
        session.add(import_batch)
        await session.flush()

        category_row = (
            await session.execute(
                select(SpendingCategory).where(SpendingCategory.public_id == category_id)
            )
        ).scalar_one()

        tx = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=user.id,
            category_id=category_row.id,
            amount=Decimal("12.34"),
            type=TransactionType.expense,
            occurred_at=datetime.now(UTC),
            source_type=TransactionSourceType.imported,
            source_ref=f"import:{import_batch.id}:1",
            source_import_id=import_batch.id,
        )
        session.add(tx)
        await session.commit()
        tx_public_id = tx.public_id
        import_public_id = import_batch.public_id

    patch_res = await client.patch(
        f"/v1/spending/transactions/{tx_public_id}",
        json={"description": "updated via patch"},
        cookies=cookies,
    )
    assert patch_res.status_code == 200, patch_res.text
    body = patch_res.json()
    assert body["description"] == "updated via patch"
    assert body["source_metadata"]["import_public_id"] == str(import_public_id)


@pytest.mark.asyncio
async def test_transaction_list_sorting(client: AsyncClient):
    cookies = await _register_and_login(client, "spendingsort@example.com", "spendingsort")

    list_res = await client.get("/v1/spending/categories", cookies=cookies)
    category_id = list_res.json()["items"][0]["public_id"]

    account_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Wallet", "account_type": "wallet", "default_currency_code": "USD"},
        cookies=cookies,
    )
    account_id = account_res.json()["public_id"]

    # Create three transactions with amount/date deliberately misaligned so a
    # correct sort can only come from the right column (not creation order).
    seeds = [
        {"amount": 30.00, "occurred_at": "2026-06-10T12:00:00Z", "description": "mid"},
        {"amount": 10.00, "occurred_at": "2026-06-20T12:00:00Z", "description": "low"},
        {"amount": 50.00, "occurred_at": "2026-06-01T12:00:00Z", "description": "high"},
    ]
    for seed in seeds:
        res = await client.post(
            "/v1/spending/transactions",
            json={
                "category_id": category_id,
                "account_id": account_id,
                "type": "expense",
                **seed,
            },
            cookies=cookies,
        )
        assert res.status_code == 201, res.text

    async def _amounts(sort: str | None = None) -> list[str]:
        params = {"sort": sort} if sort is not None else {}
        res = await client.get("/v1/spending/transactions", params=params, cookies=cookies)
        assert res.status_code == 200, res.text
        return [item["amount"] for item in res.json()["items"]]

    # Default sort (no param) is newest-created first: 30, 10, 50 were created
    # in that order, so the most-recently-created (50) comes first.
    assert await _amounts() == ["50.00", "10.00", "30.00"]
    assert await _amounts("amount_desc") == ["50.00", "30.00", "10.00"]
    assert await _amounts("amount_asc") == ["10.00", "30.00", "50.00"]
    # date_desc → newest occurred_at first (10 was on the 20th, 50 on the 1st).
    assert await _amounts("date_desc") == ["10.00", "30.00", "50.00"]
    assert await _amounts("date_asc") == ["50.00", "30.00", "10.00"]

    # An invalid sort value is rejected by the enum validation.
    bad = await client.get("/v1/spending/transactions", params={"sort": "bogus"}, cookies=cookies)
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_budget_router_endpoints(client: AsyncClient):
    cookies = await _register_and_login(client, "spendingbudget@example.com", "spendingbudget")

    # Get a category ID
    list_res = await client.get("/v1/spending/categories", cookies=cookies)
    category_id = list_res.json()["items"][0]["public_id"]

    # 1. Create a budget
    budget_data = {
        "category_id": category_id,
        "amount": 500.00,
        "start_month": "2026-06-01",
    }
    create_res = await client.post("/v1/spending/budgets", json=budget_data, cookies=cookies)
    assert create_res.status_code == 201
    budget = create_res.json()
    assert budget["amount"] == "500.00"
    budget_id = budget["public_id"]

    # 2. List budgets
    list_budgets = await client.get(
        "/v1/spending/budgets", params={"month_start": "2026-06-01"}, cookies=cookies
    )
    print("LIST BUDGETS RESPONSE:", list_budgets.text)
    assert list_budgets.status_code == 200
    assert len(list_budgets.json()["items"]) == 1
    assert list_budgets.json()["items"][0]["public_id"] == budget_id

    # 3. Patch budget
    patch_res = await client.patch(
        f"/v1/spending/budgets/{budget_id}",
        json={"amount": 600.00},
        cookies=cookies,
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["amount"] == "600.00"

    # 4. Delete budget (not supported, should return 405)
    del_res = await client.delete(f"/v1/spending/budgets/{budget_id}", cookies=cookies)
    assert del_res.status_code == 405
