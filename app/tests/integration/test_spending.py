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
from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import postgres
from app.core.exceptions import NotFoundError
from app.spending.response_helpers import category_public_id_or_404 as _category_public_id_or_404

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


def test_category_public_id_or_404_rejects_missing_transaction_category():
    with pytest.raises(NotFoundError) as exc_info:
        _category_public_id_or_404({}, 999)
    assert exc_info.value.detail == "Transaction category was not found"


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


@pytest.mark.asyncio
async def test_manual_transaction_exposes_manual_source_metadata(client: AsyncClient):
    creds = await _register_and_login(client, "manualsource")

    categories = await client.get("/v1/spending/categories", cookies=creds["cookies"])
    assert categories.status_code == 200
    category_id = categories.json()["items"][0]["public_id"]

    account_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Wallet", "account_type": "wallet", "default_currency_code": "INR"},
        cookies=creds["cookies"],
    )
    assert account_res.status_code == 201, account_res.text
    account_id = account_res.json()["public_id"]

    create = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": category_id,
            "account_id": account_id,
            "amount": "12.50",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
            "description": "manual coffee",
        },
        cookies=creds["cookies"],
    )

    assert create.status_code == 201, create.text
    body = create.json()
    assert body["source_type"] == "manual"
    assert body["source_ref"] is None
    assert body["source_metadata"] == {
        "source_type": "manual",
        "source_ref": None,
        "origin": "manual_entry",
        "label": "Manual entry",
        "import_public_id": None,
        "import_module": None,
        "import_row_number": None,
        "rollback_supported": False,
    }


@pytest.mark.asyncio
async def test_transactions_and_summary_filter_by_account(client: AsyncClient):
    creds = await _register_and_login(client, "accountfilter")
    cookies = creds["cookies"]
    categories = await client.get("/v1/spending/categories", cookies=cookies)
    category_id = categories.json()["items"][0]["public_id"]

    account_ids: list[str] = []
    for name in ("Daily Wallet", "Travel Card"):
        response = await client.post(
            "/v1/finance/accounts",
            json={
                "name": name,
                "account_type": "wallet",
                "default_currency_code": "USD",
            },
            cookies=cookies,
        )
        assert response.status_code == 201, response.text
        account_ids.append(response.json()["public_id"])

    occurred_at = datetime.now(UTC).isoformat()
    for account_id, amount in zip(account_ids, ("25.00", "75.00"), strict=True):
        response = await client.post(
            "/v1/spending/transactions",
            json={
                "category_id": category_id,
                "account_id": account_id,
                "amount": amount,
                "type": "expense",
                "occurred_at": occurred_at,
            },
            cookies=cookies,
        )
        assert response.status_code == 201, response.text

    filtered = await client.get(
        "/v1/spending/transactions",
        params={"account_id": account_ids[0]},
        cookies=cookies,
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["account_id"] == account_ids[0]

    summary = await client.get(
        "/v1/spending/transactions/summary",
        params={
            "account_id": account_ids[0],
            "from_date": "2020-01-01T00:00:00Z",
            "to_date": "2030-01-01T00:00:00Z",
        },
        cookies=cookies,
    )
    assert summary.status_code == 200, summary.text
    assert summary.json()["expense_total"] == "25.00"


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
        json={"category_id": cat_id, "amount": "300.00", "start_month": month},
        cookies=creds["cookies"],
    )
    assert first.status_code == 201

    # Second budget create for same category+month — must be rejected
    second = await client.post(
        "/v1/spending/budgets",
        json={"category_id": cat_id, "amount": "400.00", "start_month": month},
        cookies=creds["cookies"],
    )
    assert second.status_code == 409
    body = second.json()
    assert "PATCH" in body["detail"]  # hint to use PATCH for updates

    # Confirm only one budget row exists
    budgets = (await client.get("/v1/spending/budgets", cookies=creds["cookies"])).json()["items"]
    matching = [b for b in budgets if b["category_id"] == cat_id and b["start_month"] == month]
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
        json={"category_id": cat_id, "amount": "200.00", "start_month": month},
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

    account_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Wallet", "account_type": "wallet", "default_currency_code": "INR"},
        cookies=creds["cookies"],
    )
    assert account_res.status_code == 201, account_res.text
    account_id = account_res.json()["public_id"]

    budget_res = await client.post(
        "/v1/spending/budgets",
        json={
            "category_id": food_cat["public_id"],
            "amount": "1000.00",
            "start_month": month_start.date().isoformat(),
        },
        cookies=creds["cookies"],
    )
    assert budget_res.status_code == 201

    for idx in range(55):
        tx_res = await client.post(
            "/v1/spending/transactions",
            json={
                "category_id": food_cat["public_id"],
                "account_id": account_id,
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
        b["start_month"] == month_start.date().isoformat() for b in month_budgets.json()["items"]
    )


# ---------------------------------------------------------------------------
# System categories: deletable when unused, still refused when in use
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unused_system_category_deletes_successfully(client: AsyncClient):
    creds = await _register_and_login(client, "sysdelcat")
    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    system_cat = next(c for c in cats if c["is_system"])

    resp = await client.delete(
        f"/v1/spending/categories/{system_cat['public_id']}",
        cookies=creds["cookies"],
    )
    assert resp.status_code == 204, resp.text


@pytest.mark.asyncio
async def test_in_use_system_category_still_refused(client: AsyncClient):
    creds = await _register_and_login(client, "sysdelcatinuse")
    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    system_cat = next(c for c in cats if c["is_system"])

    account_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Wallet", "account_type": "wallet", "default_currency_code": "INR"},
        cookies=creds["cookies"],
    )
    assert account_res.status_code == 201, account_res.text
    account_id = account_res.json()["public_id"]

    tx_resp = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": system_cat["public_id"],
            "account_id": account_id,
            "amount": "10.00",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        cookies=creds["cookies"],
    )
    assert tx_resp.status_code == 201, tx_resp.text

    resp = await client.delete(
        f"/v1/spending/categories/{system_cat['public_id']}",
        cookies=creds["cookies"],
    )
    assert resp.status_code == 409, resp.text
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

    account_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Wallet", "account_type": "wallet", "default_currency_code": "INR"},
        cookies=creds["cookies"],
    )
    assert account_res.status_code == 201, account_res.text
    account_id = account_res.json()["public_id"]

    # Create a transaction against it
    tx_resp = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": cat_id,
            "account_id": account_id,
            "amount": "10.00",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        cookies=creds["cookies"],
    )
    assert tx_resp.status_code == 201, tx_resp.text

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


@pytest.mark.asyncio
async def test_cannot_delete_category_with_budget(client: AsyncClient):
    creds = await _register_and_login(client, "catwithbudget")

    # Create a custom category
    cat_resp = await client.post(
        "/v1/spending/categories",
        json={"name": "Test Delete Category Budget"},
        cookies=creds["cookies"],
    )
    assert cat_resp.status_code == 201
    cat_id = cat_resp.json()["public_id"]

    # Create a budget against it
    budget_resp = await client.post(
        "/v1/spending/budgets",
        json={
            "category_id": cat_id,
            "amount": "500.00",
            "start_month": date.today().replace(day=1).isoformat(),
        },
        cookies=creds["cookies"],
    )
    assert budget_resp.status_code == 201

    # Delete must be rejected
    del_resp = await client.delete(f"/v1/spending/categories/{cat_id}", cookies=creds["cookies"])
    assert del_resp.status_code == 409
    assert "Cannot delete a category" in del_resp.json()["detail"]


# ---------------------------------------------------------------------------
# Category merge (spec-062)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_categories_repoints_transactions_recurring_and_deletes_sources(
    client: AsyncClient,
):
    creds = await _register_and_login(client, "mergecats")

    async def _make_category(name: str) -> str:
        resp = await client.post(
            "/v1/spending/categories", json={"name": name}, cookies=creds["cookies"]
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["public_id"]

    target_id = await _make_category("Dining")
    source_a = await _make_category("Food")
    source_b = await _make_category("Restaurants")

    account_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Wallet", "account_type": "wallet", "default_currency_code": "INR"},
        cookies=creds["cookies"],
    )
    assert account_res.status_code == 201, account_res.text
    account_id = account_res.json()["public_id"]

    tx_resp = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": source_a,
            "account_id": account_id,
            "amount": "10.00",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        cookies=creds["cookies"],
    )
    assert tx_resp.status_code == 201, tx_resp.text
    tx_id = tx_resp.json()["public_id"]

    recurring_resp = await client.post(
        "/v1/spending/recurring",
        json={
            "category_id": source_b,
            "amount": "14.99",
            "type": "expense",
            "frequency": "monthly",
            "interval": 1,
            "anchor_date": date.today().isoformat(),
        },
        cookies=creds["cookies"],
    )
    assert recurring_resp.status_code == 201, recurring_resp.text
    recurring_id = recurring_resp.json()["public_id"]

    merge_resp = await client.post(
        f"/v1/spending/categories/{target_id}/merge",
        json={"source_public_ids": [source_a, source_b]},
        cookies=creds["cookies"],
    )
    assert merge_resp.status_code == 204, merge_resp.text

    tx_after = (await client.get("/v1/spending/transactions", cookies=creds["cookies"])).json()[
        "items"
    ]
    moved_tx = next(t for t in tx_after if t["public_id"] == tx_id)
    assert moved_tx["category_id"] == target_id

    recurring_after = (await client.get("/v1/spending/recurring", cookies=creds["cookies"])).json()[
        "items"
    ]
    moved_recurring = next(r for r in recurring_after if r["public_id"] == recurring_id)
    assert moved_recurring["category_id"] == target_id

    cats_after = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()[
        "items"
    ]
    remaining_ids = {c["public_id"] for c in cats_after}
    assert source_a not in remaining_ids
    assert source_b not in remaining_ids
    assert target_id in remaining_ids


@pytest.mark.asyncio
async def test_merge_categories_sums_overlapping_budgets_for_same_month(client: AsyncClient):
    creds = await _register_and_login(client, "mergebudgets")

    async def _make_category(name: str) -> str:
        resp = await client.post(
            "/v1/spending/categories", json={"name": name}, cookies=creds["cookies"]
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["public_id"]

    target_id = await _make_category("Groceries")
    source_id = await _make_category("Supermarket")

    month_start = date.today().replace(day=1).isoformat()

    for cat_id, amount in ((target_id, "300.00"), (source_id, "200.00")):
        budget_resp = await client.post(
            "/v1/spending/budgets",
            json={
                "category_id": cat_id,
                "amount": amount,
                "start_month": month_start,
                "end_month": month_start,
            },
            cookies=creds["cookies"],
        )
        assert budget_resp.status_code == 201, budget_resp.text

    merge_resp = await client.post(
        f"/v1/spending/categories/{target_id}/merge",
        json={"source_public_ids": [source_id]},
        cookies=creds["cookies"],
    )
    assert merge_resp.status_code == 204, merge_resp.text

    budgets_after = (
        await client.get(
            "/v1/spending/budgets",
            params={"month_start": month_start},
            cookies=creds["cookies"],
        )
    ).json()["items"]
    target_budgets = [b for b in budgets_after if b["category_id"] == target_id]
    assert len(target_budgets) == 1
    assert target_budgets[0]["amount"] == "500.00"


@pytest.mark.asyncio
async def test_merge_categories_validation_rejects_target_in_sources_and_empty_sources(
    client: AsyncClient,
):
    creds = await _register_and_login(client, "mergevalidation")

    cat_resp = await client.post(
        "/v1/spending/categories", json={"name": "Solo"}, cookies=creds["cookies"]
    )
    assert cat_resp.status_code == 201
    cat_id = cat_resp.json()["public_id"]

    resp = await client.post(
        f"/v1/spending/categories/{cat_id}/merge",
        json={"source_public_ids": [cat_id]},
        cookies=creds["cookies"],
    )
    assert resp.status_code == 422, resp.text

    resp = await client.post(
        f"/v1/spending/categories/{cat_id}/merge",
        json={"source_public_ids": []},
        cookies=creds["cookies"],
    )
    assert resp.status_code == 422, resp.text

    resp = await client.post(
        f"/v1/spending/categories/{cat_id}/merge",
        json={"source_public_ids": [str(uuid.uuid4())]},
        cookies=creds["cookies"],
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_merge_categories_records_one_audit_event(client: AsyncClient):
    creds = await _register_and_login(client, "mergeaudit")

    async def _make_category(name: str) -> str:
        resp = await client.post(
            "/v1/spending/categories", json={"name": name}, cookies=creds["cookies"]
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["public_id"]

    target_id = await _make_category("Bills")
    source_id = await _make_category("Utilities")

    merge_resp = await client.post(
        f"/v1/spending/categories/{target_id}/merge",
        json={"source_public_ids": [source_id]},
        cookies=creds["cookies"],
    )
    assert merge_resp.status_code == 204, merge_resp.text

    async with postgres.async_session_maker() as session:
        audit_rows = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "merge", AuditLog.entity_type == "spending_category"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audit_rows) == 1
        details = audit_rows[0].details
        assert details["target_public_id"] == target_id
        assert details["source_public_ids"] == [source_id]
        assert details["transactions_moved"] == 0
        assert details["recurring_rules_moved"] == 0


@pytest.mark.asyncio
async def test_cannot_delete_category_with_recurring_rule(client: AsyncClient):
    creds = await _register_and_login(client, "catwithrecurring")

    # Create a custom category
    cat_resp = await client.post(
        "/v1/spending/categories",
        json={"name": "Test Delete Category Recurring"},
        cookies=creds["cookies"],
    )
    assert cat_resp.status_code == 201
    cat_id = cat_resp.json()["public_id"]

    # Create a recurring rule against it
    recurring_resp = await client.post(
        "/v1/spending/recurring",
        json={
            "category_id": cat_id,
            "amount": "14.99",
            "type": "expense",
            "description": "Test delete guard with recurring transaction",
            "frequency": "monthly",
            "interval": 1,
            "anchor_date": date.today().isoformat(),
        },
        cookies=creds["cookies"],
    )
    assert recurring_resp.status_code == 201

    # Delete must be rejected
    del_resp = await client.delete(f"/v1/spending/categories/{cat_id}", cookies=creds["cookies"])
    assert del_resp.status_code == 409
    assert "Cannot delete a category" in del_resp.json()["detail"]
