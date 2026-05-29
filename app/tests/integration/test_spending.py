"""
Integration tests for the spending module.

Covers the required integration scenarios from spec-003:
  8.1  Workspace isolation for categories
  8.2  Cross-workspace category rejection (transaction create)
  8.3  Budget uniqueness constraint
  8.4  Default-category provisioning during registration

Also covers the full happy-path CRUD flows and the REST contract
(201 on create, 204 on delete, RFC 7807 on errors).
"""

import calendar
import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register_and_login(client: AsyncClient, suffix: str | None = None) -> dict:
    """Register a user and log in; returns {'username': ..., 'password': ...}."""
    tag = suffix or uuid.uuid4().hex[:8]
    username = f"user_{tag}"
    email = f"{username}@example.com"
    password = "Password123!"

    reg = await client.post(
        "/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert reg.status_code == 200, reg.text

    login = await client.post(
        "/v1/auth/login",
        data={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text
    return {"username": username, "password": password, "cookies": dict(login.cookies)}


async def _login(client: AsyncClient, username: str, password: str) -> dict:
    login = await client.post(
        "/v1/auth/login",
        data={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text
    return dict(login.cookies)


# ---------------------------------------------------------------------------
# 8.4 — Default-category provisioning on registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_categories_seeded_on_registration(client: AsyncClient):
    """After registration the workspace must have system categories."""
    creds = await _register_and_login(client)

    # List categories — must include system categories
    resp = await client.get("/v1/spending/categories", cookies=creds["cookies"])
    assert resp.status_code == 200
    cats = resp.json()["items"]
    assert len(cats) > 0, "Expected system categories to be seeded"
    system_cats = [c for c in cats if c["is_system"]]
    assert len(system_cats) == 8, f"Expected 8 system categories, got {len(system_cats)}"

    # Verify no cross-workspace leakage — all categories belong to this user's workspace
    cat_names = {c["name"] for c in cats}
    assert "Food & Dining" in cat_names
    assert "Income" in cat_names


# ---------------------------------------------------------------------------
# 8.1 — Workspace isolation for categories
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_isolation_categories(client: AsyncClient):
    """User A's categories must not be visible to user B."""
    user_a = await _register_and_login(client, "isola")
    user_b = await _register_and_login(client, "isolb")

    # User A creates a custom category
    create_resp = await client.post(
        "/v1/spending/categories",
        json={"name": "User A Classified"},
        cookies=user_a["cookies"],
    )
    assert create_resp.status_code == 201
    a_cat_public_id = create_resp.json()["public_id"]

    # User B lists categories — must NOT see User A's category
    b_list = await client.get("/v1/spending/categories", cookies=user_b["cookies"])
    assert b_list.status_code == 200
    b_cat_ids = {c["public_id"] for c in b_list.json()["items"]}
    assert a_cat_public_id not in b_cat_ids

    # User B fetches User A's category directly → 404 (non-disclosure)
    detail_resp = await client.get(
        f"/v1/spending/categories/{a_cat_public_id}",
        cookies=user_b["cookies"],
    )
    assert detail_resp.status_code == 404
    assert detail_resp.json()["type"] == "https://lifestack.app/errors/not-found"


# ---------------------------------------------------------------------------
# 8.2 — Cross-workspace category rejection (transaction)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_workspace_category_rejected_for_transaction(client: AsyncClient):
    """User A cannot create a transaction using User B's category public_id."""
    user_a = await _register_and_login(client, "cwpa")
    user_b = await _register_and_login(client, "cwpb")

    # User B creates a custom category
    b_cat_resp = await client.post(
        "/v1/spending/categories",
        json={"name": "B Private Category"},
        cookies=user_b["cookies"],
    )
    assert b_cat_resp.status_code == 201
    b_cat_id = b_cat_resp.json()["public_id"]

    # User A tries to create a transaction referencing User B's category
    tx_resp = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": b_cat_id,
            "amount": "50.00",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        cookies=user_a["cookies"],
    )
    # Must be rejected — 404 because the category does not exist in User A's workspace
    assert tx_resp.status_code == 404
    body = tx_resp.json()
    assert "type" in body  # RFC 7807 envelope
    assert "Cross-workspace" in body["detail"]


# ---------------------------------------------------------------------------
# 8.3 — Budget uniqueness constraint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_uniqueness_enforced(client: AsyncClient):
    """Creating two budgets for the same category+month is rejected on the second POST."""
    creds = await _register_and_login(client, "buduniq")

    # Get any system category
    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    cat_id = cats[0]["public_id"]
    month = "2026-03-01"

    # First budget create — must succeed
    first = await client.post(
        "/v1/spending/budgets",
        json={"category_id": cat_id, "amount": "300.00", "month_start": month},
        cookies=creds["cookies"],
    )
    assert first.status_code == 201

    # Second budget create for same category+month — must be rejected
    second = await client.post(
        "/v1/spending/budgets",
        json={"category_id": cat_id, "amount": "400.00", "month_start": month},
        cookies=creds["cookies"],
    )
    assert second.status_code == 409
    body = second.json()
    assert "PATCH" in body["detail"]  # hint to use PATCH for updates

    # Confirm only one budget row exists
    budgets = (await client.get("/v1/spending/budgets", cookies=creds["cookies"])).json()["items"]
    matching = [b for b in budgets if b["category_id"] == cat_id and b["month_start"] == month]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# Budget PATCH — update is allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_update_via_patch(client: AsyncClient):
    creds = await _register_and_login(client, "budpatch")
    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    cat_id = cats[0]["public_id"]
    month = "2026-04-01"

    created = await client.post(
        "/v1/spending/budgets",
        json={"category_id": cat_id, "amount": "200.00", "month_start": month},
        cookies=creds["cookies"],
    )
    assert created.status_code == 201
    budget_id = created.json()["public_id"]

    updated = await client.patch(
        f"/v1/spending/budgets/{budget_id}",
        json={"amount": "350.00"},
        cookies=creds["cookies"],
    )
    assert updated.status_code == 200
    assert updated.json()["amount"] == "350.00"


# ---------------------------------------------------------------------------
# Full happy-path: register → list categories → create transaction → list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_spending_flow(client: AsyncClient):
    creds = await _register_and_login(client, "fullflow")

    # Default categories exist
    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    assert len(cats) > 0
    food_cat = next(c for c in cats if c["name"] == "Food & Dining")

    # Create a custom category
    custom = await client.post(
        "/v1/spending/categories",
        json={"name": "Gym", "color": "#00FF00", "icon": "🏋️"},
        cookies=creds["cookies"],
    )
    assert custom.status_code == 201

    account_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Main Wallet", "account_type": "wallet", "default_currency_code": "USD"},
        cookies=creds["cookies"],
    )
    assert account_res.status_code == 201
    account_id = account_res.json()["public_id"]

    # Create a transaction in the Food category
    tx = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": food_cat["public_id"],
            "account_id": account_id,
            "amount": "42.50",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
            "description": "Dinner",
        },
        cookies=creds["cookies"],
    )
    assert tx.status_code == 201
    tx_data = tx.json()
    assert tx_data["category_id"] == food_cat["public_id"]
    assert tx_data["account_id"] == account_id
    assert tx_data["amount"] == "42.50"


@pytest.mark.asyncio
async def test_cross_workspace_account_rejected_for_transaction(client: AsyncClient):
    user_a = await _register_and_login(client, "accta")
    user_b = await _register_and_login(client, "acctb")

    # User A account
    a_account = await client.post(
        "/v1/finance/accounts",
        json={"name": "A Wallet", "account_type": "wallet", "default_currency_code": "USD"},
        cookies=user_a["cookies"],
    )
    assert a_account.status_code == 201
    a_account_id = a_account.json()["public_id"]

    # User B category
    b_categories = (await client.get("/v1/spending/categories", cookies=user_b["cookies"])).json()[
        "items"
    ]
    b_category_id = b_categories[0]["public_id"]

    # User B should not be able to reference user A account
    tx_resp = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": b_category_id,
            "account_id": a_account_id,
            "amount": "12.00",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        cookies=user_b["cookies"],
    )
    assert tx_resp.status_code == 404
    assert "Cross-workspace account references" in tx_resp.json()["detail"]


@pytest.mark.asyncio
async def test_spending_month_summary_uses_full_month_totals(client: AsyncClient):
    creds = await _register_and_login(client, "sumtot")

    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    food_cat = next(c for c in cats if c["name"] == "Food & Dining")
    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    budget_res = await client.post(
        "/v1/spending/budgets",
        json={
            "category_id": food_cat["public_id"],
            "amount": "1000.00",
            "month_start": month_start.date().isoformat(),
        },
        cookies=creds["cookies"],
    )
    assert budget_res.status_code == 201

    for idx in range(55):
        tx_res = await client.post(
            "/v1/spending/transactions",
            json={
                "category_id": food_cat["public_id"],
                "amount": "1.00",
                "type": "expense",
                "occurred_at": month_start.replace(day=min(idx + 1, 28)).isoformat(),
                "description": f"Txn {idx}",
            },
            cookies=creds["cookies"],
        )
        assert tx_res.status_code == 201

    page_one = await client.get("/v1/spending/transactions", cookies=creds["cookies"])
    assert page_one.status_code == 200
    assert page_one.json()["total"] == 55
    assert len(page_one.json()["items"]) == 50

    summary = await client.get(
        "/v1/spending/transactions/summary",
        params={
            "from_date": month_start.isoformat(),
            "to_date": month_start.replace(
                day=calendar.monthrange(month_start.year, month_start.month)[1],
                hour=23,
                minute=59,
                second=59,
                microsecond=999000,
            ).isoformat(),
        },
        cookies=creds["cookies"],
    )
    assert summary.status_code == 200
    body = summary.json()
    assert body["expense_total"] == "55.00"
    assert body["income_total"] == "0"
    assert body["net_total"] == "-55.00"
    assert body["category_totals"][0]["total"] == "55.00"

    month_budgets = await client.get(
        "/v1/spending/budgets",
        params={"month_start": month_start.date().isoformat()},
        cookies=creds["cookies"],
    )
    assert month_budgets.status_code == 200
    assert all(
        b["month_start"] == month_start.date().isoformat() for b in month_budgets.json()["items"]
    )


# ---------------------------------------------------------------------------
# Cannot delete a system category
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_delete_system_category(client: AsyncClient):
    creds = await _register_and_login(client, "sysdelcat")
    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    system_cat = next(c for c in cats if c["is_system"])

    resp = await client.delete(
        f"/v1/spending/categories/{system_cat['public_id']}",
        cookies=creds["cookies"],
    )
    assert resp.status_code == 403
    assert "type" in resp.json()  # RFC 7807


# ---------------------------------------------------------------------------
# Cannot delete a category that has transactions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_delete_category_with_transactions(client: AsyncClient):
    creds = await _register_and_login(client, "catwithtx")

    # Create a custom category
    cat_resp = await client.post(
        "/v1/spending/categories",
        json={"name": "Test Delete Category"},
        cookies=creds["cookies"],
    )
    cat_id = cat_resp.json()["public_id"]

    # Create a transaction against it
    await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": cat_id,
            "amount": "10.00",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        cookies=creds["cookies"],
    )

    # Delete must be rejected
    del_resp = await client.delete(f"/v1/spending/categories/{cat_id}", cookies=creds["cookies"])
    assert del_resp.status_code == 409
    assert "type" in del_resp.json()


# ---------------------------------------------------------------------------
# Duplicate category name rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_category_name_rejected(client: AsyncClient):
    creds = await _register_and_login(client, "dupcat")

    await client.post(
        "/v1/spending/categories",
        json={"name": "UniqueNameTest"},
        cookies=creds["cookies"],
    )
    second = await client.post(
        "/v1/spending/categories",
        json={"name": "uniquenametest"},  # case-insensitive
        cookies=creds["cookies"],
    )
    assert second.status_code == 409
