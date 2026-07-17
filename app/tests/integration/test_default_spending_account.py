"""
Integration tests for spec-054 (mandatory account on spending transactions) and
spec-084 (account resolution on recurring transactions, same resolver reused).

Covers the golden scenarios:
  1. Create-time resolution order + 422/404 rejections
  2. Default-account management (set/clear, deactivation clears it)
  3. Forward-only enforcement (existing NULL rows untouched)
  4. Import resolution order (row name -> target account -> workspace default -> error)
  5. (web-side coverage lives in lifestack-web's vitest suite)
  6. Recurring transactions reuse the same create-time resolver (spec-084)
"""

import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.auth.models import User
from app.core.database import postgres
from app.platform.models import WorkspaceMembership
from app.spending.models import RecurringTransaction, SpendingCategory, SpendingTransaction


async def _register_and_login(client: AsyncClient, suffix: str) -> dict:
    username = f"defacct_{suffix}"
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
    return {"cookies": dict(login.cookies)}


async def _create_account(
    client: AsyncClient, cookies: dict, name: str = "Wallet", account_type: str = "wallet"
) -> str:
    response = await client.post(
        "/v1/finance/accounts",
        json={"name": name, "account_type": account_type, "default_currency_code": "USD"},
        cookies=cookies,
    )
    assert response.status_code == 201, response.text
    return response.json()["public_id"]


async def _first_category_id(client: AsyncClient, cookies: dict) -> str:
    categories = await client.get("/v1/spending/categories", cookies=cookies)
    assert categories.status_code == 200
    return categories.json()["items"][0]["public_id"]


# ---------------------------------------------------------------------------
# 1. Resolution order at create time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_uses_explicit_account_id(client: AsyncClient):
    creds = await _register_and_login(client, "explicit")
    cookies = creds["cookies"]
    category_id = await _first_category_id(client, cookies)
    account_id = await _create_account(client, cookies)

    resp = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": category_id,
            "account_id": account_id,
            "amount": "5.00",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        cookies=cookies,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["account_id"] == account_id


@pytest.mark.asyncio
async def test_create_falls_back_to_workspace_default_account(client: AsyncClient):
    creds = await _register_and_login(client, "fallback")
    cookies = creds["cookies"]
    category_id = await _first_category_id(client, cookies)
    account_id = await _create_account(client, cookies)

    settings_resp = await client.patch(
        "/v1/finance/settings",
        json={"default_spending_account_id": account_id},
        cookies=cookies,
    )
    assert settings_resp.status_code == 200, settings_resp.text
    assert settings_resp.json()["default_spending_account_id"] == account_id

    resp = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": category_id,
            "amount": "7.50",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        cookies=cookies,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["account_id"] == account_id


@pytest.mark.asyncio
async def test_create_rejected_when_no_account_and_no_default(client: AsyncClient):
    creds = await _register_and_login(client, "noaccount")
    cookies = creds["cookies"]
    category_id = await _first_category_id(client, cookies)

    resp = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": category_id,
            "amount": "3.00",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        cookies=cookies,
    )
    assert resp.status_code == 422, resp.text
    assert "account_id" in resp.json()["detail"] or "default" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_rejects_inactive_account_id(client: AsyncClient):
    creds = await _register_and_login(client, "inactive")
    cookies = creds["cookies"]
    category_id = await _first_category_id(client, cookies)
    account_id = await _create_account(client, cookies)

    deactivate = await client.patch(
        f"/v1/finance/accounts/{account_id}",
        json={"is_active": False},
        cookies=cookies,
    )
    assert deactivate.status_code == 200, deactivate.text

    resp = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": category_id,
            "account_id": account_id,
            "amount": "3.00",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        cookies=cookies,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_create_rejects_foreign_workspace_account_id(client: AsyncClient):
    owner = await _register_and_login(client, "ownerws")
    other = await _register_and_login(client, "otherws")

    category_id = await _first_category_id(client, other["cookies"])
    foreign_account_id = await _create_account(client, owner["cookies"])

    resp = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": category_id,
            "account_id": foreign_account_id,
            "amount": "3.00",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        cookies=other["cookies"],
    )
    assert resp.status_code == 404, resp.text
    assert "Cross-workspace" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 2. Default-account management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_account_set_and_clear(client: AsyncClient):
    creds = await _register_and_login(client, "setclear")
    cookies = creds["cookies"]
    account_id = await _create_account(client, cookies)

    set_resp = await client.patch(
        "/v1/finance/settings",
        json={"default_spending_account_id": account_id},
        cookies=cookies,
    )
    assert set_resp.status_code == 200
    assert set_resp.json()["default_spending_account_id"] == account_id

    get_resp = await client.get("/v1/finance/settings", cookies=cookies)
    assert get_resp.status_code == 200
    assert get_resp.json()["default_spending_account_id"] == account_id

    clear_resp = await client.patch(
        "/v1/finance/settings",
        json={"default_spending_account_id": None},
        cookies=cookies,
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["default_spending_account_id"] is None


@pytest.mark.asyncio
async def test_default_account_rejects_brokerage_account(client: AsyncClient):
    creds = await _register_and_login(client, "brokerage")
    cookies = creds["cookies"]
    account_id = await _create_account(client, cookies, name="Brokerage", account_type="brokerage")

    resp = await client.patch(
        "/v1/finance/settings",
        json={"default_spending_account_id": account_id},
        cookies=cookies,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_deactivating_default_account_clears_it(client: AsyncClient):
    creds = await _register_and_login(client, "deactdef")
    cookies = creds["cookies"]
    account_id = await _create_account(client, cookies)

    set_resp = await client.patch(
        "/v1/finance/settings",
        json={"default_spending_account_id": account_id},
        cookies=cookies,
    )
    assert set_resp.status_code == 200

    deactivate = await client.patch(
        f"/v1/finance/accounts/{account_id}",
        json={"is_active": False},
        cookies=cookies,
    )
    assert deactivate.status_code == 200, deactivate.text

    get_resp = await client.get("/v1/finance/settings", cookies=cookies)
    assert get_resp.status_code == 200
    assert get_resp.json()["default_spending_account_id"] is None


# ---------------------------------------------------------------------------
# 3. Forward-only enforcement / "no account" filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unassigned_filter_returns_only_null_account_rows(client: AsyncClient):
    """
    Enforcement is forward-only (spec-050/054 precedent): a historical
    NULL-account row (simulated here via direct insert, since the create
    endpoint no longer allows one) must stay readable, editable, and must
    be the only row returned by the ``unassigned`` filter.
    """
    suffix = "unassigned"
    creds = await _register_and_login(client, suffix)
    cookies = creds["cookies"]
    category_id = await _first_category_id(client, cookies)
    account_id = await _create_account(client, cookies)

    with_account = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": category_id,
            "account_id": account_id,
            "amount": "5.00",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        cookies=cookies,
    )
    assert with_account.status_code == 201, with_account.text

    async with postgres.async_session_maker() as session:
        user = (
            await session.execute(select(User).where(User.username == f"defacct_{suffix}"))
        ).scalar_one()
        membership = (
            await session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
            )
        ).scalar_one()
        workspace_id = membership.workspace_id
        category = (
            (
                await session.execute(
                    select(SpendingCategory).where(SpendingCategory.workspace_id == workspace_id)
                )
            )
            .scalars()
            .first()
        )

        legacy_tx = SpendingTransaction(
            public_id=uuid.uuid4(),
            workspace_id=workspace_id,
            user_id=user.id,
            category_id=category.id,
            account_id=None,
            amount="10.00",
            type="expense",
            occurred_at=datetime.now(UTC),
            source_type="manual",
        )
        session.add(legacy_tx)
        await session.commit()
        legacy_public_id = str(legacy_tx.public_id)

    # Readable individually with its NULL account intact.
    detail_resp = await client.get(f"/v1/spending/transactions/{legacy_public_id}", cookies=cookies)
    assert detail_resp.status_code == 200
    assert detail_resp.json()["account_id"] is None

    # Editable without being forced to assign an account.
    patch_resp = await client.patch(
        f"/v1/spending/transactions/{legacy_public_id}",
        json={"description": "repaired description"},
        cookies=cookies,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["account_id"] is None

    # The "No account" filter returns exactly the NULL-account rows.
    unassigned_resp = await client.get(
        "/v1/spending/transactions",
        params={"unassigned": "true"},
        cookies=cookies,
    )
    assert unassigned_resp.status_code == 200
    unassigned_body = unassigned_resp.json()
    assert unassigned_body["total"] == 1
    assert unassigned_body["items"][0]["public_id"] == legacy_public_id

    # Repairing the NULL row by setting an account now works too.
    repair_resp = await client.patch(
        f"/v1/spending/transactions/{legacy_public_id}",
        json={"account_id": account_id},
        cookies=cookies,
    )
    assert repair_resp.status_code == 200
    assert repair_resp.json()["account_id"] == account_id


# ---------------------------------------------------------------------------
# 4. Import resolution order: row account_name -> import-level target
#    account -> workspace default -> row-level preview error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_row_missing_account_uses_import_target_account(client: AsyncClient):
    creds = await _register_and_login(client, "importtarget")
    cookies = creds["cookies"]
    category_id = await _first_category_id(client, cookies)
    account_id = await _create_account(client, cookies, name="Import Target")

    csv_content = (
        "occurred_at,type,amount,category,description,account_name\n"
        f"{datetime.now(UTC).isoformat()},expense,12.00,{category_id},no account column,\n"
    )
    files = {"file": ("tx.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "spending-transactions", "target_account_id": account_id},
        files=files,
        cookies=cookies,
    )
    assert validate.status_code == 200, validate.text
    assert validate.json()["error_summary"]["total_errors"] == 0
    import_id = validate.json()["import_batch"]["public_id"]

    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=cookies)
    assert commit.status_code == 200, commit.text

    txs = await client.get("/v1/spending/transactions", cookies=cookies)
    assert txs.status_code == 200
    assert txs.json()["items"][0]["account_id"] == account_id


@pytest.mark.asyncio
async def test_import_row_missing_account_uses_workspace_default(client: AsyncClient):
    creds = await _register_and_login(client, "importdefault")
    cookies = creds["cookies"]
    category_id = await _first_category_id(client, cookies)
    account_id = await _create_account(client, cookies, name="Workspace Default")

    settings_resp = await client.patch(
        "/v1/finance/settings",
        json={"default_spending_account_id": account_id},
        cookies=cookies,
    )
    assert settings_resp.status_code == 200, settings_resp.text

    csv_content = (
        "occurred_at,type,amount,category,description,account_name\n"
        f"{datetime.now(UTC).isoformat()},expense,12.00,{category_id},no account column,\n"
    )
    files = {"file": ("tx.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "spending-transactions"},
        files=files,
        cookies=cookies,
    )
    assert validate.status_code == 200, validate.text
    assert validate.json()["error_summary"]["total_errors"] == 0
    import_id = validate.json()["import_batch"]["public_id"]

    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=cookies)
    assert commit.status_code == 200, commit.text

    txs = await client.get("/v1/spending/transactions", cookies=cookies)
    assert txs.status_code == 200
    assert txs.json()["items"][0]["account_id"] == account_id


@pytest.mark.asyncio
async def test_import_row_missing_account_and_no_fallback_errors(client: AsyncClient):
    creds = await _register_and_login(client, "importnofallback")
    cookies = creds["cookies"]
    category_id = await _first_category_id(client, cookies)

    csv_content = (
        "occurred_at,type,amount,category,description,account_name\n"
        f"{datetime.now(UTC).isoformat()},expense,12.00,{category_id},no account anywhere,\n"
    )
    files = {"file": ("tx.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
    validate = await client.post(
        "/v1/imports",
        data={"module": "spending-transactions"},
        files=files,
        cookies=cookies,
    )
    assert validate.status_code == 200, validate.text
    payload = validate.json()
    assert payload["import_batch"]["status"] == "failed_validation"
    assert payload["error_summary"]["by_field"]["account_name"] == 1
    assert payload["errors"][0]["error_code"] == "required"


# ---------------------------------------------------------------------------
# 6. Recurring transactions (spec-084) — same resolver as manual creates
# ---------------------------------------------------------------------------


async def _recurring_payload(category_id: str, **overrides: object) -> dict:
    payload = {
        "category_id": category_id,
        "amount": "14.99",
        "type": "expense",
        "frequency": "monthly",
        "interval": 1,
        "anchor_date": datetime.now(UTC).date().isoformat(),
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_recurring_create_uses_explicit_account_id(client: AsyncClient):
    creds = await _register_and_login(client, "recurexplicit")
    cookies = creds["cookies"]
    category_id = await _first_category_id(client, cookies)
    account_id = await _create_account(client, cookies)

    resp = await client.post(
        "/v1/spending/recurring",
        json=await _recurring_payload(category_id, account_id=account_id),
        cookies=cookies,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["account_id"] == account_id


@pytest.mark.asyncio
async def test_recurring_create_falls_back_to_workspace_default_account(client: AsyncClient):
    creds = await _register_and_login(client, "recurfallback")
    cookies = creds["cookies"]
    category_id = await _first_category_id(client, cookies)
    account_id = await _create_account(client, cookies)

    settings_resp = await client.patch(
        "/v1/finance/settings",
        json={"default_spending_account_id": account_id},
        cookies=cookies,
    )
    assert settings_resp.status_code == 200, settings_resp.text

    resp = await client.post(
        "/v1/spending/recurring",
        json=await _recurring_payload(category_id),
        cookies=cookies,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["account_id"] == account_id


@pytest.mark.asyncio
async def test_recurring_create_rejected_when_no_account_and_no_default(client: AsyncClient):
    creds = await _register_and_login(client, "recurnoaccount")
    cookies = creds["cookies"]
    category_id = await _first_category_id(client, cookies)

    resp = await client.post(
        "/v1/spending/recurring",
        json=await _recurring_payload(category_id),
        cookies=cookies,
    )
    assert resp.status_code == 422, resp.text
    assert "account_id" in resp.json()["detail"] or "default" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_recurring_create_rejects_inactive_account_id(client: AsyncClient):
    creds = await _register_and_login(client, "recurinactive")
    cookies = creds["cookies"]
    category_id = await _first_category_id(client, cookies)
    account_id = await _create_account(client, cookies)

    deactivate = await client.patch(
        f"/v1/finance/accounts/{account_id}",
        json={"is_active": False},
        cookies=cookies,
    )
    assert deactivate.status_code == 200, deactivate.text

    resp = await client.post(
        "/v1/spending/recurring",
        json=await _recurring_payload(category_id, account_id=account_id),
        cookies=cookies,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_recurring_create_rejects_foreign_workspace_account_id(client: AsyncClient):
    owner = await _register_and_login(client, "recurownerws")
    other = await _register_and_login(client, "recurotherws")

    category_id = await _first_category_id(client, other["cookies"])
    foreign_account_id = await _create_account(client, owner["cookies"])

    resp = await client.post(
        "/v1/spending/recurring",
        json=await _recurring_payload(category_id, account_id=foreign_account_id),
        cookies=other["cookies"],
    )
    assert resp.status_code == 404, resp.text
    assert "Cross-workspace" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_recurring_update_can_change_account(client: AsyncClient):
    creds = await _register_and_login(client, "recurupdate")
    cookies = creds["cookies"]
    category_id = await _first_category_id(client, cookies)
    account_id = await _create_account(client, cookies, name="First")
    other_account_id = await _create_account(client, cookies, name="Second")

    create_resp = await client.post(
        "/v1/spending/recurring",
        json=await _recurring_payload(category_id, account_id=account_id),
        cookies=cookies,
    )
    assert create_resp.status_code == 201, create_resp.text
    recurring_id = create_resp.json()["public_id"]

    patch_resp = await client.patch(
        f"/v1/spending/recurring/{recurring_id}",
        json={"account_id": other_account_id},
        cookies=cookies,
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["account_id"] == other_account_id


@pytest.mark.asyncio
async def test_recurring_update_explicit_null_account_is_noop_for_legacy_rule(
    client: AsyncClient,
):
    """A legacy (pre-spec-084) recurring rule with account_id=NULL must stay
    editable even if the client explicitly sends account_id: null — nothing
    is actually being cleared, so this must not 422."""
    suffix = "reculegacynull"
    creds = await _register_and_login(client, suffix)
    cookies = creds["cookies"]
    category_id = await _first_category_id(client, cookies)

    async with postgres.async_session_maker() as session:
        user = (
            await session.execute(select(User).where(User.username == f"defacct_{suffix}"))
        ).scalar_one()
        membership = (
            await session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
            )
        ).scalar_one()
        workspace_id = membership.workspace_id
        category = (
            await session.execute(
                select(SpendingCategory).where(
                    SpendingCategory.workspace_id == workspace_id,
                    SpendingCategory.public_id == uuid.UUID(category_id),
                )
            )
        ).scalar_one()
        legacy = RecurringTransaction(
            public_id=uuid.uuid4(),
            workspace_id=workspace_id,
            user_id=user.id,
            category_id=category.id,
            account_id=None,
            amount=Decimal("10.00"),
            type="expense",
            frequency="monthly",
            interval=1,
            anchor_date=datetime.now(UTC).date(),
            next_due_date=datetime.now(UTC).date(),
            is_active=True,
        )
        session.add(legacy)
        await session.commit()
        legacy_public_id = str(legacy.public_id)

    resp = await client.patch(
        f"/v1/spending/recurring/{legacy_public_id}",
        json={"account_id": None, "amount": "12.00"},
        cookies=cookies,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["account_id"] is None
    assert resp.json()["amount"] == "12.00"
