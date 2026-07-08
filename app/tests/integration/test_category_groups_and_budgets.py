"""Integration tests for spec-064: category groups and recurring, date-ranged
budgets — the golden scenarios not already covered elsewhere:
  - Category group CRUD + delete guard + un-grouping on delete
  - Budget non-overlap validation
  - Atomic change-amount segmentation
  - Dashboard budget spotlight (group-only)
  - Budget performance parallel category/group lists and totals
"""

from datetime import date

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


async def _create_account(client: AsyncClient, cookies: dict) -> str:
    resp = await client.post(
        "/v1/finance/accounts",
        json={"name": "Wallet", "account_type": "wallet", "default_currency_code": "USD"},
        cookies=cookies,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["public_id"]


async def _create_category(client: AsyncClient, cookies: dict, name: str) -> str:
    resp = await client.post("/v1/spending/categories", json={"name": name}, cookies=cookies)
    assert resp.status_code == 201, resp.text
    return resp.json()["public_id"]


async def _create_group(client: AsyncClient, cookies: dict, name: str) -> str:
    resp = await client.post("/v1/spending/category-groups", json={"name": name}, cookies=cookies)
    assert resp.status_code == 201, resp.text
    return resp.json()["public_id"]


def _month(year: int, month: int) -> str:
    return date(year, month, 1).isoformat()


# ---------------------------------------------------------------------------
# Category groups: CRUD, delete guard, un-grouping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_category_group_crud_and_assignment(client: AsyncClient):
    cookies = await _register_and_login(client, "groupcrud")

    group_id = await _create_group(client, cookies, "Household")
    cat_id = await _create_category(client, cookies, "Groceries")

    update_resp = await client.patch(
        f"/v1/spending/categories/{cat_id}",
        json={"category_group_id": group_id},
        cookies=cookies,
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["category_group_id"] == group_id

    rename_resp = await client.patch(
        f"/v1/spending/category-groups/{group_id}",
        json={"name": "Home"},
        cookies=cookies,
    )
    assert rename_resp.status_code == 200, rename_resp.text
    assert rename_resp.json()["name"] == "Home"


@pytest.mark.asyncio
async def test_delete_group_ungroups_members_without_touching_categories(client: AsyncClient):
    cookies = await _register_and_login(client, "groupdelete")

    group_id = await _create_group(client, cookies, "Household")
    cat_id = await _create_category(client, cookies, "Utilities")
    await client.patch(
        f"/v1/spending/categories/{cat_id}",
        json={"category_group_id": group_id},
        cookies=cookies,
    )

    del_resp = await client.delete(f"/v1/spending/category-groups/{group_id}", cookies=cookies)
    assert del_resp.status_code == 204, del_resp.text

    cat_resp = await client.get(f"/v1/spending/categories/{cat_id}", cookies=cookies)
    assert cat_resp.status_code == 200
    assert cat_resp.json()["category_group_id"] is None


@pytest.mark.asyncio
async def test_delete_group_refused_while_budget_covers_current_month(client: AsyncClient):
    cookies = await _register_and_login(client, "groupdeleteguard")

    group_id = await _create_group(client, cookies, "Household")
    today = date.today()

    budget_resp = await client.post(
        "/v1/spending/budgets",
        json={
            "category_group_id": group_id,
            "amount": "500.00",
            "start_month": today.replace(day=1).isoformat(),
        },
        cookies=cookies,
    )
    assert budget_resp.status_code == 201, budget_resp.text

    del_resp = await client.delete(f"/v1/spending/category-groups/{group_id}", cookies=cookies)
    assert del_resp.status_code == 409, del_resp.text


# ---------------------------------------------------------------------------
# Budget non-overlap validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlapping_category_budgets_rejected_and_adjacent_segments_persist(
    client: AsyncClient,
):
    cookies = await _register_and_login(client, "budgetoverlap")
    cat_id = await _create_category(client, cookies, "Groceries")

    first = await client.post(
        "/v1/spending/budgets",
        json={
            "category_id": cat_id,
            "amount": "500.00",
            "start_month": _month(2026, 1),
            "end_month": _month(2026, 6),
        },
        cookies=cookies,
    )
    assert first.status_code == 201, first.text

    overlapping = await client.post(
        "/v1/spending/budgets",
        json={
            "category_id": cat_id,
            "amount": "600.00",
            "start_month": _month(2026, 6),
            "end_month": None,
        },
        cookies=cookies,
    )
    assert overlapping.status_code == 409, overlapping.text

    adjacent = await client.post(
        "/v1/spending/budgets",
        json={
            "category_id": cat_id,
            "amount": "600.00",
            "start_month": _month(2026, 7),
        },
        cookies=cookies,
    )
    assert adjacent.status_code == 201, adjacent.text


# ---------------------------------------------------------------------------
# Atomic change-amount segmentation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_amount_segments_budget_preserving_history(client: AsyncClient):
    cookies = await _register_and_login(client, "changeamount")
    cat_id = await _create_category(client, cookies, "Groceries")

    create_resp = await client.post(
        "/v1/spending/budgets",
        json={
            "category_id": cat_id,
            "amount": "500.00",
            "start_month": _month(2026, 1),
        },
        cookies=cookies,
    )
    assert create_resp.status_code == 201, create_resp.text
    budget_id = create_resp.json()["public_id"]

    change_resp = await client.post(
        f"/v1/spending/budgets/{budget_id}/change-amount",
        json={"amount": "600.00", "from_month": _month(2026, 7)},
        cookies=cookies,
    )
    assert change_resp.status_code == 200, change_resp.text
    successor = change_resp.json()
    assert successor["amount"] == "600.00"
    assert successor["start_month"] == _month(2026, 7)
    assert successor["end_month"] is None

    all_budgets = (
        await client.get("/v1/spending/budgets", params={"limit": 50}, cookies=cookies)
    ).json()["items"]
    original = next(b for b in all_budgets if b["public_id"] == budget_id)
    assert original["amount"] == "500.00"
    assert original["end_month"] == _month(2026, 6)

    rejected = await client.post(
        f"/v1/spending/budgets/{budget_id}/change-amount",
        json={"amount": "700.00", "from_month": _month(2025, 12)},
        cookies=cookies,
    )
    assert rejected.status_code == 422, rejected.text


# ---------------------------------------------------------------------------
# Dashboard budget spotlight (group-only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_budget_spotlight_shows_group_budgets_only(client: AsyncClient):
    cookies = await _register_and_login(client, "spotlight")
    account_id = await _create_account(client, cookies)
    group_id = await _create_group(client, cookies, "Household")
    cat_id = await _create_category(client, cookies, "Groceries")
    await client.patch(
        f"/v1/spending/categories/{cat_id}",
        json={"category_group_id": group_id},
        cookies=cookies,
    )

    today = date.today()
    budget_resp = await client.post(
        "/v1/spending/budgets",
        json={
            "category_group_id": group_id,
            "amount": "1000.00",
            "start_month": today.replace(day=1).isoformat(),
        },
        cookies=cookies,
    )
    assert budget_resp.status_code == 201, budget_resp.text

    tx_resp = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": cat_id,
            "account_id": account_id,
            "amount": "300.00",
            "type": "expense",
            "occurred_at": today.isoformat() + "T00:00:00Z",
        },
        cookies=cookies,
    )
    assert tx_resp.status_code == 201, tx_resp.text

    summary_resp = await client.get("/v1/dashboard/summary", cookies=cookies)
    assert summary_resp.status_code == 200, summary_resp.text
    summary = summary_resp.json()
    assert "month_budget" not in summary["spending"]
    spotlight = summary["spending"]["budget_spotlight"]
    assert len(spotlight) == 1
    assert spotlight[0]["category_group_id"] == group_id
    assert spotlight[0]["actual_amount"] == "300.00"
    assert spotlight[0]["budget_amount"] == "1000.00"


# ---------------------------------------------------------------------------
# Budget performance: parallel category and group lists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_performance_returns_parallel_category_and_group_lists(client: AsyncClient):
    cookies = await _register_and_login(client, "perfgroups")
    account_id = await _create_account(client, cookies)
    group_id = await _create_group(client, cookies, "Household")

    cat_ids = []
    for name in ("Groceries", "Utilities", "Rent"):
        cat_id = await _create_category(client, cookies, name)
        await client.patch(
            f"/v1/spending/categories/{cat_id}",
            json={"category_group_id": group_id},
            cookies=cookies,
        )
        cat_ids.append(cat_id)

    group_budget_resp = await client.post(
        "/v1/spending/budgets",
        json={
            "category_group_id": group_id,
            "amount": "900.00",
            "start_month": _month(2026, 6),
            "end_month": _month(2026, 6),
        },
        cookies=cookies,
    )
    assert group_budget_resp.status_code == 201, group_budget_resp.text

    for cat_id, amount in zip(cat_ids, ("100.00", "200.00", "300.00"), strict=True):
        tx_resp = await client.post(
            "/v1/spending/transactions",
            json={
                "category_id": cat_id,
                "account_id": account_id,
                "amount": amount,
                "type": "expense",
                "occurred_at": "2026-06-05T00:00:00Z",
            },
            cookies=cookies,
        )
        assert tx_resp.status_code == 201, tx_resp.text

    perf_resp = await client.get(
        "/v1/spending/analytics/budget-performance",
        params={"from": _month(2026, 6), "to": _month(2026, 6)},
        cookies=cookies,
    )
    assert perf_resp.status_code == 200, perf_resp.text
    data = perf_resp.json()

    category_ids_in_response = {c["category_id"] for c in data["categories"]}
    assert set(cat_ids).issubset(category_ids_in_response)

    assert len(data["groups"]) == 1
    group_item = data["groups"][0]
    assert group_item["category_group_id"] == group_id
    assert group_item["actual_amount"] == "600.00"
    assert group_item["budget_amount"] == "900.00"

    assert "group_totals" in data
    assert data["group_totals"]["total_actual"] == "600.00"
    assert data["group_totals"]["total_budgeted"] == "900.00"
    # Category and group totals are never combined into one figure.
    assert data["totals"]["total_budgeted"] != data["group_totals"]["total_budgeted"]
