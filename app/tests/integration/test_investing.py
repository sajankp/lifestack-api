import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import postgres
from app.finance.models import Account, Currency, FxRate, WorkspaceCurrency
from app.investing.models import CashBalance, Holding, Instrument, PortfolioSnapshot


async def _register_and_login(
    client: AsyncClient, *, email: str, username: str, password: str
) -> dict[str, str]:
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

    account_map = {}
    for account_name in [
        "brokerage",
        "wallet",
        "usd-wallet",
        "gbp-wallet",
        "eur-wallet",
        "primary",
    ]:
        res = await client.post(
            "/v1/finance/accounts",
            json={
                "name": account_name,
                "account_type": "brokerage",
                "default_currency_code": "USD",
            },
        )
        assert res.status_code == 201
        account_map[account_name] = res.json()["public_id"]
    return account_map


@pytest.mark.asyncio
async def test_investing_crud_summary_and_audit(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-e2e@example.com",
        username="investing-e2e",
        password="TestPass123!",
    )

    # Create holding
    create_holding = {
        "symbol": "AAPL",
        "account_id": account_map["brokerage"],
        "quantity": "10.50000000",
        "avg_cost": "150.25",
        "currency": "usd",
    }
    holding_res = await client.post("/v1/investing/holdings", json=create_holding)
    assert holding_res.status_code == 201
    holding = holding_res.json()
    holding_id = holding["public_id"]
    assert holding["symbol"] == "AAPL"
    assert holding["quantity"] == "10.50000000"
    assert holding["avg_cost"] == "150.25"
    assert holding["currency"] == "USD"
    assert holding["account_id"] == account_map["brokerage"]

    # Update holding
    update_res = await client.patch(
        f"/v1/investing/holdings/{holding_id}",
        json={"quantity": "12.00000000", "avg_cost": "140.00"},
    )
    assert update_res.status_code == 200
    updated = update_res.json()
    assert updated["quantity"] == "12.00000000"
    assert updated["avg_cost"] == "140.00"

    # Create cash balance
    create_cash = {
        "account_id": account_map["brokerage"],
        "balance": "1000.00",
        "currency": "usd",
        "as_of": datetime.now(UTC).isoformat(),
    }
    cash_res = await client.post("/v1/investing/cash-balances", json=create_cash)
    assert cash_res.status_code == 201
    cash = cash_res.json()
    assert cash["balance"] == "1000.00"
    assert cash["currency"] == "USD"
    assert cash["account_id"] == account_map["brokerage"]

    # Verify summary
    summary_res = await client.get("/v1/investing/summary")
    assert summary_res.status_code == 200
    summary = summary_res.json()
    assert summary["holdings_count"] == 1
    assert summary["portfolio_value"] == "1680.0000000000"
    assert summary["cash_total"] == "1000.00"
    assert summary["currency_breakdown"]["USD"] == "2680.0000000000"
    assert summary["daily_change"] is None
    assert summary["reporting_currency"] == "USD"
    assert summary["valuation_status"] == "single_currency_native"

    async with postgres.async_session_maker() as session:
        db_holding = (
            (
                await session.execute(
                    select(Holding).where(Holding.public_id == uuid.UUID(holding_id))
                )
            )
            .scalars()
            .one()
        )
        db_cash = (
            (
                await session.execute(
                    select(CashBalance).where(CashBalance.public_id == uuid.UUID(cash["public_id"]))
                )
            )
            .scalars()
            .one()
        )
        assert db_holding.quantity == Decimal("12.00000000")
        assert db_cash.balance == Decimal("1000.00")

        # Verify account matches by DB ID
        linked_account = (
            await session.execute(
                select(Account).where(Account.public_id == uuid.UUID(account_map["brokerage"]))
            )
        ).scalar_one()
        assert db_holding.account_id == linked_account.id
        assert db_cash.account_id == linked_account.id

        audits = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_type == "holding")
                    .where(AuditLog.entity_id == db_holding.id)
                    .order_by(AuditLog.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 2
        assert audits[0].module == "investing"
        assert audits[0].action == "create"
        assert audits[1].action == "update"

    # Delete holding
    delete_res = await client.delete(f"/v1/investing/holdings/{holding_id}")
    assert delete_res.status_code == 204


@pytest.mark.asyncio
async def test_investing_price_submission_rejects_unrealistic_price(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-price-bound@example.com",
        username="investing-price-bound",
        password="TestPass123!",
    )

    holding_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "AAPL",
            "account_id": account_map["brokerage"],
            "quantity": "1.00000000",
            "avg_cost": "100.00",
            "currency": "USD",
        },
    )
    assert holding_res.status_code == 201

    price_res = await client.post(
        "/v1/investing/prices",
        json={
            "price_date": datetime.now(UTC).date().isoformat(),
            "prices": [
                {
                    "holding_public_id": holding_res.json()["public_id"],
                    "unit_price": "1000000.01",
                }
            ],
        },
    )

    assert price_res.status_code == 422
    assert "less than or equal to 1000000" in price_res.text


@pytest.mark.asyncio
async def test_investing_price_submission_rejects_large_batches(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-price-batch@example.com",
        username="investing-price-batch",
        password="TestPass123!",
    )

    holding_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "MSFT",
            "account_id": account_map["brokerage"],
            "quantity": "1.00000000",
            "avg_cost": "100.00",
            "currency": "USD",
        },
    )
    assert holding_res.status_code == 201

    price_res = await client.post(
        "/v1/investing/prices",
        json={
            "price_date": datetime.now(UTC).date().isoformat(),
            "prices": [
                {"holding_public_id": str(uuid.uuid4()), "unit_price": "120.00"} for _ in range(501)
            ],
        },
    )

    assert price_res.status_code == 422
    assert "at most 500" in price_res.text


@pytest.mark.asyncio
async def test_investing_price_submission_rejects_duplicate_holdings(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-price-duplicate@example.com",
        username="investing-price-duplicate",
        password="TestPass123!",
    )

    holding_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "GOOGL",
            "account_id": account_map["brokerage"],
            "quantity": "1.00000000",
            "avg_cost": "100.00",
            "currency": "USD",
        },
    )
    assert holding_res.status_code == 201
    holding_id = holding_res.json()["public_id"]

    price_res = await client.post(
        "/v1/investing/prices",
        json={
            "price_date": datetime.now(UTC).date().isoformat(),
            "prices": [
                {"holding_public_id": holding_id, "unit_price": "120.00"},
                {"holding_public_id": holding_id, "unit_price": "121.00"},
            ],
        },
    )

    assert price_res.status_code == 422
    assert "Duplicate holding_public_id" in price_res.text


@pytest.mark.asyncio
async def test_investing_duplicate_holding_conflict(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-conflict@example.com",
        username="investing-conflict",
        password="TestPass123!",
    )

    payload = {
        "symbol": "AAPL",
        "account_id": account_map["brokerage"],
        "quantity": "1.00000000",
        "avg_cost": "100.00",
        "currency": "USD",
    }
    first = await client.post("/v1/investing/holdings", json=payload)
    assert first.status_code == 201

    second = await client.post("/v1/investing/holdings", json=payload)
    assert second.status_code == 409
    assert second.headers["content-type"].startswith("application/problem+json")
    body = second.json()
    assert body["type"] == "https://lifestack.app/errors/conflict"


@pytest.mark.asyncio
async def test_holding_instrument_type_defaults_to_stock_and_supports_etf(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-instrument-type@example.com",
        username="investing-instrument-type",
        password="TestPass123!",
    )

    stock_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "AAPL",
            "account_id": account_map["brokerage"],
            "quantity": "1.00000000",
            "avg_cost": "100.00",
            "currency": "USD",
        },
    )
    assert stock_res.status_code == 201, stock_res.text
    assert stock_res.json()["instrument_type"] == "stock"

    etf_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "VUSA",
            "account_id": account_map["wallet"],
            "quantity": "2.00000000",
            "avg_cost": "80.00",
            "currency": "USD",
            "instrument_type": "etf",
        },
    )
    assert etf_res.status_code == 201, etf_res.text
    assert etf_res.json()["instrument_type"] == "etf"

    list_res = await client.get("/v1/investing/holdings")
    assert list_res.status_code == 200
    by_symbol = {item["symbol"]: item for item in list_res.json()["items"]}
    assert by_symbol["AAPL"]["instrument_type"] == "stock"
    assert by_symbol["VUSA"]["instrument_type"] == "etf"

    async with postgres.async_session_maker() as session:
        instruments = (
            (
                await session.execute(
                    select(Instrument).where(Instrument.symbol.in_(["AAPL", "VUSA"]))
                )
            )
            .scalars()
            .all()
        )
        instrument_by_symbol = {item.symbol: item for item in instruments}
        assert instrument_by_symbol["AAPL"].instrument_type == "stock"
        assert instrument_by_symbol["AAPL"].company_id is not None
        assert instrument_by_symbol["VUSA"].instrument_type == "etf"
        assert instrument_by_symbol["VUSA"].company_id is None


@pytest.mark.asyncio
async def test_patch_instrument_type_corrects_existing_auto_created_stock(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-patch-instrument@example.com",
        username="investing-patch-instrument",
        password="TestPass123!",
    )

    holding_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "QQQ",
            "account_id": account_map["brokerage"],
            "quantity": "1.00000000",
            "avg_cost": "300.00",
            "currency": "USD",
        },
    )
    assert holding_res.status_code == 201, holding_res.text

    instruments_res = await client.get("/v1/investing/instruments")
    assert instruments_res.status_code == 200
    instrument = next(item for item in instruments_res.json() if item["symbol"] == "QQQ")
    assert instrument["instrument_type"] == "stock"

    patch_res = await client.patch(
        f"/v1/investing/instruments/{instrument['public_id']}",
        json={"instrument_type": "etf", "name": "Invesco QQQ Trust"},
    )
    assert patch_res.status_code == 200, patch_res.text
    assert patch_res.json()["instrument_type"] == "etf"
    assert patch_res.json()["name"] == "Invesco QQQ Trust"

    list_res = await client.get("/v1/investing/holdings")
    assert list_res.status_code == 200
    qqq = next(item for item in list_res.json()["items"] if item["symbol"] == "QQQ")
    assert qqq["instrument_type"] == "etf"


@pytest.mark.asyncio
async def test_investing_workspace_isolation(client: AsyncClient):
    # User A creates investing data
    account_map_a = await _register_and_login(
        client,
        email="investing-iso-a@example.com",
        username="investing-iso-a",
        password="TestPass123!",
    )
    create_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "MSFT",
            "account_id": account_map_a["brokerage"],
            "quantity": "3.00000000",
            "avg_cost": "250.00",
            "currency": "USD",
        },
    )
    assert create_res.status_code == 201
    holding_id = create_res.json()["public_id"]

    # Switch to User B and verify no access to A's workspace-scoped data
    await client.post("/v1/auth/logout")
    await _register_and_login(
        client,
        email="investing-iso-b@example.com",
        username="investing-iso-b",
        password="TestPass123!",
    )

    list_res = await client.get("/v1/investing/holdings")
    assert list_res.status_code == 200
    assert list_res.json()["items"] == []

    # Attempt cross-workspace update (expect 404/ValidationError since account belongs to A's workspace)
    patch_res = await client.patch(
        f"/v1/investing/holdings/{holding_id}",
        json={"quantity": "4.00000000"},
    )
    assert patch_res.status_code == 404

    delete_res = await client.delete(f"/v1/investing/holdings/{holding_id}")
    assert delete_res.status_code == 404

    # Attempt to create holding in B's workspace using A's account (expect 422 ValidationError due to cross-workspace account)
    cross_create_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "MSFT",
            "account_id": account_map_a["brokerage"],
            "quantity": "3.00000000",
            "avg_cost": "250.00",
            "currency": "USD",
        },
    )
    assert cross_create_res.status_code == 422
    assert "not found in this workspace" in cross_create_res.json()["detail"]


@pytest.mark.asyncio
async def test_investing_cash_balance_update_and_delete(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-cash@example.com",
        username="investing-cash",
        password="TestPass123!",
    )

    create_cash = {
        "account_id": account_map["wallet"],
        "balance": "123.45",
        "currency": "usd",
        "as_of": datetime.now(UTC).isoformat(),
    }
    create_res = await client.post("/v1/investing/cash-balances", json=create_cash)
    assert create_res.status_code == 201
    cash = create_res.json()
    cash_id = cash["public_id"]

    # Fetch cash from DB before update/delete to verify audit log by entity_id
    async with postgres.async_session_maker() as session:
        db_cash = (
            (
                await session.execute(
                    select(CashBalance).where(CashBalance.public_id == uuid.UUID(cash_id))
                )
            )
            .scalars()
            .one()
        )
        db_cash_id = db_cash.id

    update_res = await client.patch(
        f"/v1/investing/cash-balances/{cash_id}",
        json={"balance": "200.00"},
    )
    assert update_res.status_code == 200
    updated = update_res.json()
    assert updated["balance"] == "200.00"
    assert updated["currency"] == "USD"
    assert updated["account_id"] == account_map["wallet"]

    delete_res = await client.delete(f"/v1/investing/cash-balances/{cash_id}")
    assert delete_res.status_code == 204

    list_res = await client.get("/v1/investing/cash-balances")
    assert list_res.status_code == 200
    assert list_res.json()["items"] == []

    # Verify audit logs
    async with postgres.async_session_maker() as session:
        audits = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_type == "cash_balance")
                    .where(AuditLog.entity_id == db_cash_id)
                    .order_by(AuditLog.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )

        assert len(audits) == 3
        assert audits[0].module == "investing"
        assert audits[0].action == "create"
        assert audits[0].details["before"] is None
        assert audits[0].details["after"]["balance"] == "123.45"

        assert audits[1].action == "update"
        assert audits[1].details["before"]["balance"] == "123.45"
        assert audits[1].details["after"]["balance"] == "200.00"
        assert "balance" in audits[1].details["changed_fields"]

        assert audits[2].action == "delete"
        assert audits[2].details["before"]["balance"] == "200.00"
        assert audits[2].details["after"] is None


@pytest.mark.asyncio
async def test_investing_multi_currency_summary(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-multi-curr@example.com",
        username="investing-multi-curr",
        password="TestPass123!",
    )

    # 1. Create USD asset (Holding)
    await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "AAPL",
            "account_id": account_map["brokerage"],
            "quantity": "10.00000000",
            "avg_cost": "150.00",
            "currency": "usd",
        },
    )
    # 2. Create GBP asset (Holding)
    await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "SAP",
            "account_id": account_map["brokerage"],
            "quantity": "5.00000000",
            "avg_cost": "100.00",
            "currency": "gbp",
        },
    )
    # 3. Create USD cash balance
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["usd-wallet"],
            "balance": "1000.00",
            "currency": "usd",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    # 4. Create GBP cash balance
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["gbp-wallet"],
            "balance": "500.00",
            "currency": "gbp",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )

    # Multi-currency without reporting currency should require conversion.
    summary_res = await client.get("/v1/investing/summary")
    assert summary_res.status_code == 200
    summary = summary_res.json()
    assert summary["holdings_count"] == 2
    assert summary["portfolio_value"] is None
    assert summary["cash_total"] is None
    assert summary["reporting_currency"] is None
    assert summary["valuation_status"] == "multi_currency_unconverted"

    # Breakdown contains correct currency mappings for both
    assert Decimal(summary["currency_breakdown"]["USD"]) == Decimal("2500.00")
    assert Decimal(summary["currency_breakdown"]["GBP"]) == Decimal("1000.00")


@pytest.mark.asyncio
async def test_performance_summary_converts_multi_currency_snapshot(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-performance-fx@example.com",
        username="investing-performance-fx",
        password="TestPass123!",
    )

    settings_res = await client.patch(
        "/v1/finance/settings",
        json={"reporting_currency_code": "USD"},
    )
    assert settings_res.status_code == 200, settings_res.text

    empty_performance_res = await client.get("/v1/investing/performance/summary")
    assert empty_performance_res.status_code == 200, empty_performance_res.text
    assert Decimal(empty_performance_res.json()["total_value"]) == Decimal("0.00")

    async with postgres.async_session_maker() as session:
        account = (
            await session.execute(
                select(Account).where(Account.public_id == uuid.UUID(account_map["usd-wallet"]))
            )
        ).scalar_one()
        workspace_id = account.workspace_id
        existing_eur = (
            await session.execute(select(Currency).where(Currency.code == "EUR"))
        ).scalar_one_or_none()
        if existing_eur is None:
            session.add(Currency(code="EUR", name="Euro", symbol="EUR", minor_unit=2))
        enabled_eur = (
            await session.execute(
                select(WorkspaceCurrency).where(
                    WorkspaceCurrency.workspace_id == workspace_id,
                    WorkspaceCurrency.currency_code == "EUR",
                )
            )
        ).scalar_one_or_none()
        if enabled_eur is None:
            session.add(WorkspaceCurrency(workspace_id=workspace_id, currency_code="EUR"))
        await session.commit()

    price_date = datetime.now(UTC).date()
    usd_holding_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "AAPL",
            "account_id": account_map["usd-wallet"],
            "quantity": "2.00000000",
            "avg_cost": "100.00",
            "currency": "usd",
        },
    )
    assert usd_holding_res.status_code == 201, usd_holding_res.text

    gbp_holding_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "VUSA",
            "account_id": account_map["gbp-wallet"],
            "quantity": "3.00000000",
            "avg_cost": "10.00",
            "currency": "gbp",
        },
    )
    assert gbp_holding_res.status_code == 201, gbp_holding_res.text

    cash_res = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["eur-wallet"],
            "balance": "100.00",
            "currency": "eur",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    assert cash_res.status_code == 201, cash_res.text

    price_res = await client.post(
        "/v1/investing/prices",
        json={
            "price_date": price_date.isoformat(),
            "prices": [
                {
                    "holding_public_id": usd_holding_res.json()["public_id"],
                    "unit_price": "110.00",
                },
                {
                    "holding_public_id": gbp_holding_res.json()["public_id"],
                    "unit_price": "12.00",
                },
            ],
        },
    )
    assert price_res.status_code == 201, price_res.text

    now = datetime.now(UTC)
    async with postgres.async_session_maker() as session:
        session.add_all([
            FxRate(
                base_currency_code="GBP",
                quote_currency_code="USD",
                rate=Decimal("1.2500000000"),
                as_of=now,
                fetched_at=now,
                source="test",
            ),
            FxRate(
                base_currency_code="EUR",
                quote_currency_code="USD",
                rate=Decimal("1.0850000000"),
                as_of=now,
                fetched_at=now,
                source="test",
            ),
            PortfolioSnapshot(
                workspace_id=workspace_id,
                snapshot_date=price_date - timedelta(days=1),
                total_value=Decimal("250.00"),
                total_cost=Decimal("200.00"),
                holdings_value=Decimal("200.00"),
                cash_value=Decimal("50.00"),
                currency_code="USD",
                fx_rates_used={},
            ),
        ])
        await session.commit()

    perf_res = await client.get("/v1/investing/performance/summary")
    assert perf_res.status_code == 200, perf_res.text
    perf = perf_res.json()

    assert perf["currency"] == "USD"
    assert Decimal(perf["portfolio_value"]) == Decimal("265.00")
    assert Decimal(perf["cash_total"]) == Decimal("108.50")
    assert Decimal(perf["total_value"]) == Decimal("265.00")
    assert Decimal(perf["total_cost"]) == Decimal("237.50")
    assert Decimal(perf["invested_value"]) == Decimal("237.50")
    assert Decimal(perf["total_gain_loss"]) == Decimal("27.50")
    assert Decimal(perf["total_gain_loss_pct"]) == Decimal("11.57894736842105263157894737")
    assert Decimal(perf["daily_change"]) == Decimal("65.00")
    assert Decimal(perf["daily_change_pct"]) == Decimal("32.500")
    assert perf["previous_snapshot_date"] == (price_date - timedelta(days=1)).isoformat()
    assert perf["valuation_status"] == "current"
    assert Decimal(perf["fx_rates_used"]["GBP"]) == Decimal("1.2500000000")
    assert Decimal(perf["fx_rates_used"]["EUR"]) == Decimal("1.0850000000")

    async with postgres.async_session_maker() as session:
        snapshot = (
            await session.execute(
                select(PortfolioSnapshot).where(
                    PortfolioSnapshot.workspace_id == workspace_id,
                    PortfolioSnapshot.currency_code == "USD",
                    PortfolioSnapshot.snapshot_date == price_date,
                )
            )
        ).scalar_one()

    assert snapshot.total_value == Decimal("373.50")
    assert snapshot.total_cost == Decimal("237.50")
    assert snapshot.holdings_value == Decimal("265.00")
    assert snapshot.cash_value == Decimal("108.50")
    assert snapshot.fx_rates_used == {"EUR": "1.0850000000", "GBP": "1.2500000000"}

    settings_res = await client.patch(
        "/v1/finance/settings",
        json={"reporting_currency_code": "EUR"},
    )
    assert settings_res.status_code == 200, settings_res.text

    perf_eur_res = await client.get("/v1/investing/performance/summary")
    assert perf_eur_res.status_code == 200, perf_eur_res.text
    perf_eur = perf_eur_res.json()

    assert perf_eur["currency"] == "EUR"
    assert Decimal(perf_eur["portfolio_value"]) == Decimal("244.24")
    assert Decimal(perf_eur["cash_total"]) == Decimal("100.00")
    assert Decimal(perf_eur["total_value"]) == Decimal("244.24")
    assert Decimal(perf_eur["total_cost"]) == Decimal("218.89")
    assert Decimal(perf_eur["total_gain_loss"]) == Decimal("25.35")
    assert Decimal(perf_eur["fx_rates_used"]["USD"]).quantize(Decimal("0.0000000001")) == Decimal(
        "0.9216589862"
    )
    assert Decimal(perf_eur["fx_rates_used"]["GBP"]).quantize(Decimal("0.0000000001")) == Decimal(
        "1.1520737327"
    )


@pytest.mark.asyncio
async def test_investing_lookthrough_exposure_and_overlap(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-lookthrough@example.com",
        username="investing-lookthrough",
        password="TestPass123!",
    )

    instrument_res = await client.post(
        "/v1/investing/instruments",
        json={
            "symbol": "VTI",
            "name": "Vanguard Total Market ETF",
            "instrument_type": "etf",
        },
    )
    assert instrument_res.status_code == 201
    instrument_id = instrument_res.json()["public_id"]

    today = datetime.now(UTC).date().isoformat()
    constituent_res = await client.post(
        f"/v1/investing/instruments/{instrument_id}/constituents",
        json={
            "as_of_date": today,
            "source": "test-seed",
            "fetched_at": datetime.now(UTC).isoformat(),
            "constituents": [
                {"company_name": "Apple Inc", "company_ticker": "AAPL", "weight": "0.60000000"},
                {
                    "company_name": "Microsoft Corp",
                    "company_ticker": "MSFT",
                    "weight": "0.40000000",
                },
            ],
        },
    )
    assert constituent_res.status_code == 201
    assert len(constituent_res.json()) == 2

    etf_holding = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "VTI",
            "account_id": account_map["brokerage"],
            "quantity": "10.00000000",
            "avg_cost": "100.00",
            "currency": "USD",
        },
    )
    assert etf_holding.status_code == 201

    direct_holding = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "AAPL",
            "account_id": account_map["wallet"],
            "quantity": "2.00000000",
            "avg_cost": "150.00",
            "currency": "USD",
        },
    )
    assert direct_holding.status_code == 201

    exposure_res = await client.get("/v1/investing/analytics/exposure", params={"as_of": today})
    assert exposure_res.status_code == 200
    exposure = exposure_res.json()
    assert exposure["analysis_status"] == "complete"
    assert exposure["snapshot_coverage"] == "1"
    assert Decimal(exposure["total_lookthrough_exposure"]) > Decimal("0")
    assert len(exposure["exposure"]) >= 2

    aapl_row = next((row for row in exposure["exposure"] if row["company_ticker"] == "AAPL"), None)
    assert aapl_row is not None
    assert Decimal(aapl_row["lookthrough_exposure"]) > Decimal(aapl_row["direct_exposure"])

    overlap_res = await client.get("/v1/investing/analytics/overlap", params={"as_of": today})
    assert overlap_res.status_code == 200
    overlap = overlap_res.json()
    assert overlap["analysis_status"] == "complete"
    assert len(overlap["overlaps"]) >= 2
    assert overlap["overlaps"][0]["company_ticker"] in {"AAPL", "MSFT"}


@pytest.mark.asyncio
async def test_investing_constituent_weights_validation(client: AsyncClient):
    await _register_and_login(
        client,
        email="constituent-weights@example.com",
        username="const-weights",
        password="TestPass123!",
    )

    instrument_res = await client.post(
        "/v1/investing/instruments",
        json={
            "symbol": "VTI",
            "name": "Vanguard Total Market ETF",
            "instrument_type": "etf",
        },
    )
    assert instrument_res.status_code == 201
    instrument_id = instrument_res.json()["public_id"]

    today = datetime.now(UTC).date().isoformat()
    # sum of weights is 0.5 (invalid, should fail)
    res = await client.post(
        f"/v1/investing/instruments/{instrument_id}/constituents",
        json={
            "as_of_date": today,
            "source": "test-seed",
            "fetched_at": datetime.now(UTC).isoformat(),
            "constituents": [
                {"company_name": "Apple Inc", "company_ticker": "AAPL", "weight": "0.3"},
                {"company_name": "Microsoft Corp", "company_ticker": "MSFT", "weight": "0.2"},
            ],
        },
    )
    assert res.status_code == 422
    assert "Constituent weights must sum to approximately 1.0" in res.json()["detail"]


@pytest.mark.asyncio
async def test_investing_constituent_workspace_isolation(client: AsyncClient):
    """Verify that constituents and instrument updates are workspace isolated."""
    # Register and login User A
    await _register_and_login(
        client,
        email="const-iso-a@example.com",
        username="const-iso-a",
        password="TestPass123!",
    )
    etf_res = await client.post(
        "/v1/investing/instruments",
        json={
            "symbol": "IVV",
            "name": "iShares Core S&P 500 ETF",
            "instrument_type": "etf",
        },
    )
    assert etf_res.status_code == 201
    etf_a_id = etf_res.json()["public_id"]

    # Register and login User B
    await client.post("/v1/auth/logout")
    await _register_and_login(
        client,
        email="const-iso-b@example.com",
        username="const-iso-b",
        password="TestPass123!",
    )

    # User B tries to fetch User A's ETF constituents
    today = datetime.now(UTC).date().isoformat()
    get_res = await client.get(
        f"/v1/investing/instruments/{etf_a_id}/constituents",
        params={"as_of": today},
    )
    assert get_res.status_code == 404

    # User B tries to upsert constituents on User A's ETF
    upsert_res = await client.post(
        f"/v1/investing/instruments/{etf_a_id}/constituents",
        json={
            "as_of_date": today,
            "source": "malicious",
            "fetched_at": datetime.now(UTC).isoformat(),
            "constituents": [
                {"company_name": "Apple Inc", "company_ticker": "AAPL", "weight": "1.0"},
            ],
        },
    )
    assert upsert_res.status_code == 404


@pytest.mark.asyncio
async def test_portfolio_snapshot_fx_rates_validation():
    """Verify that fx_rates_used in PortfolioSnapshot enforces correct format."""
    # Valid snapshot creation
    snapshot = PortfolioSnapshot(
        workspace_id=1,
        snapshot_date=datetime.now(UTC).date(),
        total_value=Decimal("100.00"),
        total_cost=Decimal("90.00"),
        holdings_value=Decimal("80.00"),
        cash_value=Decimal("20.00"),
        currency_code="USD",
        fx_rates_used={"EUR": "1.085", "GBP": 1.25},
    )
    assert snapshot.fx_rates_used["EUR"] == "1.085"

    # Invalid cases
    invalid_cases = [
        "not-a-dict",
        {"EUR": "invalid-rate"},
        {"eur": "1.085"},  # lowercase code
        {"US": "1.085"},  # wrong length code
        {"USDT": "1.085"},  # wrong length code
        {"EUR": -1.0},  # negative rate
        {"EUR": 0},  # zero rate
    ]

    for invalid_fx in invalid_cases:
        with pytest.raises(ValidationError):
            PortfolioSnapshot(
                workspace_id=1,
                snapshot_date=datetime.now(UTC).date(),
                total_value=Decimal("100.00"),
                total_cost=Decimal("90.00"),
                holdings_value=Decimal("80.00"),
                cash_value=Decimal("20.00"),
                currency_code="USD",
                fx_rates_used=invalid_fx,
            )


@pytest.mark.asyncio
async def test_investing_prices_refresh_and_valuation_fields(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="refresh-val@example.com",
        username="refresh-val",
        password="TestPass123!",
    )

    # 1. Create a holding
    holding_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "AAPL",
            "account_id": account_map["brokerage"],
            "quantity": "10.00000000",
            "avg_cost": "150.00",
            "currency": "USD",
        },
    )
    assert holding_res.status_code == 201
    holding = holding_res.json()

    # No prices yet -> fallback to avg_cost
    assert Decimal(holding["current_price"]) == Decimal("150.00")
    assert Decimal(holding["current_value"]) == Decimal("1500.00")
    assert Decimal(holding["gain_loss"]) == Decimal("0.00")
    assert Decimal(holding["gain_loss_pct"]) == Decimal("0.00")

    # 2. Trigger refresh with mock stock API
    with patch("app.investing.service._fetch_stock_price", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = Decimal("180.00")

        refresh_res = await client.post("/v1/investing/prices/refresh")
        assert refresh_res.status_code == 200
        assert "AAPL" in refresh_res.json()["updated"]

        mock_fetch.assert_called_once()
        _, symbol, currency = mock_fetch.call_args.args
        assert symbol == "AAPL"
        assert currency == "USD"

    # 3. Verify updated fields in holding list
    list_res = await client.get("/v1/investing/holdings")
    assert list_res.status_code == 200
    items = list_res.json()["items"]
    assert len(items) == 1
    h_data = items[0]

    assert Decimal(h_data["current_price"]) == Decimal("180.00")
    assert Decimal(h_data["current_value"]) == Decimal("1800.00")
    assert Decimal(h_data["gain_loss"]) == Decimal("300.00")
    assert Decimal(h_data["gain_loss_pct"]) == Decimal("20.00")

    # 4. Verify audit log
    async with postgres.async_session_maker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.action == "holding_prices_submitted")
        )
        audit = result.scalars().first()
        assert audit is not None
        assert audit.details["prices"]["AAPL"] == "180.00"
        assert audit.details["source"] == "api"


@pytest.mark.asyncio
async def test_investing_prices_refresh_indian_stock_appends_ns(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="indian-refresh@example.com",
        username="indian-refresh",
        password="TestPass123!",
    )

    # Create an INR holding with symbol TATSILV (no dot)
    holding_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "TATSILV",
            "account_id": account_map["brokerage"],
            "quantity": "10.00000000",
            "avg_cost": "20.00",
            "currency": "INR",
        },
    )
    assert holding_res.status_code == 201

    # Trigger refresh with mock fetch API
    with patch("app.investing.service._fetch_stock_price", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = Decimal("23.34")

        refresh_res = await client.post("/v1/investing/prices/refresh")
        assert refresh_res.status_code == 200
        assert "TATSILV" in refresh_res.json()["updated"]

        # Assert that it was called with symbol TATSILV and currency INR
        mock_fetch.assert_called_once()
        _, symbol, currency = mock_fetch.call_args.args
        assert symbol == "TATSILV"
        assert currency == "INR"
