import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.auth.models import User
from app.core.database import postgres
from app.finance.models import Account, CapitalTransfer, FxRate, TransferModule
from app.spending.models import SpendingCategory, SpendingTransaction, TransactionType


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
        password="TestPass123!",
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

    delete_res = await client.delete(f"/v1/finance/accounts/{account['public_id']}")
    assert delete_res.status_code == 204

    list_after_delete = await client.get("/v1/finance/accounts")
    assert list_after_delete.status_code == 200
    assert list_after_delete.json()["total"] == 0


@pytest.mark.asyncio
async def test_finance_account_validation_and_workspace_isolation(client: AsyncClient):
    await _register_and_login(
        client,
        email="finance-iso-a@example.com",
        username="finance-iso-a",
        password="TestPass123!",
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
        password="TestPass123!",
    )

    list_res = await client.get("/v1/finance/accounts")
    assert list_res.status_code == 200
    assert list_res.json()["items"] == []

    patch_res = await client.patch(
        f"/v1/finance/accounts/{account_id}",
        json={"name": "Should Not Work"},
    )
    assert patch_res.status_code == 404

    delete_res = await client.delete(f"/v1/finance/accounts/{account_id}")
    assert delete_res.status_code == 404


@pytest.mark.asyncio
async def test_finance_user_override_currency_validation(client: AsyncClient):
    await _register_and_login(
        client,
        email="finance-user-override-validation@example.com",
        username="finance-user-override-validation",
        password="TestPass123!",
    )

    update_user_setting = await client.patch(
        "/v1/finance/settings/user",
        json={"reporting_currency_override_code": "EUR"},
    )
    assert update_user_setting.status_code == 422
    assert update_user_setting.json()["code"] == "validation_error"


@pytest.mark.asyncio
async def test_finance_account_delete_rejected_when_in_use(client: AsyncClient):
    await _register_and_login(
        client,
        email="finance-delete-guard@example.com",
        username="finance-delete-guard",
        password="TestPass123!",
    )

    category_res = await client.get("/v1/spending/categories")
    assert category_res.status_code == 200
    category_id = category_res.json()["items"][0]["public_id"]

    account_res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "Wallet In Use",
            "account_type": "wallet",
            "default_currency_code": "USD",
        },
    )
    assert account_res.status_code == 201
    account_id = account_res.json()["public_id"]

    tx_res = await client.post(
        "/v1/spending/transactions",
        json={
            "amount": "12.00",
            "type": "expense",
            "category_id": category_id,
            "account_id": account_id,
            "occurred_at": datetime.now(UTC).isoformat(),
            "description": "coffee",
        },
    )
    assert tx_res.status_code == 201

    delete_res = await client.delete(f"/v1/finance/accounts/{account_id}")
    assert delete_res.status_code == 409
    assert "cannot be deleted" in delete_res.json()["detail"]


@pytest.mark.asyncio
async def test_finance_settings_fx_and_transfers_flow(client: AsyncClient):
    await _register_and_login(
        client,
        email="finance-transfer@example.com",
        username="finance-transfer",
        password="TestPass123!",
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
        json={
            "reporting_currency_code": "USD",
            "currency_display_preference": "code",
        },
    )
    assert setting_res.status_code == 200
    assert setting_res.json()["reporting_currency_code"] == "USD"
    assert setting_res.json()["currency_display_preference"] == "code"

    user_setting_res = await client.get("/v1/finance/settings/user")
    assert user_setting_res.status_code == 200
    user_setting_body = user_setting_res.json()
    assert user_setting_body["workspace_reporting_currency_code"] == "USD"
    assert user_setting_body["workspace_currency_display_preference"] == "code"
    assert user_setting_body["effective_reporting_currency_code"] == "USD"
    assert user_setting_body["effective_currency_display_preference"] == "code"
    assert user_setting_body["reporting_currency_override_code"] is None
    assert user_setting_body["currency_display_preference_override"] is None

    update_user_setting = await client.patch(
        "/v1/finance/settings/user",
        json={
            "reporting_currency_override_code": "INR",
            "currency_display_preference_override": "symbol",
        },
    )
    assert update_user_setting.status_code == 200
    updated_user_setting_body = update_user_setting.json()
    assert updated_user_setting_body["reporting_currency_override_code"] == "INR"
    assert updated_user_setting_body["effective_reporting_currency_code"] == "INR"
    assert updated_user_setting_body["currency_display_preference_override"] == "symbol"
    assert updated_user_setting_body["effective_currency_display_preference"] == "symbol"

    clear_user_override = await client.patch(
        "/v1/finance/settings/user",
        json={
            "reporting_currency_override_code": None,
            "currency_display_preference_override": None,
        },
    )
    assert clear_user_override.status_code == 200
    cleared_body = clear_user_override.json()
    assert cleared_body["reporting_currency_override_code"] is None
    assert cleared_body["currency_display_preference_override"] is None
    assert cleared_body["effective_reporting_currency_code"] == "USD"
    assert cleared_body["effective_currency_display_preference"] == "code"

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
    transfer_body = transfer_res.json()
    transfer_id = transfer_body["public_id"]
    assert transfer_body["from_account_public_id"] == from_account.json()["public_id"]
    assert transfer_body["to_account_public_id"] == to_account.json()["public_id"]
    assert transfer_body["from_account_name"] == "Budget Bank"
    assert transfer_body["to_account_name"] == "Global Brokerage"
    assert transfer_body["from_account_type"] == "bank"
    assert transfer_body["to_account_type"] == "brokerage"

    list_transfers = await client.get("/v1/finance/transfers")
    assert list_transfers.status_code == 200
    list_body = list_transfers.json()
    assert list_body["total"] == 1
    assert list_body["items"][0]["from_account_name"] == "Budget Bank"
    assert list_body["items"][0]["to_account_name"] == "Global Brokerage"

    get_transfer = await client.get(f"/v1/finance/transfers/{transfer_id}")
    assert get_transfer.status_code == 200
    get_body = get_transfer.json()
    assert get_body["net_amount_received"] == "792.00"
    assert get_body["from_account_public_id"] == from_account.json()["public_id"]
    assert get_body["to_account_public_id"] == to_account.json()["public_id"]


@pytest.mark.asyncio
async def test_fx_rate_same_currency_check_constraint(override_database_url):
    # 1. Insert matching currency with rate != 1.0: should fail due to DB constraint
    async with postgres.async_session_maker() as session:
        bad_rate = FxRate(
            base_currency_code="USD",
            quote_currency_code="USD",
            rate=1.05,
            as_of=datetime.now(UTC),
            fetched_at=datetime.now(UTC),
            source="test-violator",
        )
        session.add(bad_rate)
        with pytest.raises(DBAPIError) as exc:
            await session.commit()
        assert "ck_fx_rates_same_currency_rate" in str(exc.value)
        await session.rollback()

    # 2. Insert matching currency with rate == 1.0: should succeed
    async with postgres.async_session_maker() as session:
        good_rate = FxRate(
            base_currency_code="USD",
            quote_currency_code="USD",
            rate=1.0,
            as_of=datetime.now(UTC),
            fetched_at=datetime.now(UTC),
            source="test-good",
        )
        session.add(good_rate)
        await session.commit()

        # Clean up
        await session.delete(good_rate)
        await session.commit()


@pytest.mark.asyncio
async def test_transfer_arithmetic_validation_failure(client: AsyncClient):
    await _register_and_login(
        client,
        email="finance-transfer-err@example.com",
        username="finance-transfer-err",
        password="TestPass123!",
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

    # Inconsistent arithmetic: gross=1000, rate=0.8 (converted=800), fees=8.0, but net=700 (should be 792)
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
            "net_amount_received": "700.00",
            "occurred_at": datetime.now(UTC).isoformat(),
            "notes": "Invalid transfer",
        },
    )
    assert transfer_res.status_code == 422
    assert "Transfer arithmetic inconsistent" in transfer_res.json()["detail"]


@pytest.mark.asyncio
async def test_spending_transaction_account_fk_is_tenant_safe(client: AsyncClient):
    await _register_and_login(
        client,
        email="finance-db-safe-a@example.com",
        username="finance-db-safe-a",
        password="TestPass123!",
    )
    account_res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "A Wallet",
            "account_type": "wallet",
            "default_currency_code": "USD",
        },
    )
    assert account_res.status_code == 201
    account_public_id = uuid.UUID(account_res.json()["public_id"])

    await client.post("/v1/auth/logout")
    await _register_and_login(
        client,
        email="finance-db-safe-b@example.com",
        username="finance-db-safe-b",
        password="TestPass123!",
    )
    categories_res = await client.get("/v1/spending/categories")
    assert categories_res.status_code == 200
    b_category_public_id = uuid.UUID(categories_res.json()["items"][0]["public_id"])

    async with postgres.async_session_maker() as session:
        a_account = (
            await session.execute(select(Account).where(Account.public_id == account_public_id))
        ).scalar_one()
        b_category = (
            await session.execute(
                select(SpendingCategory).where(SpendingCategory.public_id == b_category_public_id)
            )
        ).scalar_one()
        b_user = (
            await session.execute(select(User).where(User.username == "finance-db-safe-b"))
        ).scalar_one()

        session.add(
            SpendingTransaction(
                workspace_id=b_category.workspace_id,
                user_id=b_user.id,
                category_id=b_category.id,
                account_id=a_account.id,
                amount=Decimal("12.00"),
                type=TransactionType.expense,
                occurred_at=datetime.now(UTC),
                description="direct cross-workspace account link",
            )
        )

        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_capital_transfer_account_fks_are_tenant_safe(client: AsyncClient):
    await _register_and_login(
        client,
        email="finance-transfer-db-safe-a@example.com",
        username="finance-transfer-db-safe-a",
        password="TestPass123!",
    )
    a_from_account_res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "A Bank",
            "account_type": "bank",
            "default_currency_code": "USD",
        },
    )
    assert a_from_account_res.status_code == 201
    a_from_account_public_id = uuid.UUID(a_from_account_res.json()["public_id"])

    await client.post("/v1/auth/logout")
    await _register_and_login(
        client,
        email="finance-transfer-db-safe-b@example.com",
        username="finance-transfer-db-safe-b",
        password="TestPass123!",
    )
    b_to_account_res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "B Brokerage",
            "account_type": "brokerage",
            "default_currency_code": "GBP",
        },
    )
    assert b_to_account_res.status_code == 201
    b_to_account_public_id = uuid.UUID(b_to_account_res.json()["public_id"])

    async with postgres.async_session_maker() as session:
        a_from_account = (
            await session.execute(
                select(Account).where(Account.public_id == a_from_account_public_id)
            )
        ).scalar_one()
        b_to_account = (
            await session.execute(
                select(Account).where(Account.public_id == b_to_account_public_id)
            )
        ).scalar_one()
        a_actor = (
            await session.execute(select(User).where(User.username == "finance-transfer-db-safe-a"))
        ).scalar_one()

        session.add(
            CapitalTransfer(
                workspace_id=a_from_account.workspace_id,
                actor_id=a_actor.id,
                from_module=TransferModule.spending,
                to_module=TransferModule.investing,
                from_account_id=a_from_account.id,
                to_account_id=b_to_account.id,
                from_currency_code="USD",
                to_currency_code="GBP",
                gross_amount=Decimal("1000.00"),
                fx_rate_used=Decimal("0.8000000000"),
                fx_fee_amount=Decimal("5.00"),
                platform_fee_amount=Decimal("2.00"),
                tax_amount=Decimal("1.00"),
                net_amount_received=Decimal("792.00"),
                occurred_at=datetime.now(UTC),
                notes="direct cross-workspace account link",
            )
        )

        with pytest.raises(IntegrityError):
            await session.commit()
