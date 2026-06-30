import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import postgres
from app.finance.models import Account, Currency, FxRate, WorkspaceCurrency
from app.investing.models import CashBalance, Holding, HoldingPrice, Instrument, PortfolioSnapshot


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


async def _create_holding_via_order(
    client: AsyncClient,
    account_id: str,
    symbol: str,
    quantity: str,
    price: str,
    currency: str = "USD",
    instrument_type: str = "stock",
    instrument_name: str | None = None,
) -> str:
    """Create a holding by placing a buy order (the only supported path)."""
    cost = float(quantity) * float(price)
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
        "occurred_at": datetime.now(UTC).isoformat(),
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
    assert summary["currency_breakdown"]["USD"] == "2577.6250000000"
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
    # 2. Create GBP asset via order on brokerage; then zero out the leftover cash
    await _create_holding_via_order(
        client, account_map["brokerage"], "SAP", "5.00000000", "100.00", "gbp"
    )
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["brokerage"],
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

    gbp_holding_id = await _create_holding_via_order(
        client, account_map["gbp-wallet"], "VUSA", "3.00000000", "10.00", "gbp"
    )
    # Zero out the order's leftover GBP cash
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_map["gbp-wallet"],
            "balance": "0.00",
            "currency": "GBP",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )

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

    await _create_holding_via_order(
        client, account_map["brokerage"], "VTI", "10.00000000", "100.00", "GBP", "etf"
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
                as_of=datetime.now(UTC),
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
    for symbol, currency, account_id in [
        ("AAPL", "USD", account_map["brokerage"]),
        ("VOD", "GBP", account_map["wallet"]),
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
    account_map = await _register_and_login(
        client,
        email="indian-refresh@example.com",
        username="indian-refresh",
        password="TestPass123!",
    )

    # Create an INR holding with symbol TATSILV (no dot)
    await _create_holding_via_order(
        client, account_map["brokerage"], "TATSILV", "10.00000000", "20.00", "INR"
    )

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
    account_map = await _register_and_login(
        client,
        email="amfi-refresh@example.com",
        username="amfi-refresh",
        password="TestPass123!",
    )
    await _create_holding_via_order(
        client,
        account_map["brokerage"],
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
async def test_update_holding_symbol_relinks_instrument_and_preserves_fields(client: AsyncClient):
    account_map = await _register_and_login(
        client,
        email="symbol-edit@example.com",
        username="symbol-edit",
        password="TestPass123!",
    )
    created_id = await _create_holding_via_order(
        client, account_map["brokerage"], "NEFTPHARMA", "12.00000000", "25.00", "INR", "etf"
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
    account_map = await _register_and_login(
        client,
        email="symbol-price-reset@example.com",
        username="symbol-price-reset",
        password="TestPass123!",
    )
    created_id = await _create_holding_via_order(
        client,
        account_map["brokerage"],
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
    account_map = await _register_and_login(
        client,
        email="symbol-rename-orders@example.com",
        username="symbol-rename-orders",
        password="TestPass123!",
    )
    account_id = account_map["brokerage"]
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
    account_map = await _register_and_login(
        client,
        email="fifo-e2e@example.com",
        username="fifo-e2e",
        password="TestPass123!",
    )
    account_id = account_map["brokerage"]

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
