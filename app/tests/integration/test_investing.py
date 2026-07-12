import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

from app.application.jobs import bhavcopy_price_feed_job
from app.core.audit import AuditLog
from app.core.database import postgres
from app.finance.models import Account, Currency, FxRate, WorkspaceCurrency
from app.investing.models import (
    CashBalance,
    Holding,
    HoldingPrice,
    Instrument,
    OrderLot,
    PortfolioSnapshot,
)
from app.investing.repository import CompanyRepository, InstrumentRepository
from app.investing.schemas import InstrumentType
from app.investing.service import InstrumentService


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


async def _create_brokerage_account(client: AsyncClient, name: str, currency: str) -> str:
    """One account, one currency (spec-050): _register_and_login's account_map
    accounts are all USD, so non-USD scenarios need their own dedicated account."""
    res = await client.post(
        "/v1/finance/accounts",
        json={"name": name, "account_type": "brokerage", "default_currency_code": currency},
    )
    assert res.status_code == 201, res.text
    return res.json()["public_id"]


async def _create_holding_via_order(
    client: AsyncClient,
    account_id: str,
    symbol: str,
    quantity: str,
    price: str,
    currency: str = "USD",
    instrument_type: str = "stock",
    instrument_name: str | None = None,
    occurred_at: str | None = None,
) -> str:
    """Create a holding by placing a buy order (the only supported path)."""
    cost = float(quantity) * float(price)
    occurred_at = occurred_at or datetime.now(UTC).isoformat()
    cash_res = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_id,
            "balance": f"{cost + 1000:.2f}",
            "currency": currency.upper(),
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    assert cash_res.status_code == 201, cash_res.text
    order_payload: dict = {
        "account_id": account_id,
        "order_type": "buy",
        "symbol": symbol,
        "quantity": quantity,
        "price_per_unit": price,
        "currency": currency.upper(),
        "occurred_at": occurred_at,
        "instrument_type": instrument_type,
    }
    if instrument_name is not None:
        order_payload["instrument_name"] = instrument_name
    order_res = await client.post("/v1/investing/orders", json=order_payload)
    assert order_res.status_code == 201, order_res.text
    holdings_res = await client.get("/v1/investing/holdings")
    holding = next(
        h
        for h in holdings_res.json()["items"]
        if h["symbol"] == symbol.upper() and h["account_id"] == account_id
    )
    return holding["public_id"]


@pytest.mark.asyncio
async def test_investing_crud_summary_and_audit(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-e2e@example.com",
        username="investing-e2e",
        password="TestPass123!",
    )

    # Create holding via buy order (holdings are order-derived)
    holding_id = await _create_holding_via_order(
        client, account_map["brokerage"], "AAPL", "10.50000000", "150.25"
    )
    holdings_res = await client.get("/v1/investing/holdings")
    assert holdings_res.status_code == 200
    holding = next(h for h in holdings_res.json()["items"] if h["public_id"] == holding_id)
    assert holding["symbol"] == "AAPL"
    assert holding["quantity"] == "10.50000000"
    assert holding["avg_cost"] == "150.250000"
    assert holding["currency"] == "USD"
    assert holding["account_id"] == account_map["brokerage"]
    # book_value is server-computed (quantity * avg_cost) so the frontend
    # doesn't have to re-derive it with float arithmetic.
    assert Decimal(holding["book_value"]) == Decimal("10.50000000") * Decimal("150.25")

    # Update holding — this holding is order-derived, so quantity/avg_cost are
    # computed from orders and not directly editable (see spec-045); symbol and
    # instrument_type remain editable.
    update_res = await client.patch(
        f"/v1/investing/holdings/{holding_id}",
        json={"symbol": "AAPL2", "instrument_type": "stock"},
    )
    assert update_res.status_code == 200
    updated = update_res.json()
    assert updated["symbol"] == "AAPL2"
    assert updated["quantity"] == "10.50000000"
    assert updated["avg_cost"] == "150.250000"

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
    assert summary["portfolio_value"] == "1577.62500000000000"
    assert summary["cash_total"] == "1000.00"
    assert summary["currency_breakdown"]["USD"] == "2577.62500000000000"
    assert summary["daily_change"] is None
    assert summary["reporting_currency"] == "USD"
    assert summary["valuation_status"] == "cost_basis_fallback"

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
        assert db_holding.quantity == Decimal("10.50000000")
        assert db_holding.symbol == "AAPL2"
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
        assert len(audits) >= 1
        assert audits[-1].module == "investing"
        assert audits[-1].action == "update"

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

    holding_id = await _create_holding_via_order(
        client, account_map["brokerage"], "AAPL", "1.00000000", "100.00"
    )

    price_res = await client.post(
        "/v1/investing/prices",
        json={
            "price_date": datetime.now(UTC).date().isoformat(),
            "prices": [
                {
                    "holding_public_id": holding_id,
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

    await _create_holding_via_order(
        client, account_map["brokerage"], "MSFT", "1.00000000", "100.00"
    )

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

    holding_id = await _create_holding_via_order(
        client, account_map["brokerage"], "GOOGL", "1.00000000", "100.00"
    )

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
async def test_patch_instrument_type_corrects_existing_auto_created_stock(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-patch-instrument@example.com",
        username="investing-patch-instrument",
        password="TestPass123!",
    )

    await _create_holding_via_order(
        client, account_map["brokerage"], "CUSTOMQQQ", "1.00000000", "300.00"
    )

    instruments_res = await client.get("/v1/investing/instruments")
    assert instruments_res.status_code == 200
    instrument = next(item for item in instruments_res.json() if item["symbol"] == "CUSTOMQQQ")
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
    qqq = next(item for item in list_res.json()["items"] if item["symbol"] == "CUSTOMQQQ")
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
    holding_id = await _create_holding_via_order(
        client, account_map_a["brokerage"], "MSFT", "3.00000000", "250.00"
    )

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

    # Attempt cross-workspace update (expect 404 since account belongs to A's workspace)
    patch_res = await client.patch(
        f"/v1/investing/holdings/{holding_id}",
        json={"quantity": "4.00000000"},
    )
    assert patch_res.status_code == 404

    delete_res = await client.delete(f"/v1/investing/holdings/{holding_id}")
    assert delete_res.status_code == 404

    # Attempt to place order in B's workspace using A's account (expect 422 due to cross-workspace account)
    cross_create_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": account_map_a["brokerage"],
            "order_type": "buy",
            "symbol": "MSFT",
            "quantity": "1.00000000",
            "price_per_unit": "250.00",
            "currency": "USD",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
    )
    assert cross_create_res.status_code in (404, 422)


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

    # One account, one currency (spec-050): GBP assets/cash need their own
    # GBP-denominated brokerage account rather than sharing account_map["brokerage"]
    # (USD) or the misleadingly-named but USD-default account_map["gbp-wallet"].
    gbp_broker_id = await _create_brokerage_account(client, "GBP Brokerage", "GBP")

    # 1. Create USD asset via order on brokerage; then zero out the leftover cash
    await _create_holding_via_order(
        client, account_map["brokerage"], "AAPL", "10.00000000", "150.00", "usd"
    )
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["brokerage"],
            "balance": "0.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    # 2. Create GBP asset via order on the GBP brokerage; then zero out the leftover cash
    await _create_holding_via_order(client, gbp_broker_id, "SAP", "5.00000000", "100.00", "gbp")
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": gbp_broker_id,
            "balance": "0.00",
            "currency": "GBP",
            "as_of": datetime.now(UTC).isoformat(),
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
            "account_id": gbp_broker_id,
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

    # Breakdown: USD = AAPL value (10*150=1500) + usd-wallet cash (1000) = 2500
    #            GBP = SAP value (5*100=500) + gbp-wallet cash (500) = 1000
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
    usd_holding_id = await _create_holding_via_order(
        client, account_map["usd-wallet"], "AAPL", "2.00000000", "100.00", "usd"
    )
    # Zero out the order's leftover USD cash so EUR-wallet is the only cash source
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["usd-wallet"],
            "balance": "0.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )

    # One account, one currency (spec-050): account_map's "gbp-wallet"/"eur-wallet"
    # are USD-default despite their names, so this needs dedicated accounts.
    gbp_broker_id = await _create_brokerage_account(client, "GBP Brokerage", "GBP")
    eur_broker_id = await _create_brokerage_account(client, "EUR Brokerage", "EUR")

    gbp_holding_id = await _create_holding_via_order(
        client, gbp_broker_id, "VUSA", "3.00000000", "10.00", "gbp"
    )
    # Zero out the order's leftover GBP cash
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": gbp_broker_id,
            "balance": "0.00",
            "currency": "GBP",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )

    cash_res = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": eur_broker_id,
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
                    "holding_public_id": usd_holding_id,
                    "unit_price": "110.00",
                },
                {
                    "holding_public_id": gbp_holding_id,
                    "unit_price": "12.00",
                },
            ],
        },
    )
    assert price_res.status_code == 201, price_res.text

    now = datetime.now(UTC)
    # spec-075: display conversion always uses the *previous* day's close --
    # a same-day rate is never picked up (no intraday/live refresh).
    rate_as_of = now - timedelta(days=1)
    async with postgres.async_session_maker() as session:
        session.add_all([
            FxRate(
                base_currency_code="GBP",
                quote_currency_code="USD",
                rate=Decimal("1.2500000000"),
                as_of=rate_as_of,
                fetched_at=now,
                source="test",
            ),
            FxRate(
                base_currency_code="EUR",
                quote_currency_code="USD",
                rate=Decimal("1.0850000000"),
                as_of=rate_as_of,
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
async def test_performance_summary_cash_total_excludes_non_brokerage_accounts(client: AsyncClient):
    """Investing > Cash must only ever reflect brokerage-account cash, matching
    the brokerage-only filter already applied to net worth's investing_cash_total
    (spec-050). A bank account's manual cash-balance snapshot (used for
    reconciliation) must not leak into the investing performance total."""
    account_map = await _register_and_login(
        client,
        email="investing-perf-brokerage-filter@example.com",
        username="investing-perf-brokerage-filter",
        password="TestPass123!",
    )

    bank_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Bank", "account_type": "bank", "default_currency_code": "USD"},
    )
    assert bank_res.status_code == 201, bank_res.text
    bank_id = bank_res.json()["public_id"]

    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["brokerage"],
            "balance": "1000.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": bank_id,
            "balance": "500.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )

    perf_res = await client.get("/v1/investing/performance/summary")
    assert perf_res.status_code == 200, perf_res.text
    assert Decimal(perf_res.json()["cash_total"]) == Decimal("1000.00")


@pytest.mark.asyncio
async def test_performance_summary_cash_total_reflects_same_day_order_changes(client: AsyncClient):
    """The performance snapshot cash_total is cached for the day, but placing,
    editing, or deleting an order changes investing_cash_balances immediately —
    the cache must be invalidated on each of those so the figure doesn't go
    stale for the rest of the day."""
    account_map = await _register_and_login(
        client,
        email="investing-perf-staleness@example.com",
        username="investing-perf-staleness",
        password="TestPass123!",
    )

    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["brokerage"],
            "balance": "1000.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )

    # First read creates today's portfolio snapshot.
    first_res = await client.get("/v1/investing/performance/summary")
    assert first_res.status_code == 200, first_res.text
    assert Decimal(first_res.json()["cash_total"]) == Decimal("1000.00")

    order_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": account_map["brokerage"],
            "order_type": "buy",
            "symbol": "AAPL",
            "quantity": "1.00000000",
            "price_per_unit": "100.00",
            "currency": "USD",
            "occurred_at": datetime.now(UTC).isoformat(),
            "instrument_type": "stock",
        },
    )
    assert order_res.status_code == 201, order_res.text
    order_id = order_res.json()["public_id"]

    second_res = await client.get("/v1/investing/performance/summary")
    assert second_res.status_code == 200, second_res.text
    assert Decimal(second_res.json()["cash_total"]) == Decimal("900.00")

    update_res = await client.patch(
        f"/v1/investing/orders/{order_id}",
        json={"quantity": "2.00000000"},
    )
    assert update_res.status_code == 200, update_res.text

    third_res = await client.get("/v1/investing/performance/summary")
    assert third_res.status_code == 200, third_res.text
    assert Decimal(third_res.json()["cash_total"]) == Decimal("800.00")

    delete_res = await client.delete(f"/v1/investing/orders/{order_id}")
    assert delete_res.status_code == 204, delete_res.text

    fourth_res = await client.get("/v1/investing/performance/summary")
    assert fourth_res.status_code == 200, fourth_res.text
    assert Decimal(fourth_res.json()["cash_total"]) == Decimal("1000.00")


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

    await _create_holding_via_order(
        client, account_map["brokerage"], "VTI", "10.00000000", "100.00", "USD", "etf"
    )
    await _create_holding_via_order(
        client, account_map["wallet"], "AAPL", "2.00000000", "150.00", "USD"
    )

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

    threshold_res = await client.patch(
        "/v1/finance/settings",
        json={"lookthrough_min_weight_pct": "25"},
    )
    assert threshold_res.status_code == 200

    filtered_res = await client.get("/v1/investing/analytics/exposure", params={"as_of": today})
    assert filtered_res.status_code == 200
    filtered = filtered_res.json()
    assert filtered["display_threshold_pct"] == "25.0000"
    assert filtered["hidden_exposure_count"] > 0
    assert len(filtered["exposure"]) + filtered["hidden_exposure_count"] == len(
        exposure["exposure"]
    )
    assert filtered["total_lookthrough_exposure"] == exposure["total_lookthrough_exposure"]

    filtered_overlap_res = await client.get(
        "/v1/investing/analytics/overlap", params={"as_of": today}
    )
    assert filtered_overlap_res.status_code == 200
    filtered_overlap = filtered_overlap_res.json()
    assert filtered_overlap["hidden_overlap_count"] > 0
    assert len(filtered_overlap["overlaps"]) + filtered_overlap["hidden_overlap_count"] == len(
        overlap["overlaps"]
    )
    assert filtered_overlap["top_5_concentration_pct"] == overlap["top_5_concentration_pct"]


@pytest.mark.asyncio
async def test_investing_lookthrough_converts_holdings_to_reporting_currency(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-lookthrough-fx@example.com",
        username="investing-lookthrough-fx",
        password="TestPass123!",
    )
    settings_res = await client.patch(
        "/v1/finance/settings",
        json={"reporting_currency_code": "USD"},
    )
    assert settings_res.status_code == 200

    instrument_res = await client.post(
        "/v1/investing/instruments",
        json={
            "symbol": "VTI",
            "name": "Vanguard Total Market ETF",
            "instrument_type": "etf",
        },
    )
    instrument_id = instrument_res.json()["public_id"]
    today = datetime.now(UTC).date()
    assert (
        await client.post(
            f"/v1/investing/instruments/{instrument_id}/constituents",
            json={
                "as_of_date": today.isoformat(),
                "source": "test-seed",
                "fetched_at": datetime.now(UTC).isoformat(),
                "constituents": [
                    {"company_name": "Apple Inc", "company_ticker": "AAPL", "weight": "1.0"}
                ],
            },
        )
    ).status_code == 201

    # One account, one currency (spec-050): the GBP holding needs its own account.
    gbp_broker_id = await _create_brokerage_account(client, "GBP Brokerage", "GBP")
    await _create_holding_via_order(
        client, gbp_broker_id, "VTI", "10.00000000", "100.00", "GBP", "etf"
    )
    await _create_holding_via_order(
        client, account_map["wallet"], "AAPL", "2.00000000", "150.00", "USD"
    )

    async with postgres.async_session_maker() as session:
        session.add(
            FxRate(
                base_currency_code="GBP",
                quote_currency_code="USD",
                rate=Decimal("1.25"),
                # spec-075: display conversion uses the previous day's close.
                as_of=datetime.now(UTC) - timedelta(days=1),
                fetched_at=datetime.now(UTC),
                source="test",
            )
        )
        await session.commit()

    exposure_res = await client.get(
        "/v1/investing/analytics/exposure", params={"as_of": today.isoformat()}
    )
    assert exposure_res.status_code == 200
    exposure = exposure_res.json()
    assert exposure["currency"] == "USD"
    assert Decimal(exposure["total_direct_exposure"]) == Decimal("300")
    assert Decimal(exposure["total_lookthrough_exposure"]) == Decimal("1550")
    assert Decimal(exposure["fx_rates_used"]["GBP"]) == Decimal("1.25")

    overlap_res = await client.get(
        "/v1/investing/analytics/overlap", params={"as_of": today.isoformat()}
    )
    assert overlap_res.status_code == 200
    overlap = overlap_res.json()
    assert overlap["currency"] == "USD"
    assert sum(Decimal(row["overlap_exposure"]) for row in overlap["overlaps"]) == Decimal("1550")


@pytest.mark.asyncio
async def test_investing_lookthrough_does_not_mix_currencies_without_reporting_currency(
    client: AsyncClient,
):
    account_map = await _register_and_login(
        client,
        email="investing-lookthrough-no-fx@example.com",
        username="investing-lookthrough-no-fx",
        password="TestPass123!",
    )
    # One account, one currency (spec-050): the GBP holding needs its own account.
    gbp_broker_id = await _create_brokerage_account(client, "GBP Brokerage", "GBP")
    for symbol, currency, account_id in [
        ("AAPL", "USD", account_map["brokerage"]),
        ("VOD", "GBP", gbp_broker_id),
    ]:
        await _create_holding_via_order(
            client, account_id, symbol, "1.00000000", "100.00", currency
        )

    response = await client.get(
        "/v1/investing/analytics/exposure",
        params={"as_of": datetime.now(UTC).date().isoformat()},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["analysis_status"] == "unavailable"
    assert body["currency"] is None
    assert body["total_direct_exposure"] is None
    assert body["total_lookthrough_exposure"] is None
    assert body["exposure"] == []


@pytest.mark.asyncio
async def test_investing_lookthrough_ignores_fully_sold_positions(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-lookthrough-closed@example.com",
        username="investing-lookthrough-closed",
        password="TestPass123!",
    )

    # A mutual fund holding that's fully sold (zero quantity, zero book value)
    # has no current exposure and no constituent snapshot — it must not
    # generate warnings or otherwise affect analytics for still-open holdings.
    await _create_holding_via_order(
        client,
        account_map["brokerage"],
        "118285",
        "10.00000000",
        "100.00",
        "USD",
        "mutual_fund",
        instrument_name="Some Fully Sold Fund",
    )
    sell_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": account_map["brokerage"],
            "order_type": "sell",
            "symbol": "118285",
            "quantity": "10.00000000",
            "price_per_unit": "110.00",
            "currency": "USD",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
    )
    assert sell_res.status_code == 201, sell_res.text

    await _create_holding_via_order(
        client, account_map["wallet"], "AAPL", "2.00000000", "150.00", "USD"
    )

    today = datetime.now(UTC).date().isoformat()
    exposure_res = await client.get("/v1/investing/analytics/exposure", params={"as_of": today})
    assert exposure_res.status_code == 200
    exposure = exposure_res.json()
    assert exposure["analysis_status"] == "complete"
    assert exposure["warnings"] == []


@pytest.mark.asyncio
async def test_investing_lookthrough_closed_position_currency_does_not_block_reporting(
    client: AsyncClient,
):
    account_map = await _register_and_login(
        client,
        email="investing-lookthrough-closed-fx@example.com",
        username="investing-lookthrough-closed-fx",
        password="TestPass123!",
    )
    # One account, one currency (spec-050): the GBP holding needs its own account.
    gbp_broker_id = await _create_brokerage_account(client, "GBP Brokerage", "GBP")

    await _create_holding_via_order(
        client, account_map["brokerage"], "AAPL", "1.00000000", "100.00", "USD"
    )
    await _create_holding_via_order(client, gbp_broker_id, "VOD", "1.00000000", "100.00", "GBP")
    sell_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": gbp_broker_id,
            "order_type": "sell",
            "symbol": "VOD",
            "quantity": "1.00000000",
            "price_per_unit": "110.00",
            "currency": "GBP",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
    )
    assert sell_res.status_code == 201, sell_res.text

    # The GBP position is fully closed now — with no *open* multi-currency
    # exposure remaining, analytics should resolve USD automatically instead
    # of demanding a reporting currency because of a stale closed position.
    response = await client.get(
        "/v1/investing/analytics/exposure",
        params={"as_of": datetime.now(UTC).date().isoformat()},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["analysis_status"] == "complete"
    assert body["currency"] == "USD"


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
            "symbol": "CUSTOMIVV",
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

    # 1. Create a holding via buy order
    await _create_holding_via_order(
        client, account_map["brokerage"], "AAPL", "10.00000000", "150.00"
    )
    holdings_list = await client.get("/v1/investing/holdings")
    holding = next(h for h in holdings_list.json()["items"] if h["symbol"] == "AAPL")

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
    await _register_and_login(
        client,
        email="indian-refresh@example.com",
        username="indian-refresh",
        password="TestPass123!",
    )
    inr_broker_id = await _create_brokerage_account(client, "INR Brokerage", "INR")

    # Create an INR holding with symbol TATSILV (no dot)
    await _create_holding_via_order(client, inr_broker_id, "TATSILV", "10.00000000", "20.00", "INR")

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


@pytest.mark.asyncio
async def test_investing_prices_refresh_indian_mutual_fund_uses_amfi(client: AsyncClient):
    await _register_and_login(
        client,
        email="amfi-refresh@example.com",
        username="amfi-refresh",
        password="TestPass123!",
    )
    inr_broker_id = await _create_brokerage_account(client, "INR Brokerage", "INR")
    await _create_holding_via_order(
        client,
        inr_broker_id,
        "122639",
        "10.00000000",
        "80.00",
        "INR",
        "mutual_fund",
        "Axis Bluechip Fund Direct Growth",
    )

    with (
        patch("app.investing.service._fetch_stock_price", new_callable=AsyncMock) as stock_fetch,
        patch("app.investing.service._fetch_all_amfi_navs", new_callable=AsyncMock) as amfi_fetch,
    ):
        amfi_fetch.return_value = {"122639": (date(2026, 6, 19), Decimal("90.1404"))}
        refresh_res = await client.post("/v1/investing/prices/refresh")

    assert refresh_res.status_code == 200
    assert refresh_res.json()["updated"] == ["122639"]
    stock_fetch.assert_not_called()
    amfi_fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_bhavcopy_price_feed_job_prices_inr_stock_holding(client: AsyncClient):
    await _register_and_login(
        client,
        email="bhavcopy-hit@example.com",
        username="bhavcopy-hit",
        password="TestPass123!",
    )
    inr_broker_id = await _create_brokerage_account(client, "INR Brokerage", "INR")
    await _create_holding_via_order(
        client, inr_broker_id, "RELIANCE", "5.00000000", "2500.00", "INR"
    )

    with patch(
        "app.investing.service._fetch_nse_bhavcopy", new_callable=AsyncMock
    ) as bhavcopy_fetch:
        bhavcopy_fetch.return_value = {"RELIANCE": (date(2026, 6, 19), Decimal("2601.50"))}
        await bhavcopy_price_feed_job()

    holdings_res = await client.get("/v1/investing/holdings")
    holding = next(h for h in holdings_res.json()["items"] if h["symbol"] == "RELIANCE")
    assert Decimal(holding["current_price"]) == Decimal("2601.50")

    async with postgres.async_session_maker() as session:
        db_holding = (
            (
                await session.execute(
                    select(Holding).where(Holding.public_id == uuid.UUID(holding["public_id"]))
                )
            )
            .scalars()
            .one()
        )
        price_row = (
            (
                await session.execute(
                    select(HoldingPrice).where(HoldingPrice.holding_id == db_holding.id)
                )
            )
            .scalars()
            .one()
        )
        assert price_row.source == "bhavcopy"
        assert price_row.unit_price == Decimal("2601.500000")


@pytest.mark.asyncio
async def test_bhavcopy_price_feed_job_miss_falls_back_to_yahoo(client: AsyncClient):
    await _register_and_login(
        client,
        email="bhavcopy-miss@example.com",
        username="bhavcopy-miss",
        password="TestPass123!",
    )
    inr_broker_id = await _create_brokerage_account(client, "INR Brokerage", "INR")
    await _create_holding_via_order(client, inr_broker_id, "TCS", "3.00000000", "3800.00", "INR")

    with patch(
        "app.investing.service._fetch_nse_bhavcopy", new_callable=AsyncMock
    ) as bhavcopy_fetch:
        bhavcopy_fetch.return_value = {}
        await bhavcopy_price_feed_job()

    async with postgres.async_session_maker() as session:
        db_holding = (
            (await session.execute(select(Holding).where(Holding.symbol == "TCS"))).scalars().one()
        )
        price_rows = (
            (
                await session.execute(
                    select(HoldingPrice).where(HoldingPrice.holding_id == db_holding.id)
                )
            )
            .scalars()
            .all()
        )
        assert price_rows == []

    with patch("app.investing.service._fetch_stock_price", new_callable=AsyncMock) as stock_fetch:
        stock_fetch.return_value = (date(2026, 6, 19), Decimal("3900.00"))
        refresh_res = await client.post("/v1/investing/prices/refresh")

    assert refresh_res.status_code == 200
    assert "TCS" in refresh_res.json()["updated"]
    stock_fetch.assert_called_once()


@pytest.mark.asyncio
async def test_bhavcopy_price_feed_job_hit_prevents_yahoo_fallback(client: AsyncClient):
    await _register_and_login(
        client,
        email="bhavcopy-skip@example.com",
        username="bhavcopy-skip",
        password="TestPass123!",
    )
    inr_broker_id = await _create_brokerage_account(client, "INR Brokerage", "INR")
    await _create_holding_via_order(client, inr_broker_id, "INFY", "4.00000000", "1500.00", "INR")

    with patch(
        "app.investing.service._fetch_nse_bhavcopy", new_callable=AsyncMock
    ) as bhavcopy_fetch:
        bhavcopy_fetch.return_value = {"INFY": (date(2026, 6, 19), Decimal("1555.25"))}
        await bhavcopy_price_feed_job()

    with patch("app.investing.service._fetch_stock_price", new_callable=AsyncMock) as stock_fetch:
        refresh_res = await client.post("/v1/investing/prices/refresh")

    assert refresh_res.status_code == 200
    assert refresh_res.json()["updated"] == []
    stock_fetch.assert_not_called()

    holdings_res = await client.get("/v1/investing/holdings")
    holding = next(h for h in holdings_res.json()["items"] if h["symbol"] == "INFY")
    assert Decimal(holding["current_price"]) == Decimal("1555.25")


@pytest.mark.asyncio
async def test_bhavcopy_price_feed_job_skips_mutual_funds_and_non_inr(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="bhavcopy-guard@example.com",
        username="bhavcopy-guard",
        password="TestPass123!",
    )
    inr_broker_id = await _create_brokerage_account(client, "INR Brokerage", "INR")
    usd_broker_id = account_map["brokerage"]

    # Mutual fund with a symbol that happens to collide with a bhavcopy row.
    await _create_holding_via_order(
        client,
        inr_broker_id,
        "COLLIDE",
        "10.00000000",
        "100.00",
        "INR",
        "mutual_fund",
        "Colliding Fund Direct Growth",
    )
    # USD stock with a symbol that also happens to collide.
    await _create_holding_via_order(
        client, usd_broker_id, "COLLIDE", "10.00000000", "100.00", "USD"
    )

    with patch(
        "app.investing.service._fetch_nse_bhavcopy", new_callable=AsyncMock
    ) as bhavcopy_fetch:
        bhavcopy_fetch.return_value = {"COLLIDE": (date(2026, 6, 19), Decimal("999.99"))}
        await bhavcopy_price_feed_job()

    async with postgres.async_session_maker() as session:
        colliding_holdings = (
            (await session.execute(select(Holding).where(Holding.symbol == "COLLIDE")))
            .scalars()
            .all()
        )
        assert len(colliding_holdings) == 2
        for db_holding in colliding_holdings:
            price_rows = (
                (
                    await session.execute(
                        select(HoldingPrice).where(HoldingPrice.holding_id == db_holding.id)
                    )
                )
                .scalars()
                .all()
            )
            assert price_rows == []


@pytest.mark.asyncio
async def test_update_holding_symbol_relinks_instrument_and_preserves_fields(client: AsyncClient):
    await _register_and_login(
        client,
        email="symbol-edit@example.com",
        username="symbol-edit",
        password="TestPass123!",
    )
    inr_broker_id = await _create_brokerage_account(client, "INR Brokerage", "INR")
    created_id = await _create_holding_via_order(
        client, inr_broker_id, "NEFTPHARMA", "12.00000000", "25.00", "INR", "etf"
    )

    response = await client.patch(
        f"/v1/investing/holdings/{created_id}",
        json={"symbol": "PHARMABEES", "instrument_type": "etf"},
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["symbol"] == "PHARMABEES"
    assert updated["instrument_type"] == "etf"
    assert Decimal(updated["quantity"]) == Decimal("12.00000000")
    assert Decimal(updated["avg_cost"]) == Decimal("25.00")
    assert updated["currency"] == "INR"


@pytest.mark.asyncio
async def test_update_holding_symbol_deletes_existing_price_history(client: AsyncClient):
    await _register_and_login(
        client,
        email="symbol-price-reset@example.com",
        username="symbol-price-reset",
        password="TestPass123!",
    )
    inr_broker_id = await _create_brokerage_account(client, "INR Brokerage", "INR")
    created_id = await _create_holding_via_order(
        client,
        inr_broker_id,
        "122639",
        "10.00000000",
        "80.00",
        "INR",
        "mutual_fund",
        "Axis Bluechip Fund Direct Growth",
    )
    price_date = datetime.now(UTC).date().isoformat()
    assert (
        await client.post(
            "/v1/investing/prices",
            json={
                "price_date": price_date,
                "prices": [
                    {
                        "holding_public_id": created_id,
                        "unit_price": "90.1404",
                    }
                ],
            },
        )
    ).status_code == 201

    response = await client.patch(
        f"/v1/investing/holdings/{created_id}",
        json={"symbol": "122640", "instrument_type": "mutual_fund"},
    )

    assert response.status_code == 200
    async with postgres.async_session_maker() as session:
        holding = (
            await session.execute(select(Holding).where(Holding.public_id == uuid.UUID(created_id)))
        ).scalar_one()
        prices = (
            (
                await session.execute(
                    select(HoldingPrice).where(HoldingPrice.holding_id == holding.id)
                )
            )
            .scalars()
            .all()
        )
        assert prices == []


@pytest.mark.asyncio
async def test_update_holding_symbol_renames_linked_orders(client: AsyncClient):
    await _register_and_login(
        client,
        email="symbol-rename-orders@example.com",
        username="symbol-rename-orders",
        password="TestPass123!",
    )
    account_id = await _create_brokerage_account(client, "INR Brokerage", "INR")
    created_id = await _create_holding_via_order(
        client, account_id, "NEFTPHARMA", "12.00000000", "25.00", "INR", "etf"
    )
    # A second buy order against the same (wrong) symbol so the cascade is exercised
    # against more than one order row.
    cash_res = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_id,
            "balance": "1000.00",
            "currency": "INR",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    assert cash_res.status_code == 201, cash_res.text
    second_order_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": account_id,
            "order_type": "buy",
            "symbol": "NEFTPHARMA",
            "quantity": "3.00000000",
            "price_per_unit": "26.00",
            "currency": "INR",
            "occurred_at": datetime.now(UTC).isoformat(),
            "instrument_type": "etf",
        },
    )
    assert second_order_res.status_code == 201, second_order_res.text

    old_orders_res = await client.get(
        "/v1/investing/orders/by-holding/NEFTPHARMA", params={"account_id": account_id}
    )
    assert len(old_orders_res.json()) == 2

    response = await client.patch(
        f"/v1/investing/holdings/{created_id}",
        json={"symbol": "PHARMABEES", "instrument_type": "etf"},
    )
    assert response.status_code == 200
    assert response.json()["symbol"] == "PHARMABEES"

    renamed_orders_res = await client.get(
        "/v1/investing/orders/by-holding/PHARMABEES", params={"account_id": account_id}
    )
    assert renamed_orders_res.status_code == 200
    renamed_orders = renamed_orders_res.json()
    assert len(renamed_orders) == 2
    assert {o["symbol"] for o in renamed_orders} == {"PHARMABEES"}

    stale_orders_res = await client.get(
        "/v1/investing/orders/by-holding/NEFTPHARMA", params={"account_id": account_id}
    )
    assert stale_orders_res.json() == []


@pytest.mark.asyncio
async def test_update_holding_symbol_conflict_leaves_orders_untouched(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="symbol-rename-conflict@example.com",
        username="symbol-rename-conflict",
        password="TestPass123!",
    )
    account_id = account_map["brokerage"]
    created_id = await _create_holding_via_order(
        client, account_id, "TARGETSYM", "5.00000000", "10.00", "USD", "stock"
    )
    await _create_holding_via_order(
        client, account_id, "TAKENSYM", "1.00000000", "1.00", "USD", "stock"
    )

    response = await client.patch(
        f"/v1/investing/holdings/{created_id}",
        json={"symbol": "TAKENSYM", "instrument_type": "stock"},
    )
    assert response.status_code == 409

    untouched_orders_res = await client.get(
        "/v1/investing/orders/by-holding/TARGETSYM", params={"account_id": account_id}
    )
    assert len(untouched_orders_res.json()) == 1


@pytest.mark.asyncio
async def test_update_order_holding_rejects_quantity_and_avg_cost_edit(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="symbol-rename-reject-qty@example.com",
        username="symbol-rename-reject-qty",
        password="TestPass123!",
    )
    account_id = account_map["brokerage"]
    created_id = await _create_holding_via_order(
        client, account_id, "REJECTQTY", "5.00000000", "10.00", "USD", "stock"
    )

    response = await client.patch(
        f"/v1/investing/holdings/{created_id}",
        json={"symbol": "REJECTQTY2", "instrument_type": "stock", "quantity": "99.00000000"},
    )
    assert response.status_code == 422

    holding_res = await client.get("/v1/investing/holdings")
    holding = next(h for h in holding_res.json()["items"] if h["public_id"] == created_id)
    assert holding["symbol"] == "REJECTQTY"
    assert Decimal(holding["quantity"]) == Decimal("5.00000000")


@pytest.mark.asyncio
async def test_update_order_holding_rejects_quantity_edit_without_symbol_change(
    client: AsyncClient,
):
    """Regression test: the quantity/avg_cost guard must not be bypassable by leaving
    the symbol unchanged (only the instrument_type or no other field changing)."""
    account_map = await _register_and_login(
        client,
        email="symbol-rename-reject-qty-only@example.com",
        username="symbol-rename-reject-qty-only",
        password="TestPass123!",
    )
    account_id = account_map["brokerage"]
    created_id = await _create_holding_via_order(
        client, account_id, "REJECTQTYONLY", "5.00000000", "10.00", "USD", "stock"
    )

    response = await client.patch(
        f"/v1/investing/holdings/{created_id}",
        json={"quantity": "99.00000000"},
    )
    assert response.status_code == 422

    holding_res = await client.get("/v1/investing/holdings")
    holding = next(h for h in holding_res.json()["items"] if h["public_id"] == created_id)
    assert Decimal(holding["quantity"]) == Decimal("5.00000000")


@pytest.mark.asyncio
async def test_update_holding_symbol_then_new_order_recomputes_renamed_holding(
    client: AsyncClient,
):
    account_map = await _register_and_login(
        client,
        email="symbol-rename-recompute@example.com",
        username="symbol-rename-recompute",
        password="TestPass123!",
    )
    account_id = account_map["brokerage"]
    created_id = await _create_holding_via_order(
        client, account_id, "OLDSYM", "5.00000000", "10.00", "USD", "stock"
    )

    response = await client.patch(
        f"/v1/investing/holdings/{created_id}",
        json={"symbol": "NEWSYM", "instrument_type": "stock"},
    )
    assert response.status_code == 200

    cash_res = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_id,
            "balance": "1000.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    assert cash_res.status_code == 201, cash_res.text
    second_order_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": account_id,
            "order_type": "buy",
            "symbol": "NEWSYM",
            "quantity": "5.00000000",
            "price_per_unit": "12.00",
            "currency": "USD",
            "occurred_at": datetime.now(UTC).isoformat(),
            "instrument_type": "stock",
        },
    )
    assert second_order_res.status_code == 201, second_order_res.text

    holdings_res = await client.get("/v1/investing/holdings")
    matching = [
        h
        for h in holdings_res.json()["items"]
        if h["account_id"] == account_id and h["symbol"] in ("OLDSYM", "NEWSYM")
    ]
    assert len(matching) == 1
    assert matching[0]["public_id"] == created_id
    assert matching[0]["symbol"] == "NEWSYM"
    assert Decimal(matching[0]["quantity"]) == Decimal("10.00000000")


@pytest.mark.asyncio
async def test_delete_holding_cascades_existing_price_history(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="holding-delete-prices@example.com",
        username="holding-delete-prices",
        password="TestPass123!",
    )
    created_id = await _create_holding_via_order(
        client, account_map["brokerage"], "AAPL", "2.00000000", "150.00"
    )
    assert (
        await client.post(
            "/v1/investing/prices",
            json={
                "price_date": datetime.now(UTC).date().isoformat(),
                "prices": [
                    {
                        "holding_public_id": created_id,
                        "unit_price": "180.00",
                    }
                ],
            },
        )
    ).status_code == 201

    response = await client.delete(f"/v1/investing/holdings/{created_id}")

    assert response.status_code == 204
    async with postgres.async_session_maker() as session:
        holding = (
            await session.execute(select(Holding).where(Holding.public_id == uuid.UUID(created_id)))
        ).scalar_one_or_none()
        prices = (await session.execute(select(HoldingPrice))).scalars().all()
        assert holding is None
        assert prices == []


@pytest.mark.asyncio
async def test_hybrid_catalog_lookup_and_override(client: AsyncClient):
    # Register workspace A and workspace B
    await _register_and_login(
        client,
        email="hybrid-a@example.com",
        username="hybrid-a",
        password="TestPass123!",
    )

    # Create a workspace-scoped instrument in A (doesn't exist in Yahoo Finance, so we make a private override or custom instrument)
    etf_res_a = await client.post(
        "/v1/investing/instruments",
        json={
            "symbol": "PVT_HOLDING",
            "name": "Workspace A Private Fund",
            "instrument_type": "etf",
        },
    )
    assert etf_res_a.status_code == 201

    # Create a global instrument (e.g. GLOBAL_MSFT - Yahoo Finance mocked/stubbed to return something)
    with patch("app.investing.service._fetch_stock_price", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = (date(2026, 6, 24), Decimal("400.00"))

        global_res = await client.post(
            "/v1/investing/instruments",
            json={
                "symbol": "GLOBAL_MSFT",
                "name": "Global Microsoft",
                "instrument_type": "stock",
            },
        )
        assert global_res.status_code == 201
        assert global_res.json()["symbol"] == "GLOBAL_MSFT"

        # Verify it has no workspace_id in the database
        async with postgres.async_session_maker() as session:
            db_inst = (
                await session.execute(select(Instrument).where(Instrument.symbol == "GLOBAL_MSFT"))
            ).scalar_one()
            assert db_inst.workspace_id is None

            db_pvt = (
                await session.execute(select(Instrument).where(Instrument.symbol == "PVT_HOLDING"))
            ).scalar_one()
            assert db_pvt.workspace_id is not None

    # Now logout and login to workspace B
    await client.post("/v1/auth/logout")
    await _register_and_login(
        client,
        email="hybrid-b@example.com",
        username="hybrid-b",
        password="TestPass123!",
    )

    # List instruments for B: should see GLOBAL_MSFT, but NOT PVT_HOLDING
    list_res = await client.get("/v1/investing/instruments")
    assert list_res.status_code == 200
    symbols = {item["symbol"] for item in list_res.json()}
    assert "GLOBAL_MSFT" in symbols
    assert "PVT_HOLDING" not in symbols


async def test_investing_summary_market_price_and_daily_change(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="investing-mp@example.com",
        username="investing-mp",
        password="TestPass123!",
    )

    # 1. Create a holding via buy order (10 shares @ $200 = $2000)
    holding_public_id = await _create_holding_via_order(
        client,
        account_map["brokerage"],
        "MSFT",
        "10.00000000",
        "200.00",
        "USD",
    )

    # 2. Verify summary initially uses cost basis fallback (since no price exists)
    summary_res = await client.get("/v1/investing/summary")
    assert summary_res.status_code == 200
    assert summary_res.json()["valuation_status"] == "cost_basis_fallback"
    assert summary_res.json()["portfolio_value"] == "2000.00000000000000"

    # Fetch DB ids
    async with postgres.async_session_maker() as session:
        # Get workspace id and holding db id
        db_holding = (
            await session.execute(
                select(Holding).where(Holding.public_id == uuid.UUID(holding_public_id))
            )
        ).scalar_one()
        workspace_id = db_holding.workspace_id
        holding_db_id = db_holding.id

        # Insert a portfolio snapshot for yesterday (today - 1 day)
        yesterday = datetime.now(UTC).date() - timedelta(days=1)
        snapshot = PortfolioSnapshot(
            workspace_id=workspace_id,
            snapshot_date=yesterday,
            total_value=Decimal("1500.00"),
            total_cost=Decimal("1500.00"),
            holdings_value=Decimal("1500.00"),
            cash_value=Decimal("0.00"),
            currency_code="USD",
        )
        session.add(snapshot)

        # Insert a holding price for today
        today = datetime.now(UTC).date()
        price = HoldingPrice(
            workspace_id=workspace_id,
            holding_id=holding_db_id,
            price_date=today,
            unit_price=Decimal("250.00"),
            source="manual",
        )
        session.add(price)
        await session.commit()

    # 3. Verify summary now uses market price (10 * 250 = 2500) and calculates daily_change (2500 - 1500 = 1000)
    # and valuation_status is single_currency_native (since all holdings have prices)
    summary_res = await client.get("/v1/investing/summary")
    assert summary_res.status_code == 200
    summary = summary_res.json()
    assert summary["valuation_status"] == "single_currency_native"
    assert Decimal(summary["portfolio_value"]) == Decimal("2500.00")
    assert Decimal(summary["daily_change"]) == Decimal("1000.00")


@pytest.mark.asyncio
async def test_fifo_cost_basis_matches_broker_lot_consumption(client: AsyncClient):
    """Reproduces the production discrepancy from spec-044 (Bandhan ELSS Tax
    Saver Fund vs Groww): three buys then a sell that exactly exhausts the
    first two lots should leave avg_cost as just the third lot's price, not
    a moving average blended across all three buys.
    """
    await _register_and_login(
        client,
        email="fifo-e2e@example.com",
        username="fifo-e2e",
        password="TestPass123!",
    )
    account_id = await _create_brokerage_account(client, "INR Brokerage", "INR")

    cash_res = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_id,
            "balance": "100000.00",
            "currency": "INR",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    assert cash_res.status_code == 201, cash_res.text

    async def _place(order_type: str, quantity: str, price: str, occurred_at: datetime) -> dict:
        res = await client.post(
            "/v1/investing/orders",
            json={
                "account_id": account_id,
                "order_type": order_type,
                "symbol": "BANDHANELSS",
                "quantity": quantity,
                "price_per_unit": price,
                "currency": "INR",
                "occurred_at": occurred_at.isoformat(),
                "instrument_type": "mutual_fund",
                "instrument_name": "Bandhan ELSS Tax Saver Fund Direct Plan Growth",
            },
        )
        assert res.status_code == 201, res.text
        return res.json()

    await _place("buy", "180.573", "110.75", datetime(2022, 8, 18, tzinfo=UTC))
    await _place("buy", "119.528", "108.76", datetime(2022, 10, 14, tzinfo=UTC))
    await _place("buy", "140.319", "142.53", datetime(2023, 12, 12, tzinfo=UTC))
    sell = await _place("sell", "300.101", "182.30", datetime(2025, 12, 19, tzinfo=UTC))

    holdings_res = await client.get("/v1/investing/holdings")
    assert holdings_res.status_code == 200
    holding = next(h for h in holdings_res.json()["items"] if h["symbol"] == "BANDHANELSS")
    assert Decimal(holding["quantity"]) == Decimal("140.319")
    # FIFO: the sell fully consumed the first two lots, leaving only the
    # third lot's own price as the open cost basis — matching Groww's
    # "Avg. NAV" of 142.53 in the spec-044 production comparison, not a
    # moving average across all three buys (which would be ~120.33).
    assert Decimal(holding["avg_cost"]) == Decimal("142.53")

    assert Decimal(sell["realized_gain_loss"]) == (
        Decimal("180.573") * (Decimal("182.30") - Decimal("110.75"))
        + Decimal("119.528") * (Decimal("182.30") - Decimal("108.76"))
    ).quantize(Decimal("0.01"))


@pytest.mark.asyncio
async def test_orders_search_matches_symbol_and_instrument_name(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="orders-search@example.com",
        username="orders-search",
        password="TestPass123!",
    )
    # Mutual fund whose symbol is a numeric folio — only findable by name.
    await _create_holding_via_order(
        client,
        account_map["brokerage"],
        "152981",
        "100.00000000",
        "9.08",
        instrument_type="mutual_fund",
        instrument_name="Edelweiss Nifty500 Momentum",
    )
    await _create_holding_via_order(
        client, account_map["brokerage"], "NVDA", "1.00000000", "100.00"
    )

    # Substring of the symbol (exact-match would have failed on "NV").
    res = await client.get("/v1/investing/orders", params={"search": "nv"})
    assert res.status_code == 200
    assert {o["symbol"] for o in res.json()["items"]} == {"NVDA"}

    # Substring of the instrument name finds the numeric-symbol mutual fund.
    res = await client.get("/v1/investing/orders", params={"search": "edelweiss"})
    assert {o["symbol"] for o in res.json()["items"]} == {"152981"}

    # No match returns nothing.
    res = await client.get("/v1/investing/orders", params={"search": "zzzznope"})
    assert res.json()["items"] == []


@pytest.mark.asyncio
async def test_net_worth_lists_brokerage_cash_accounts(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="networth-cash@example.com",
        username="networth-cash",
        password="TestPass123!",
    )
    # Buying leaves residual cash (helper funds cost + 1000) on the brokerage account.
    await _create_holding_via_order(
        client, account_map["brokerage"], "AAPL", "1.00000000", "100.00"
    )

    res = await client.get("/v1/finance/net-worth")
    assert res.status_code == 200
    data = res.json()
    account = next(
        a for a in data["investing_accounts"] if a["account_public_id"] == account_map["brokerage"]
    )
    assert account["currency_code"] == "USD"
    assert Decimal(account["balance"]) == Decimal("1000.00")


@pytest.mark.asyncio
async def test_networth_multi_currency_does_not_double_count_cash(client: AsyncClient):
    """Regression: in the FX-converted path, ``portfolio_value`` is holdings-only
    (cash is reported separately as ``cash_total``). It previously folded cash
    into ``portfolio_value``, which made net worth double-count cash because the
    net-worth router adds ``cash_total`` to ``holdings_value``.
    """
    account_map = await _register_and_login(
        client,
        email="nw-doublecount@example.com",
        username="nw-doublecount",
        password="TestPass123!",
    )
    # USD holding: 10 @ 100 = 1000 cost; the helper funds cost + 1000, so the
    # buy leaves 1000 USD residual cash. No price -> cost-basis fallback (1000).
    await _create_holding_via_order(
        client, account_map["brokerage"], "AAPL", "10.00000000", "100.00"
    )

    # Report in INR and seed USD -> INR = 80 so the FX-converted path runs.
    setting_res = await client.patch(
        "/v1/finance/settings", json={"reporting_currency_code": "INR"}
    )
    assert setting_res.status_code == 200, setting_res.text

    now = datetime.now(UTC)
    async with postgres.async_session_maker() as session:
        session.add(
            FxRate(
                base_currency_code="USD",
                quote_currency_code="INR",
                rate=Decimal("80.0000000000"),
                # spec-075: display conversion uses the previous day's close.
                as_of=now - timedelta(days=1),
                fetched_at=now,
                source="test",
            )
        )
        await session.commit()

    summary = (await client.get("/v1/investing/summary")).json()
    assert summary["reporting_currency"] == "INR"
    # holdings 1000 USD -> 80000 INR; cash 1000 USD -> 80000 INR, reported
    # separately and NOT folded into portfolio_value.
    assert Decimal(summary["portfolio_value"]) == Decimal("80000")
    assert Decimal(summary["cash_total"]) == Decimal("80000")

    nw = (await client.get("/v1/finance/net-worth")).json()
    assert Decimal(nw["holdings_value"]) == Decimal("80000")  # holdings only
    assert Decimal(nw["investing_cash_total"]) == Decimal("80000")
    # holdings + cash, NOT holdings + 2 * cash (the double-count bug).
    assert Decimal(nw["investing_total"]) == Decimal("160000")


@pytest.mark.asyncio
async def test_networth_conversion_ignores_same_day_rate_uses_previous_close(
    client: AsyncClient,
):
    """spec-075: display conversion always uses the *previous* calendar
    day's close, never a same-day/live rate -- one rate per day, for
    historical and "current" views alike. A rate stamped ``today`` must be
    ignored even when it is the only USD->INR row in the table; only a rate
    dated yesterday (or earlier) may be picked up."""
    account_map = await _register_and_login(
        client,
        email="nw-fx-asof@example.com",
        username="nw-fx-asof",
        password="TestPass123!",
    )
    await _create_holding_via_order(
        client, account_map["brokerage"], "AAPL", "10.00000000", "100.00"
    )
    setting_res = await client.patch(
        "/v1/finance/settings", json={"reporting_currency_code": "INR"}
    )
    assert setting_res.status_code == 200, setting_res.text

    now = datetime.now(UTC)
    async with postgres.async_session_maker() as session:
        session.add(
            FxRate(
                base_currency_code="USD",
                quote_currency_code="INR",
                rate=Decimal("90.0000000000"),
                as_of=now,  # today -- must be ignored
                fetched_at=now,
                source="test",
            )
        )
        await session.commit()

    # No rate dated yesterday-or-earlier exists yet: conversion is unavailable.
    nw = (await client.get("/v1/finance/net-worth")).json()
    assert nw["valuation_status"] != "ok"
    assert nw["total_net_worth"] is None

    yesterday = now - timedelta(days=1)
    async with postgres.async_session_maker() as session:
        session.add(
            FxRate(
                base_currency_code="USD",
                quote_currency_code="INR",
                rate=Decimal("80.0000000000"),
                as_of=yesterday,
                fetched_at=now,
                source="test",
            )
        )
        await session.commit()

    # Now that yesterday's close exists, conversion uses it (80), not
    # today's 90 -- even though today's row is more recent.
    nw = (await client.get("/v1/finance/net-worth")).json()
    assert nw["valuation_status"] == "ok"
    assert Decimal(nw["investing_cash_total"]) == Decimal("80000")


# ---------------------------------------------------------------------------
# spec-050: one account, one currency -- cash balances, orders and transfers
# must match the account's default_currency_code. Plus the net-worth
# aggregation fix (non-brokerage cash balances no longer counted in
# investing_cash_total) and the account_id filter on GET /cash-balances.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_cash_balance_rejects_currency_mismatch(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="cash-currency-mismatch@example.com",
        username="cash-currency-mismatch",
        password="TestPass123!",
    )
    res = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["brokerage"],  # USD
            "balance": "100.00",
            "currency": "INR",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    assert res.status_code == 422
    assert "does not match account" in res.json()["detail"]


@pytest.mark.asyncio
async def test_create_cash_balance_allows_any_account_type_when_currency_matches(
    client: AsyncClient,
):
    """Reconciliation depends on this: a bank/wallet account must still be able
    to have a cash-balance snapshot (spec-050 restricts currency, not account type)."""
    account_map = await _register_and_login(
        client,
        email="cash-any-account-type@example.com",
        username="cash-any-account-type",
        password="TestPass123!",
    )
    res = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["wallet"],  # USD, account_type="brokerage" per helper
            "balance": "100.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    assert res.status_code == 201, res.text


@pytest.mark.asyncio
async def test_update_cash_balance_rejects_currency_mismatch(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="cash-update-currency-mismatch@example.com",
        username="cash-update-currency-mismatch",
        password="TestPass123!",
    )
    created = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["brokerage"],
            "balance": "100.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    assert created.status_code == 201
    public_id = created.json()["public_id"]

    res = await client.patch(f"/v1/investing/cash-balances/{public_id}", json={"currency": "INR"})
    assert res.status_code == 422
    assert "does not match account" in res.json()["detail"]

    # Patching an unrelated field is unaffected.
    ok = await client.patch(f"/v1/investing/cash-balances/{public_id}", json={"balance": "150.00"})
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_place_order_rejects_currency_mismatch(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="order-currency-mismatch@example.com",
        username="order-currency-mismatch",
        password="TestPass123!",
    )
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["brokerage"],
            "balance": "10000.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": account_map["brokerage"],  # USD
            "order_type": "buy",
            "symbol": "TCS",
            "quantity": "1.00000000",
            "price_per_unit": "100.00",
            "currency": "INR",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
    )
    assert res.status_code == 422
    assert "does not match account" in res.json()["detail"]


@pytest.mark.asyncio
async def test_create_transfer_rejects_currency_mismatch_either_side(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="transfer-currency-mismatch@example.com",
        username="transfer-currency-mismatch",
        password="TestPass123!",
    )
    # to_currency_code (INR) doesn't match the brokerage's USD.
    res = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": "spending",
            "to_module": "investing",
            "from_account_id": account_map["wallet"],
            "to_account_id": account_map["brokerage"],
            "from_currency_code": "USD",
            "to_currency_code": "INR",
            "gross_amount": "100.00",
            "net_amount_received": "100.00",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
    )
    assert res.status_code == 422
    assert "does not match account" in res.json()["detail"]


@pytest.mark.asyncio
async def test_update_transfer_rejects_currency_mismatch_but_allows_unrelated_edits(
    client: AsyncClient,
):
    account_map = await _register_and_login(
        client,
        email="transfer-update-currency@example.com",
        username="transfer-update-currency",
        password="TestPass123!",
    )
    transfer = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": "spending",
            "to_module": "investing",
            "from_account_id": account_map["wallet"],
            "to_account_id": account_map["brokerage"],
            "from_currency_code": "USD",
            "to_currency_code": "USD",
            "gross_amount": "100.00",
            "net_amount_received": "100.00",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
    )
    assert transfer.status_code == 201
    transfer_id = transfer.json()["public_id"]

    mismatched = await client.patch(
        f"/v1/finance/transfers/{transfer_id}", json={"to_currency_code": "INR"}
    )
    assert mismatched.status_code == 422
    assert "does not match account" in mismatched.json()["detail"]

    # Unrelated field edit still works.
    ok = await client.patch(f"/v1/finance/transfers/{transfer_id}", json={"notes": "corrected"})
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_networth_does_not_double_count_wallet_cash_balance_snapshot(
    client: AsyncClient,
):
    """Regression: a cash-balance snapshot on a non-brokerage account (added
    for reconciliation ground truth, e.g. backfilling pre-tracking history)
    must not also be summed into investing_cash_total -- that account's
    ledger contribution (spending_total) already counts it once."""
    await _register_and_login(
        client,
        email="networth-wallet-cash@example.com",
        username="networth-wallet-cash",
        password="TestPass123!",
    )
    settings_res = await client.patch(
        "/v1/finance/settings", json={"reporting_currency_code": "USD"}
    )
    assert settings_res.status_code == 200, settings_res.text

    # _register_and_login's account_map is all account_type="brokerage"
    # regardless of name, so this needs a genuinely non-brokerage account.
    wallet_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "ICICI", "account_type": "wallet", "default_currency_code": "USD"},
    )
    assert wallet_res.status_code == 201
    wallet_id = wallet_res.json()["public_id"]

    cat_res = await client.get("/v1/spending/categories")
    category_id = cat_res.json()["items"][0]["public_id"]

    await client.post(
        "/v1/spending/transactions",
        json={
            "amount": "5000.00",
            "type": "income",
            "category_id": category_id,
            "account_id": wallet_id,
            "occurred_at": datetime.now(UTC).isoformat(),
        },
    )
    # Backfill a cash-balance snapshot on the wallet (non-brokerage) account.
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": wallet_id,
            "balance": "5000.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )

    nw = (await client.get("/v1/finance/net-worth")).json()
    # spending_total already includes the wallet's 5000 via the ledger; the
    # cash-balance snapshot must not add it again via investing_cash_total.
    assert Decimal(nw["spending_total"]) == Decimal("5000.00")
    assert Decimal(nw["investing_cash_total"] or "0") == Decimal("0")
    assert Decimal(nw["total_net_worth"]) == Decimal("5000.00")


@pytest.mark.asyncio
async def test_cash_balances_account_id_filter_scopes_server_side(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="cash-balances-account-filter@example.com",
        username="cash-balances-account-filter",
        password="TestPass123!",
    )
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["brokerage"],
            "balance": "100.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["wallet"],
            "balance": "200.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )

    all_res = await client.get("/v1/investing/cash-balances")
    assert all_res.json()["total"] == 2

    filtered_res = await client.get(
        "/v1/investing/cash-balances", params={"account_id": account_map["wallet"]}
    )
    assert filtered_res.status_code == 200
    filtered = filtered_res.json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["account_id"] == account_map["wallet"]
    assert filtered["items"][0]["balance"] == "200.00"


@pytest.mark.asyncio
async def test_update_order_writes_single_combined_cash_balance_snapshot(client: AsyncClient):
    """update_order should reverse the old cash impact and apply the new one
    as a single combined delta -- one new CashBalance row per edit, not two."""
    account_map = await _register_and_login(
        client,
        email="update-order-single-snapshot@example.com",
        username="update-order-single-snapshot",
        password="TestPass123!",
    )
    account_id = account_map["brokerage"]

    cash_res = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_id,
            "balance": "10000.00",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    assert cash_res.status_code == 201, cash_res.text

    order_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": account_id,
            "order_type": "buy",
            "symbol": "AAPL",
            "quantity": "10.00000000",
            "price_per_unit": "100.00",
            "currency": "USD",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
    )
    assert order_res.status_code == 201, order_res.text
    order = order_res.json()
    order_id = order["public_id"]

    async with postgres.async_session_maker() as session:
        pre_update_count = (
            (
                await session.execute(
                    select(CashBalance).where(CashBalance.trigger_ref == uuid.UUID(order_id))
                )
            )
            .scalars()
            .all()
        )
    assert len(pre_update_count) == 1

    # Edit the order's quantity -- this reverses the old cash impact (+1000
    # buy reversal) and applies the new one (-1500 for 15 units @ 100) in a
    # single combined delta.
    update_res = await client.patch(
        f"/v1/investing/orders/{order_id}",
        json={"quantity": "15.00000000"},
    )
    assert update_res.status_code == 200, update_res.text

    async with postgres.async_session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(CashBalance)
                    .where(CashBalance.trigger_ref == uuid.UUID(order_id))
                    .order_by(CashBalance.created_at)
                )
            )
            .scalars()
            .all()
        )
    # One row from order placement + exactly ONE new row from the edit (was 2).
    assert len(rows) == 2
    final_balance = rows[-1].balance
    # 10000 starting cash - 1500 (15 * 100) = 8500
    assert final_balance == Decimal("8500.00")


@pytest.mark.asyncio
async def test_find_or_create_instrument_normalizes_symbol_case(client: AsyncClient):
    """find_or_create_instrument must normalize the symbol to uppercase at the
    very start, so callers that bypass the InvestingOrderCreate schema's own
    normalize_symbol validator (e.g. internal/bulk-import callers) still
    resolve 'aapl' and 'AAPL' to the same Instrument record."""
    account_map = await _register_and_login(
        client,
        email="symbol-case-normalize@example.com",
        username="symbol-case-normalize",
        password="TestPass123!",
    )

    async with postgres.async_session_maker() as session:
        account = (
            await session.execute(
                select(Account).where(Account.public_id == uuid.UUID(account_map["brokerage"]))
            )
        ).scalar_one()
        workspace_id = account.workspace_id

        service = InstrumentService(InstrumentRepository(session), CompanyRepository(session))
        lower = await service.find_or_create_instrument(workspace_id, "aapl", InstrumentType.stock)
        await session.commit()
        assert lower is not None
        assert lower.symbol == "AAPL"

        upper = await service.find_or_create_instrument(workspace_id, "AAPL", InstrumentType.stock)
        await session.commit()
        assert upper is not None
        assert upper.id == lower.id

        instruments = (
            (await session.execute(select(Instrument).where(Instrument.symbol == "AAPL")))
            .scalars()
            .all()
        )
    assert len(instruments) == 1


# ---------------------------------------------------------------------------
# Spec-051: corporate actions (splits, reverse splits, bonus issues)
# ---------------------------------------------------------------------------


async def _cash_balance_count(client: AsyncClient, account_id: str) -> int:
    res = await client.get("/v1/investing/cash-balances", params={"account_id": account_id})
    assert res.status_code == 200, res.text
    return res.json()["total"]


@pytest.mark.asyncio
async def test_corporate_action_split_scales_lot_and_allows_post_split_sell(client: AsyncClient):
    """Spec-051 golden scenario 1 (2:1 split).

    Today, without a recorded split, a sell for the true post-split share
    count fails with a negative-holding ValidationError because the replay
    still thinks the position is the smaller, un-split quantity. This proves
    the fix: the split brings the holding to 20 @ avg_cost 500 (10 @ 1000
    scaled 2x qty / 0.5x cost, same total cost of 10,000), the post-split sell
    of all 20 shares succeeds, and its realized gain matches exactly what the
    old manual-edit workaround (rewriting the buy to qty=20, price=500) would
    have produced: 20 * (600 - 500) = 2,000.00.
    """
    account_map = await _register_and_login(
        client, email="split-basic@example.com", username="split-basic", password="TestPass123!"
    )
    broker_id = account_map["brokerage"]

    await _create_holding_via_order(
        client, broker_id, "AAPL", "10.00000000", "1000.00", occurred_at="2026-01-15T10:00:00Z"
    )

    holdings_before = (await client.get("/v1/investing/holdings")).json()["items"]
    pre_split = next(h for h in holdings_before if h["symbol"] == "AAPL")
    assert Decimal(pre_split["quantity"]) == Decimal("10.00000000")
    assert Decimal(pre_split["avg_cost"]) == Decimal("1000.000000")

    count_before_split = await _cash_balance_count(client, broker_id)

    split_res = await client.post(
        "/v1/investing/corporate-actions",
        json={
            "account_id": broker_id,
            "symbol": "AAPL",
            "action_type": "split",
            "ratio_base": "1",
            "ratio_quote": "2",
            "ex_date": "2026-03-01",
        },
    )
    assert split_res.status_code == 201, split_res.text
    action_public_id = split_res.json()["public_id"]

    # Cash neutrality: the split itself writes no investing_cash_balances row.
    assert await _cash_balance_count(client, broker_id) == count_before_split

    holdings_after_split = (await client.get("/v1/investing/holdings")).json()["items"]
    post_split = next(h for h in holdings_after_split if h["symbol"] == "AAPL")
    assert Decimal(post_split["quantity"]) == Decimal("20.00000000")
    assert Decimal(post_split["avg_cost"]) == Decimal("500.000000")

    # Idempotent replay: deleting the split (before any order depends on the
    # post-split quantity) must exactly reverse it.
    delete_res = await client.delete(f"/v1/investing/corporate-actions/{action_public_id}")
    assert delete_res.status_code == 204, delete_res.text
    holdings_after_delete = (await client.get("/v1/investing/holdings")).json()["items"]
    reverted = next(h for h in holdings_after_delete if h["symbol"] == "AAPL")
    assert Decimal(reverted["quantity"]) == Decimal("10.00000000")
    assert Decimal(reverted["avg_cost"]) == Decimal("1000.000000")

    # Re-record the same split to continue the scenario.
    split_res_2 = await client.post(
        "/v1/investing/corporate-actions",
        json={
            "account_id": broker_id,
            "symbol": "AAPL",
            "action_type": "split",
            "ratio_base": "1",
            "ratio_quote": "2",
            "ex_date": "2026-03-01",
        },
    )
    assert split_res_2.status_code == 201, split_res_2.text
    count_before_sell = await _cash_balance_count(client, broker_id)

    # Today (pre-fix) this sell would raise ValidationError: the replay still
    # believes the holding is 10 shares.
    sell_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": broker_id,
            "order_type": "sell",
            "symbol": "AAPL",
            "quantity": "20.00000000",
            "price_per_unit": "600.00",
            "currency": "USD",
            "occurred_at": "2026-06-01T10:00:00Z",
        },
    )
    assert sell_res.status_code == 201, sell_res.text
    sell_data = sell_res.json()
    assert Decimal(sell_data["realized_gain_loss"]) == Decimal("2000.00")
    assert Decimal(sell_data["avg_cost_at_sale"]) == Decimal("500.000000")

    # The sell (and only the sell) writes a cash-balance row here.
    assert await _cash_balance_count(client, broker_id) == count_before_sell + 1

    holdings_final = (await client.get("/v1/investing/holdings")).json()["items"]
    closed = next(h for h in holdings_final if h["symbol"] == "AAPL")
    assert Decimal(closed["quantity"]) == Decimal("0")
    assert Decimal(closed["avg_cost"]) == Decimal("0.000000")


@pytest.mark.asyncio
async def test_corporate_action_reverse_split_scales_lot_down(client: AsyncClient):
    """Spec-051 golden scenario 2: a reverse split is the same transform as a
    forward split, just with ratio_base > ratio_quote (1-for-10 here)."""
    account_map = await _register_and_login(
        client,
        email="reverse-split@example.com",
        username="reverse-split",
        password="TestPass123!",
    )
    broker_id = account_map["brokerage"]

    await _create_holding_via_order(
        client, broker_id, "PENNY", "100.00000000", "50.00", occurred_at="2026-01-15T10:00:00Z"
    )

    reverse_split_res = await client.post(
        "/v1/investing/corporate-actions",
        json={
            "account_id": broker_id,
            "symbol": "PENNY",
            "action_type": "split",
            "ratio_base": "10",
            "ratio_quote": "1",
            "ex_date": "2026-03-01",
        },
    )
    assert reverse_split_res.status_code == 201, reverse_split_res.text

    holdings = (await client.get("/v1/investing/holdings")).json()["items"]
    holding = next(h for h in holdings if h["symbol"] == "PENNY")
    # Same total cost (100 * 50 = 5,000), 1/10th the shares: 10 @ 500.00.
    assert Decimal(holding["quantity"]) == Decimal("10.00000000")
    assert Decimal(holding["avg_cost"]) == Decimal("500.000000")


@pytest.mark.asyncio
async def test_corporate_action_bonus_issue_creates_zero_cost_lot(client: AsyncClient):
    """Spec-051 golden scenario 3: a bonus issue creates a *new* zero-cost lot
    (distinct acquired_at from the original buy), not a scaling transform on
    the existing lot — the tax treatment differs from a split (nil cost
    basis, holding period starts at allotment). A sell crossing both lots via
    FIFO nets the loss on the original lot against the full-gain bonus lot."""
    account_map = await _register_and_login(
        client, email="bonus-basic@example.com", username="bonus-basic", password="TestPass123!"
    )
    broker_id = account_map["brokerage"]

    holding_public_id = await _create_holding_via_order(
        client, broker_id, "BONUSCO", "10.00000000", "1000.00", occurred_at="2026-01-15T10:00:00Z"
    )

    count_before_bonus = await _cash_balance_count(client, broker_id)

    bonus_res = await client.post(
        "/v1/investing/corporate-actions",
        json={
            "account_id": broker_id,
            "symbol": "BONUSCO",
            "action_type": "bonus",
            "ratio_base": "2",
            "ratio_quote": "1",
            "ex_date": "2026-03-01",
        },
    )
    assert bonus_res.status_code == 201, bonus_res.text

    # Cash neutrality: the bonus issue itself writes no investing_cash_balances row.
    assert await _cash_balance_count(client, broker_id) == count_before_bonus

    async with postgres.async_session_maker() as session:
        holding = (
            await session.execute(
                select(Holding).where(Holding.public_id == uuid.UUID(holding_public_id))
            )
        ).scalar_one()
        lots = (
            (await session.execute(select(OrderLot).where(OrderLot.holding_id == holding.id)))
            .scalars()
            .all()
        )
    assert len(lots) == 2
    buy_lot = next(lot for lot in lots if lot.buy_order_id is not None)
    bonus_lot = next(lot for lot in lots if lot.corporate_action_id is not None)
    assert buy_lot.acquired_at != bonus_lot.acquired_at
    assert bonus_lot.cost_per_unit == Decimal("0")
    assert bonus_lot.remaining_quantity == Decimal("5.00000000")  # 10 held * (1/2)

    sell_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": broker_id,
            "order_type": "sell",
            "symbol": "BONUSCO",
            "quantity": "12.00000000",
            "price_per_unit": "800.00",
            "currency": "USD",
            "occurred_at": "2026-06-01T10:00:00Z",
        },
    )
    assert sell_res.status_code == 201, sell_res.text
    # FIFO: 10 from the original lot (loss of 200/share) + 2 from the
    # zero-cost bonus lot (full 800/share gain): 10*(800-1000) + 2*(800-0).
    assert Decimal(sell_res.json()["realized_gain_loss"]) == Decimal("-400.00")

    holdings = (await client.get("/v1/investing/holdings")).json()["items"]
    holding_after_sell = next(h for h in holdings if h["symbol"] == "BONUSCO")
    assert Decimal(holding_after_sell["quantity"]) == Decimal("3.00000000")
    assert Decimal(holding_after_sell["avg_cost"]) == Decimal("0.000000")


@pytest.mark.asyncio
async def test_corporate_action_split_is_reconciliation_neutral(client: AsyncClient):
    """Spec-051 golden scenario 4 (cash-correctness campaign G4 gate): a split
    recorded mid-history must add no term to either side of the reconciliation
    identity. Funding via a capital transfer (not a manual cash-balance edit,
    which is itself an unmodelled event and would produce its own
    discrepancy) isolates the split's effect specifically."""
    account_map = await _register_and_login(
        client, email="split-recon@example.com", username="split-recon", password="TestPass123!"
    )
    broker_id = account_map["brokerage"]

    bank_res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "Recon Funding Bank",
            "account_type": "bank",
            "default_currency_code": "USD",
        },
    )
    assert bank_res.status_code == 201, bank_res.text
    bank_id = bank_res.json()["public_id"]

    transfer_res = await client.post(
        "/v1/finance/transfers",
        json={
            "from_module": "spending",
            "to_module": "investing",
            "from_account_id": bank_id,
            "to_account_id": broker_id,
            "from_currency_code": "USD",
            "to_currency_code": "USD",
            "gross_amount": "1000.00",
            "net_amount_received": "1000.00",
            "occurred_at": "2026-06-01T10:00:00Z",
        },
    )
    assert transfer_res.status_code == 201, transfer_res.text

    buy_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": broker_id,
            "order_type": "buy",
            "symbol": "NVDA",
            "quantity": "2.00000000",
            "price_per_unit": "100.00",
            "currency": "USD",
            "occurred_at": "2026-06-02T10:00:00Z",
        },
    )
    assert buy_res.status_code == 201, buy_res.text

    split_res = await client.post(
        "/v1/investing/corporate-actions",
        json={
            "account_id": broker_id,
            "symbol": "NVDA",
            "action_type": "split",
            "ratio_base": "1",
            "ratio_quote": "2",
            "ex_date": "2026-06-03",
        },
    )
    assert split_res.status_code == 201, split_res.text

    # Sell the full post-split position (4 shares) at a price that would have
    # been invalid pre-split (a sell of 4 against a nominal 2-share holding).
    sell_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": broker_id,
            "order_type": "sell",
            "symbol": "NVDA",
            "quantity": "4.00000000",
            "price_per_unit": "60.00",
            "currency": "USD",
            "occurred_at": "2026-06-04T10:00:00Z",
        },
    )
    assert sell_res.status_code == 201, sell_res.text

    recon_res = await client.get(f"/v1/finance/accounts/{broker_id}/reconciliation")
    assert recon_res.status_code == 200, recon_res.text
    data = recon_res.json()
    # projected = transfer_in 1000 - buy net 200 + sell net 240 = 1040;
    # snapshot is written only by the transfer/buy/sell, never the split.
    assert float(data["projected_balance"]) == 1040.0
    assert float(data["snapshot_balance"]) == 1040.0
    assert float(data["discrepancy"]) == 0.0
    assert data["order_count"] == 2
    assert data["transfer_count"] == 1


# ---------------------------------------------------------------------------
# Dividends / income events (spec-073)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_dividend_credits_cash_with_no_offsetting_debit(client: AsyncClient):
    """INV-1: a dividend credits brokerage cash with no counterparty debit in
    any user account — the structural fix for the old wallet->brokerage
    transfer workaround. No CapitalTransfer row is created."""
    account_map = await _register_and_login(
        client,
        email="dividend-credit@example.com",
        username="dividend-credit",
        password="TestPass123!",
    )
    broker_id = account_map["brokerage"]

    res = await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": broker_id,
            "symbol": "NVDA",
            "income_type": "dividend",
            "gross_amount": "100.00",
            "tax_withheld": "10.00",
            "currency": "USD",
            "pay_date": "2026-06-15",
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["net_amount"] == "90.00"
    assert data["income_type"] == "dividend"
    assert data["symbol"] == "NVDA"

    cash_res = await client.get("/v1/investing/cash-balances", params={"account_id": broker_id})
    assert cash_res.status_code == 200
    balances = cash_res.json()["items"]
    assert len(balances) == 1
    assert balances[0]["balance"] == "90.00"
    assert balances[0]["trigger_type"] == "dividend"

    transfers_res = await client.get("/v1/finance/transfers")
    assert transfers_res.status_code == 200
    assert transfers_res.json()["total"] == 0


@pytest.mark.asyncio
async def test_create_dividend_rejects_non_brokerage_account(client: AsyncClient):
    await _register_and_login(
        client,
        email="dividend-non-brokerage@example.com",
        username="dividend-non-brokerage",
        password="TestPass123!",
    )
    bank_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Bank", "account_type": "bank", "default_currency_code": "USD"},
    )
    assert bank_res.status_code == 201
    bank_id = bank_res.json()["public_id"]

    res = await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": bank_id,
            "income_type": "interest",
            "gross_amount": "50.00",
            "currency": "USD",
            "pay_date": "2026-06-15",
        },
    )
    assert res.status_code == 422
    assert "brokerage" in res.json()["detail"]


@pytest.mark.asyncio
async def test_create_dividend_rejects_currency_mismatch(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="dividend-currency-mismatch@example.com",
        username="dividend-currency-mismatch",
        password="TestPass123!",
    )
    res = await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": account_map["brokerage"],  # USD
            "gross_amount": "50.00",
            "currency": "INR",
            "pay_date": "2026-06-15",
        },
    )
    assert res.status_code == 422
    assert "does not match account" in res.json()["detail"]


@pytest.mark.asyncio
async def test_interest_income_has_no_symbol_attribution(client: AsyncClient):
    """Decision (spec-073 rev.3): interest is account-level — no holding_id/
    symbol attribution, unlike dividends."""
    account_map = await _register_and_login(
        client,
        email="dividend-interest@example.com",
        username="dividend-interest",
        password="TestPass123!",
    )
    res = await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": account_map["brokerage"],
            "income_type": "interest",
            "gross_amount": "25.00",
            "currency": "USD",
            "pay_date": "2026-06-15",
        },
    )
    assert res.status_code == 201, res.text
    assert res.json()["symbol"] is None


@pytest.mark.asyncio
async def test_delete_dividend_reverses_cash_credit(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="dividend-delete@example.com",
        username="dividend-delete",
        password="TestPass123!",
    )
    broker_id = account_map["brokerage"]

    create_res = await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": broker_id,
            "gross_amount": "100.00",
            "currency": "USD",
            "pay_date": "2026-06-15",
        },
    )
    assert create_res.status_code == 201
    dividend_id = create_res.json()["public_id"]

    delete_res = await client.delete(f"/v1/investing/dividends/{dividend_id}")
    assert delete_res.status_code == 204

    cash_res = await client.get("/v1/investing/cash-balances", params={"account_id": broker_id})
    assert cash_res.json()["items"] == []

    get_res = await client.get(f"/v1/investing/dividends/{dividend_id}")
    assert get_res.status_code == 404


@pytest.mark.asyncio
async def test_dividend_reconciliation_is_included_in_projected_balance(client: AsyncClient):
    """INV-2: a dividend credit must appear on the reconciliation projected
    side too, or it manufactures a discrepancy equal to the dividend total."""
    account_map = await _register_and_login(
        client,
        email="dividend-recon@example.com",
        username="dividend-recon",
        password="TestPass123!",
    )
    broker_id = account_map["brokerage"]

    div_res = await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": broker_id,
            "symbol": "NVDA",
            "gross_amount": "100.00",
            "tax_withheld": "10.00",
            "currency": "USD",
            "pay_date": "2026-06-15",
        },
    )
    assert div_res.status_code == 201, div_res.text

    recon_res = await client.get(f"/v1/finance/accounts/{broker_id}/reconciliation")
    assert recon_res.status_code == 200, recon_res.text
    data = recon_res.json()
    assert float(data["projected_balance"]) == 90.0
    assert float(data["snapshot_balance"]) == 90.0
    assert float(data["discrepancy"]) == 0.0
    assert data["dividend_count"] == 1


@pytest.mark.asyncio
async def test_update_dividend_amount_recomputes_cash_credit(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="dividend-update@example.com",
        username="dividend-update",
        password="TestPass123!",
    )
    broker_id = account_map["brokerage"]

    create_res = await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": broker_id,
            "gross_amount": "100.00",
            "currency": "USD",
            "pay_date": "2026-06-15",
        },
    )
    dividend_id = create_res.json()["public_id"]

    update_res = await client.patch(
        f"/v1/investing/dividends/{dividend_id}",
        json={"gross_amount": "150.00", "tax_withheld": "15.00"},
    )
    assert update_res.status_code == 200, update_res.text
    assert update_res.json()["net_amount"] == "135.00"

    cash_res = await client.get("/v1/investing/cash-balances", params={"account_id": broker_id})
    balances = cash_res.json()["items"]
    assert len(balances) == 1
    assert balances[0]["balance"] == "135.00"


# ---------------------------------------------------------------------------
# Return metrics: XIRR / open-closed / realized-unrealized (spec-071)
# ---------------------------------------------------------------------------


async def _fund_brokerage(client: AsyncClient, broker_id: str, amount: str = "100000.00") -> None:
    res = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": broker_id,
            "balance": amount,
            "currency": "USD",
            "as_of": "2023-12-01T00:00:00Z",
        },
    )
    assert res.status_code == 201, res.text


@pytest.mark.asyncio
async def test_return_metrics_open_position_includes_terminal_value(client: AsyncClient):
    account_map = await _register_and_login(
        client, email="returns-open@example.com", username="returns-open", password="TestPass123!"
    )
    broker_id = account_map["brokerage"]
    await _fund_brokerage(client, broker_id)

    buy_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": broker_id,
            "order_type": "buy",
            "symbol": "NVDA",
            "quantity": "10.00000000",
            "price_per_unit": "100.00",
            "currency": "USD",
            "occurred_at": "2024-01-01T10:00:00Z",
        },
    )
    assert buy_res.status_code == 201, buy_res.text

    res = await client.get("/v1/investing/performance/returns")
    assert res.status_code == 200, res.text
    data = res.json()
    overall = data["overall"]
    # Open block should carry the position (market value defaults to book
    # value in this test since no price history was recorded).
    assert overall["open"]["invested"] == "1000.00"
    assert overall["closed"]["invested"] == "0.00"
    assert overall["open"]["market_value"] == "1000.00"


@pytest.mark.asyncio
async def test_return_metrics_closed_position_has_no_terminal_value(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="returns-closed@example.com",
        username="returns-closed",
        password="TestPass123!",
    )
    broker_id = account_map["brokerage"]
    await _fund_brokerage(client, broker_id)

    await client.post(
        "/v1/investing/orders",
        json={
            "account_id": broker_id,
            "order_type": "buy",
            "symbol": "NVDA",
            "quantity": "10.00000000",
            "price_per_unit": "100.00",
            "currency": "USD",
            "occurred_at": "2024-01-01T10:00:00Z",
        },
    )
    sell_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": broker_id,
            "order_type": "sell",
            "symbol": "NVDA",
            "quantity": "10.00000000",
            "price_per_unit": "150.00",
            "currency": "USD",
            "occurred_at": "2024-06-01T10:00:00Z",
        },
    )
    assert sell_res.status_code == 201, sell_res.text

    res = await client.get("/v1/investing/performance/returns")
    data = res.json()["overall"]
    assert data["open"]["invested"] == "0.00"
    assert data["closed"]["invested"] == "1000.00"
    assert data["closed"]["market_value"] == "0.00"
    assert float(data["closed"]["realized"]) == 500.0


@pytest.mark.asyncio
async def test_return_metrics_dividend_is_income_not_contribution(client: AsyncClient):
    """INV-6: a dividend enters the flow series as positive income but is
    excluded from invested capital -- so invested reflects only the buy,
    while realized includes the dividend net amount."""
    account_map = await _register_and_login(
        client,
        email="returns-dividend@example.com",
        username="returns-dividend",
        password="TestPass123!",
    )
    broker_id = account_map["brokerage"]
    await _fund_brokerage(client, broker_id)

    await client.post(
        "/v1/investing/orders",
        json={
            "account_id": broker_id,
            "order_type": "buy",
            "symbol": "NVDA",
            "quantity": "10.00000000",
            "price_per_unit": "100.00",
            "currency": "USD",
            "occurred_at": "2024-01-01T10:00:00Z",
        },
    )
    div_res = await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": broker_id,
            "symbol": "NVDA",
            "gross_amount": "50.00",
            "currency": "USD",
            "pay_date": "2024-03-01",
        },
    )
    assert div_res.status_code == 201, div_res.text

    res = await client.get("/v1/investing/performance/returns")
    data = res.json()["overall"]
    assert data["open"]["invested"] == "1000.00"  # dividend excluded
    assert float(data["open"]["realized"]) == 50.0  # dividend is realized income


@pytest.mark.asyncio
async def test_return_metrics_sub_year_annualization_not_reliable(client: AsyncClient):
    """INV-7: a position held under 365 days never gets an annualized
    figure, even though XIRR itself may still be solvable."""
    account_map = await _register_and_login(
        client,
        email="returns-subyear@example.com",
        username="returns-subyear",
        password="TestPass123!",
    )
    broker_id = account_map["brokerage"]
    await _fund_brokerage(client, broker_id, amount="100000.00")

    await client.post(
        "/v1/investing/orders",
        json={
            "account_id": broker_id,
            "order_type": "buy",
            "symbol": "NVDA",
            "quantity": "10.00000000",
            "price_per_unit": "100.00",
            "currency": "USD",
            "occurred_at": "2026-06-01T10:00:00Z",
        },
    )

    res = await client.get("/v1/investing/performance/returns")
    data = res.json()["overall"]
    assert data["annualization_reliable"] is False
    assert data["annualized_return_pct"] is None


@pytest.mark.asyncio
async def test_return_metrics_open_closed_partition_reconciles_to_overall(client: AsyncClient):
    """INV-5: open and closed realized components sum to the overall
    realized total -- the partition is exhaustive and disjoint."""
    account_map = await _register_and_login(
        client,
        email="returns-partition@example.com",
        username="returns-partition",
        password="TestPass123!",
    )
    broker_id = account_map["brokerage"]
    await _fund_brokerage(client, broker_id)

    # NVDA: fully closed with realized gain.
    await client.post(
        "/v1/investing/orders",
        json={
            "account_id": broker_id,
            "order_type": "buy",
            "symbol": "NVDA",
            "quantity": "10.00000000",
            "price_per_unit": "100.00",
            "currency": "USD",
            "occurred_at": "2024-01-01T10:00:00Z",
        },
    )
    await client.post(
        "/v1/investing/orders",
        json={
            "account_id": broker_id,
            "order_type": "sell",
            "symbol": "NVDA",
            "quantity": "10.00000000",
            "price_per_unit": "150.00",
            "currency": "USD",
            "occurred_at": "2024-06-01T10:00:00Z",
        },
    )
    # AAPL: still open, with a dividend as realized income.
    await client.post(
        "/v1/investing/orders",
        json={
            "account_id": broker_id,
            "order_type": "buy",
            "symbol": "AAPL",
            "quantity": "5.00000000",
            "price_per_unit": "50.00",
            "currency": "USD",
            "occurred_at": "2024-02-01T10:00:00Z",
        },
    )
    await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": broker_id,
            "symbol": "AAPL",
            "gross_amount": "20.00",
            "currency": "USD",
            "pay_date": "2024-05-01",
        },
    )

    res = await client.get("/v1/investing/performance/returns")
    data = res.json()["overall"]
    open_realized = Decimal(data["open"]["realized"])
    closed_realized = Decimal(data["closed"]["realized"])
    assert open_realized + closed_realized == Decimal(data["realized"])
    assert closed_realized == Decimal("500.00")
    assert open_realized == Decimal("20.00")


@pytest.mark.asyncio
async def test_return_metrics_by_account_present(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="returns-by-account@example.com",
        username="returns-by-account",
        password="TestPass123!",
    )
    broker_id = account_map["brokerage"]
    await _fund_brokerage(client, broker_id)

    await client.post(
        "/v1/investing/orders",
        json={
            "account_id": broker_id,
            "order_type": "buy",
            "symbol": "NVDA",
            "quantity": "10.00000000",
            "price_per_unit": "100.00",
            "currency": "USD",
            "occurred_at": "2024-01-01T10:00:00Z",
        },
    )

    res = await client.get("/v1/investing/performance/returns")
    data = res.json()
    assert len(data["by_account"]) == 1
    assert data["by_account"][0]["account_id"] == broker_id
    assert data["by_account"][0]["currency"] == "USD"


@pytest.mark.asyncio
async def test_return_metrics_empty_workspace_returns_zeroed_scopes(client: AsyncClient):
    await _register_and_login(
        client, email="returns-empty@example.com", username="returns-empty", password="TestPass123!"
    )
    res = await client.get("/v1/investing/performance/returns")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["overall"]["xirr"] is None
    assert data["by_account"] == []


@pytest.mark.asyncio
async def test_return_metrics_total_return_not_inflated_by_terminal_value(client: AsyncClient):
    """A flat open position (market value == book value) must show 0% total
    return. The terminal market-value flow is already part of the position's
    flow series, so net_flow must not add market value a second time — the
    double-count would render a flat position as +100%."""
    account_map = await _register_and_login(
        client, email="returns-flat@example.com", username="returns-flat", password="TestPass123!"
    )
    broker_id = account_map["brokerage"]
    await _fund_brokerage(client, broker_id)

    buy_res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": broker_id,
            "order_type": "buy",
            "symbol": "NVDA",
            "quantity": "10.00000000",
            "price_per_unit": "100.00",
            "currency": "USD",
            "occurred_at": "2024-01-01T10:00:00Z",
        },
    )
    assert buy_res.status_code == 201, buy_res.text

    res = await client.get("/v1/investing/performance/returns")
    assert res.status_code == 200, res.text
    overall = res.json()["overall"]
    # No price history -> market value defaults to book value -> flat.
    assert overall["open"]["total_return_pct"] == "0.00"
    # Scope level also carries total_return_pct (spec-071 §B) so the UI can
    # show the INV-7 simple-return fallback for sub-year spans.
    assert overall["total_return_pct"] == "0.00"


@pytest.mark.asyncio
async def test_return_metrics_account_level_income_included_in_all_scopes(client: AsyncClient):
    """Account-level income (interest, no symbol) must flow into overall and
    by_currency, not just by_account — otherwise overall realized disagrees
    with the sum of the account blocks."""
    account_map = await _register_and_login(
        client,
        email="returns-interest@example.com",
        username="returns-interest",
        password="TestPass123!",
    )
    broker_id = account_map["brokerage"]

    div_res = await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": broker_id,
            "income_type": "interest",
            "gross_amount": "25.00",
            "currency": "USD",
            "pay_date": "2026-06-15",
        },
    )
    assert div_res.status_code == 201, div_res.text

    res = await client.get("/v1/investing/performance/returns")
    assert res.status_code == 200, res.text
    data = res.json()
    assert float(data["overall"]["realized"]) == 25.0
    assert len(data["by_currency"]) == 1
    assert float(data["by_currency"][0]["realized"]) == 25.0
    assert len(data["by_account"]) == 1
    assert float(data["by_account"][0]["realized"]) == 25.0


@pytest.mark.asyncio
async def test_create_dividend_rejects_tax_equal_to_gross(client: AsyncClient):
    """net_amount must stay > 0 (DB CHECK): full withholding must be a clean
    422, never an IntegrityError 500."""
    account_map = await _register_and_login(
        client,
        email="dividend-full-tax@example.com",
        username="dividend-full-tax",
        password="TestPass123!",
    )
    res = await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": account_map["brokerage"],
            "gross_amount": "50.00",
            "tax_withheld": "50.00",
            "currency": "USD",
            "pay_date": "2026-06-15",
        },
    )
    assert res.status_code == 422, res.text
    assert "less than" in res.text
