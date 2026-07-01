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

    async with postgres.async_session_maker() as session:
        stored_transfer = (
            await session.execute(
                select(CapitalTransfer).where(CapitalTransfer.public_id == uuid.UUID(transfer_id))
            )
        ).scalar_one()
        assert isinstance(stored_transfer.gross_amount, Decimal)
        assert stored_transfer.gross_amount == Decimal("1000.00")
        assert isinstance(stored_transfer.fx_rate_used, Decimal)
        assert stored_transfer.fx_rate_used == Decimal("0.8000000000")
        assert isinstance(stored_transfer.net_amount_received, Decimal)
        assert stored_transfer.net_amount_received == Decimal("792.00")


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
        await session.rollback()


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
        await session.rollback()


@pytest.mark.asyncio
async def test_transfer_same_currency_fx_rate_enforced(client: AsyncClient):
    await _register_and_login(
        client,
        email="same-curr-transfer@example.com",
        username="samecurrtransfer",
        password="TestPass123!",
    )
    from_account = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "USD Wallet 1",
            "account_type": "wallet",
            "default_currency_code": "USD",
        },
    )
    assert from_account.status_code == 201
    to_account = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "USD Wallet 2",
            "account_type": "wallet",
            "default_currency_code": "USD",
        },
    )
    assert to_account.status_code == 201

    # Same currency USD -> USD, but fx_rate_used = 1.05: should fail validation
    res = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": "spending",
            "to_module": "spending",
            "from_account_id": from_account.json()["public_id"],
            "to_account_id": to_account.json()["public_id"],
            "from_currency_code": "USD",
            "to_currency_code": "USD",
            "gross_amount": "100.00",
            "fx_rate_used": "1.0500000000",
            "fx_fee_amount": "0.00",
            "platform_fee_amount": "0.00",
            "tax_amount": "0.00",
            "net_amount_received": "105.00",
            "occurred_at": datetime.now(UTC).isoformat(),
            "notes": "Same currency transfer invalid rate",
        },
    )
    assert res.status_code == 422
    assert "FX rate must be 1.0 when transferring between the same currency" in res.json()["detail"]

    # Same currency USD -> USD, fx_rate_used = 1.0: should succeed
    res_ok = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": "spending",
            "to_module": "spending",
            "from_account_id": from_account.json()["public_id"],
            "to_account_id": to_account.json()["public_id"],
            "from_currency_code": "USD",
            "to_currency_code": "USD",
            "gross_amount": "100.00",
            "fx_rate_used": "1.0000000000",
            "fx_fee_amount": "0.00",
            "platform_fee_amount": "0.00",
            "tax_amount": "0.00",
            "net_amount_received": "100.00",
            "occurred_at": datetime.now(UTC).isoformat(),
            "notes": "Same currency transfer valid rate",
        },
    )
    assert res_ok.status_code == 201


@pytest.mark.asyncio
async def test_finance_account_delete_rejected_when_in_use_by_investing_cash(client: AsyncClient):
    await _register_and_login(
        client,
        email="finance-delete-investing@example.com",
        username="finance-delete-investing",
        password="TestPass123!",
    )

    account_res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "Wallet Cash Use",
            "account_type": "wallet",
            "default_currency_code": "USD",
        },
    )
    assert account_res.status_code == 201
    account_id = account_res.json()["public_id"]

    # Create a cash balance referencing the account
    cash_res = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_id,
            "balance": "250.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    assert cash_res.status_code == 201

    # Attempt to delete the account
    delete_res = await client.delete(f"/v1/finance/accounts/{account_id}")
    assert delete_res.status_code == 409
    assert "cannot be deleted" in delete_res.json()["detail"]


async def _create_bank_and_brokerage(client: AsyncClient, *, suffix: str = "") -> tuple[str, str]:
    bank = await client.post(
        "/v1/finance/accounts",
        json={"name": f"Bank{suffix}", "account_type": "bank", "default_currency_code": "USD"},
    )
    assert bank.status_code == 201
    broker = await client.post(
        "/v1/finance/accounts",
        json={
            "name": f"Broker{suffix}",
            "account_type": "brokerage",
            "default_currency_code": "USD",
        },
    )
    assert broker.status_code == 201
    return bank.json()["public_id"], broker.json()["public_id"]


async def _create_investing_transfer(
    client: AsyncClient, *, from_id: str, to_id: str, amount: str = "1000.00"
) -> dict:
    res = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": "spending",
            "to_module": "investing",
            "from_account_id": from_id,
            "to_account_id": to_id,
            "from_currency_code": "USD",
            "to_currency_code": "USD",
            "gross_amount": amount,
            "net_amount_received": amount,
            "occurred_at": datetime.now(UTC).isoformat(),
            "notes": "seed",
        },
    )
    assert res.status_code == 201
    return res.json()


@pytest.mark.asyncio
async def test_delete_transfer_removes_transfer_and_cash_balance(client: AsyncClient):
    await _register_and_login(
        client,
        email="delete-transfer@example.com",
        username="delete-transfer",
        password="TestPass123!",
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix="-del")
    transfer = await _create_investing_transfer(client, from_id=bank_id, to_id=broker_id)
    transfer_id = transfer["public_id"]

    # Verify cash balance was created
    cb_res = await client.get("/v1/investing/cash-balances")
    assert cb_res.status_code == 200
    assert cb_res.json()["total"] == 1
    assert cb_res.json()["items"][0]["balance"] == "1000.00"

    delete_res = await client.delete(f"/v1/finance/transfers/{transfer_id}")
    assert delete_res.status_code == 204

    # Transfer should be gone
    get_res = await client.get(f"/v1/finance/transfers/{transfer_id}")
    assert get_res.status_code == 404

    # Cash balance snapshot should also be gone
    cb_after = await client.get("/v1/investing/cash-balances")
    assert cb_after.status_code == 200
    assert cb_after.json()["total"] == 0


@pytest.mark.asyncio
async def test_delete_transfer_blocked_by_newer_cash_balance(client: AsyncClient):
    await _register_and_login(
        client,
        email="delete-transfer-blocked@example.com",
        username="delete-transfer-blocked",
        password="TestPass123!",
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix="-blk")
    transfer = await _create_investing_transfer(client, from_id=bank_id, to_id=broker_id)
    transfer_id = transfer["public_id"]

    # Manually add another cash balance for the same account/currency (simulates order snapshot)
    extra_cb = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": broker_id,
            "balance": "900.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    assert extra_cb.status_code == 201

    delete_res = await client.delete(f"/v1/finance/transfers/{transfer_id}")
    assert delete_res.status_code == 409
    detail = delete_res.json()["detail"]
    assert "newer balance snapshot" in detail
    assert "Delete those order imports first" in detail


@pytest.mark.asyncio
async def test_delete_transfer_not_found(client: AsyncClient):
    await _register_and_login(
        client,
        email="delete-transfer-404@example.com",
        username="delete-transfer-404",
        password="TestPass123!",
    )
    res = await client.delete(f"/v1/finance/transfers/{uuid.uuid4()}")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_patch_transfer_notes_only_no_balance_change(client: AsyncClient):
    await _register_and_login(
        client,
        email="patch-transfer-notes@example.com",
        username="patch-transfer-notes",
        password="TestPass123!",
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix="-notes")
    transfer = await _create_investing_transfer(client, from_id=bank_id, to_id=broker_id)
    transfer_id = transfer["public_id"]

    patch_res = await client.patch(
        f"/v1/finance/transfers/{transfer_id}",
        json={"notes": "corrected note"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["notes"] == "corrected note"
    assert patch_res.json()["net_amount_received"] == "1000.00"

    # Cash balance should be unchanged
    cb_res = await client.get("/v1/investing/cash-balances")
    assert cb_res.json()["items"][0]["balance"] == "1000.00"


@pytest.mark.asyncio
async def test_patch_transfer_amount_updates_cash_balance(client: AsyncClient):
    await _register_and_login(
        client,
        email="patch-transfer-amount@example.com",
        username="patch-transfer-amount",
        password="TestPass123!",
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix="-amt")
    transfer = await _create_investing_transfer(
        client, from_id=bank_id, to_id=broker_id, amount="500.00"
    )
    transfer_id = transfer["public_id"]

    patch_res = await client.patch(
        f"/v1/finance/transfers/{transfer_id}",
        json={"gross_amount": "800.00", "net_amount_received": "800.00"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["net_amount_received"] == "800.00"

    cb_res = await client.get("/v1/investing/cash-balances")
    balances = cb_res.json()["items"]
    assert any(b["balance"] == "800.00" for b in balances)


@pytest.mark.asyncio
async def test_patch_transfer_to_account_moves_cash_balance(client: AsyncClient):
    await _register_and_login(
        client,
        email="patch-transfer-acct@example.com",
        username="patch-transfer-acct",
        password="TestPass123!",
    )
    bank_id, broker1_id = await _create_bank_and_brokerage(client, suffix="-acct1")
    broker2 = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "Broker2-acct",
            "account_type": "brokerage",
            "default_currency_code": "USD",
        },
    )
    assert broker2.status_code == 201
    broker2_id = broker2.json()["public_id"]

    transfer = await _create_investing_transfer(client, from_id=bank_id, to_id=broker1_id)
    transfer_id = transfer["public_id"]

    patch_res = await client.patch(
        f"/v1/finance/transfers/{transfer_id}",
        json={"to_account_id": broker2_id},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["to_account_public_id"] == broker2_id

    # Cash balance should now be on broker2, not broker1
    cb_res = await client.get("/v1/investing/cash-balances")
    balances = {b["account_name"]: b["balance"] for b in cb_res.json()["items"]}
    assert balances.get("Broker2-acct") == "1000.00"
    assert "Broker1-acct1" not in balances


@pytest.mark.asyncio
async def test_patch_transfer_blocked_by_newer_cash_balance(client: AsyncClient):
    await _register_and_login(
        client,
        email="patch-transfer-blk@example.com",
        username="patch-transfer-blk",
        password="TestPass123!",
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix="-pblk")
    transfer = await _create_investing_transfer(client, from_id=bank_id, to_id=broker_id)
    transfer_id = transfer["public_id"]

    # Add a newer snapshot on the same account to simulate a committed order
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": broker_id,
            "balance": "900.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )

    patch_res = await client.patch(
        f"/v1/finance/transfers/{transfer_id}",
        json={"net_amount_received": "1200.00", "gross_amount": "1200.00"},
    )
    assert patch_res.status_code == 409
    detail = patch_res.json()["detail"]
    assert "newer balance snapshot" in detail
    assert "Delete those order imports first" in detail


# ---------------------------------------------------------------------------
# spec-049: transfers OUT of a brokerage account (from_module == "investing")
# must also decrement the source account's cash-balance snapshot.
# ---------------------------------------------------------------------------


async def _create_outflow_transfer(
    client: AsyncClient, *, from_id: str, to_id: str, amount: str = "1000.00"
) -> dict:
    res = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": "investing",
            "to_module": "spending",
            "from_account_id": from_id,
            "to_account_id": to_id,
            "from_currency_code": "USD",
            "to_currency_code": "USD",
            "gross_amount": amount,
            "net_amount_received": amount,
            "occurred_at": datetime.now(UTC).isoformat(),
            "notes": "outflow",
        },
    )
    assert res.status_code == 201
    return res.json()


@pytest.mark.asyncio
async def test_create_outflow_transfer_decrements_brokerage_cash(client: AsyncClient):
    await _register_and_login(
        client,
        email="outflow-create@example.com",
        username="outflow-create",
        password="TestPass123!",
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix="-out")

    # Seed the brokerage with 1000 via a to-investing transfer first.
    await _create_investing_transfer(client, from_id=bank_id, to_id=broker_id, amount="1000.00")

    # Now transfer 300 OUT of the brokerage back to the bank.
    await _create_outflow_transfer(client, from_id=broker_id, to_id=bank_id, amount="300.00")

    cb_res = await client.get("/v1/investing/cash-balances")
    assert cb_res.status_code == 200
    balances = cb_res.json()["items"]
    # Two snapshots now exist for the brokerage account: 1000 (inflow), then
    # 700 (inflow - outflow). The latest is what matters for the current balance.
    assert any(b["balance"] == "700.00" for b in balances)


@pytest.mark.asyncio
async def test_delete_outflow_transfer_removes_from_side_cash_balance(client: AsyncClient):
    await _register_and_login(
        client,
        email="outflow-delete@example.com",
        username="outflow-delete",
        password="TestPass123!",
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix="-outdel")
    await _create_investing_transfer(client, from_id=bank_id, to_id=broker_id, amount="1000.00")
    outflow = await _create_outflow_transfer(
        client, from_id=broker_id, to_id=bank_id, amount="300.00"
    )

    cb_before = await client.get("/v1/investing/cash-balances")
    assert cb_before.json()["total"] == 2  # inflow snapshot + outflow snapshot

    delete_res = await client.delete(f"/v1/finance/transfers/{outflow['public_id']}")
    assert delete_res.status_code == 204

    cb_after = await client.get("/v1/investing/cash-balances")
    assert cb_after.json()["total"] == 1
    assert cb_after.json()["items"][0]["balance"] == "1000.00"


@pytest.mark.asyncio
async def test_delete_outflow_transfer_blocked_by_newer_cash_balance(client: AsyncClient):
    await _register_and_login(
        client,
        email="outflow-delete-blk@example.com",
        username="outflow-delete-blk",
        password="TestPass123!",
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix="-outdelblk")
    await _create_investing_transfer(client, from_id=bank_id, to_id=broker_id, amount="1000.00")
    outflow = await _create_outflow_transfer(
        client, from_id=broker_id, to_id=bank_id, amount="300.00"
    )

    # Simulate a later order snapshot on the brokerage account.
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": broker_id,
            "balance": "650.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )

    delete_res = await client.delete(f"/v1/finance/transfers/{outflow['public_id']}")
    assert delete_res.status_code == 409
    detail = delete_res.json()["detail"]
    assert "newer balance snapshot" in detail
    assert "Delete those order imports first" in detail

    # Nothing should have been deleted -- transfer and both snapshots survive.
    get_res = await client.get(f"/v1/finance/transfers/{outflow['public_id']}")
    assert get_res.status_code == 200
    cb_res = await client.get("/v1/investing/cash-balances")
    assert cb_res.json()["total"] == 3


@pytest.mark.asyncio
async def test_patch_outflow_transfer_amount_updates_from_side_cash_balance(client: AsyncClient):
    await _register_and_login(
        client,
        email="outflow-patch@example.com",
        username="outflow-patch",
        password="TestPass123!",
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix="-outpatch")
    await _create_investing_transfer(client, from_id=bank_id, to_id=broker_id, amount="1000.00")
    outflow = await _create_outflow_transfer(
        client, from_id=broker_id, to_id=bank_id, amount="300.00"
    )

    patch_res = await client.patch(
        f"/v1/finance/transfers/{outflow['public_id']}",
        json={"gross_amount": "500.00", "net_amount_received": "500.00"},
    )
    assert patch_res.status_code == 200

    cb_res = await client.get("/v1/investing/cash-balances")
    balances = cb_res.json()["items"]
    # In-place delta adjustment: 700 (1000 - 300) -> 500 (1000 - 500)
    assert any(b["balance"] == "500.00" for b in balances)
    assert not any(b["balance"] == "700.00" for b in balances)


@pytest.mark.asyncio
async def test_investing_to_investing_transfer_writes_two_snapshots(client: AsyncClient):
    """An investing-to-investing transfer triggers both the to-side and
    from-side branches, producing two CashBalance rows that share
    trigger_ref=transfer.public_id -- exercises get_by_trigger_ref_and_account
    disambiguation rather than the old single-row get_by_trigger_ref."""
    await _register_and_login(
        client,
        email="inv-to-inv@example.com",
        username="inv-to-inv",
        password="TestPass123!",
    )
    broker_a = await client.post(
        "/v1/finance/accounts",
        json={"name": "BrokerA", "account_type": "brokerage", "default_currency_code": "USD"},
    )
    broker_b = await client.post(
        "/v1/finance/accounts",
        json={"name": "BrokerB", "account_type": "brokerage", "default_currency_code": "USD"},
    )
    assert broker_a.status_code == 201 and broker_b.status_code == 201
    a_id, b_id = broker_a.json()["public_id"], broker_b.json()["public_id"]

    # Seed BrokerA with 1000 first.
    bank_id, _ = await _create_bank_and_brokerage(client, suffix="-i2ifund")
    await _create_investing_transfer(client, from_id=bank_id, to_id=a_id, amount="1000.00")

    transfer = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": "investing",
            "to_module": "investing",
            "from_account_id": a_id,
            "to_account_id": b_id,
            "from_currency_code": "USD",
            "to_currency_code": "USD",
            "gross_amount": "400.00",
            "net_amount_received": "400.00",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
    )
    assert transfer.status_code == 201
    transfer_id = transfer.json()["public_id"]

    cb_res = await client.get("/v1/investing/cash-balances")
    # Results are ordered newest-first; BrokerA has two rows (1000 seed, then
    # 600 from this transfer), so build the dict from oldest-first so the
    # latest snapshot per account wins on duplicate keys.
    balances = {b["account_name"]: b["balance"] for b in reversed(cb_res.json()["items"])}
    assert balances.get("BrokerA") == "600.00"  # 1000 - 400
    assert balances.get("BrokerB") == "400.00"

    # Deleting must clean up BOTH snapshots (previously only get_by_trigger_ref
    # was used, which would have raised MultipleResultsFound here).
    delete_res = await client.delete(f"/v1/finance/transfers/{transfer_id}")
    assert delete_res.status_code == 204

    cb_after = await client.get("/v1/investing/cash-balances")
    balances_after = {b["account_name"]: b["balance"] for b in cb_after.json()["items"]}
    assert balances_after.get("BrokerA") == "1000.00"
    assert "BrokerB" not in balances_after


@pytest.mark.asyncio
async def test_legacy_outflow_transfer_without_snapshot_is_unaffected(client: AsyncClient):
    """A transfer created before this fix (or any from_module='spending'
    transfer) has no from-side snapshot at all -- delete/update must treat
    that side as unmanaged (no-op), not error."""
    await _register_and_login(
        client,
        email="legacy-outflow@example.com",
        username="legacy-outflow",
        password="TestPass123!",
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix="-legacy")
    # A spending-to-spending transfer: neither side is ever snapshot-managed.
    transfer = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": "spending",
            "to_module": "spending",
            "from_account_id": bank_id,
            "to_account_id": bank_id,
            "from_currency_code": "USD",
            "to_currency_code": "USD",
            "gross_amount": "50.00",
            "net_amount_received": "50.00",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
    )
    assert transfer.status_code == 201
    transfer_id = transfer.json()["public_id"]

    patch_res = await client.patch(
        f"/v1/finance/transfers/{transfer_id}",
        json={"gross_amount": "75.00", "net_amount_received": "75.00"},
    )
    assert patch_res.status_code == 200

    delete_res = await client.delete(f"/v1/finance/transfers/{transfer_id}")
    assert delete_res.status_code == 204
