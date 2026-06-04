import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import postgres
from app.investing.models import CashBalance, Holding


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
    for account_name in [
        "brokerage",
        "wallet",
        "usd-wallet",
        "gbp-wallet",
        "eur-wallet",
        "primary",
    ]:
        await client.post(
            "/v1/finance/accounts",
            json={
                "name": account_name,
                "account_type": "brokerage",
                "default_currency_code": "USD",
            },
        )


@pytest.mark.asyncio
async def test_investing_crud_summary_and_audit(client: AsyncClient):
    await _register_and_login(
        client,
        email="investing-e2e@example.com",
        username="investing-e2e",
        password="TestPass123!",
    )

    # Create holding
    create_holding = {
        "symbol": "AAPL",
        "account_name": "brokerage",
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
        "account_name": "brokerage",
        "balance": "1000.00",
        "currency": "usd",
        "as_of": datetime.now(UTC).isoformat(),
    }
    cash_res = await client.post("/v1/investing/cash-balances", json=create_cash)
    assert cash_res.status_code == 201
    cash = cash_res.json()
    assert cash["balance"] == "1000.00"
    assert cash["currency"] == "USD"

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
    await _register_and_login(
        client,
        email="investing-price-bound@example.com",
        username="investing-price-bound",
        password="TestPass123!",
    )

    holding_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "AAPL",
            "account_name": "brokerage",
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
    await _register_and_login(
        client,
        email="investing-price-batch@example.com",
        username="investing-price-batch",
        password="TestPass123!",
    )

    holding_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "MSFT",
            "account_name": "brokerage",
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
    await _register_and_login(
        client,
        email="investing-price-duplicate@example.com",
        username="investing-price-duplicate",
        password="TestPass123!",
    )

    holding_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "GOOGL",
            "account_name": "brokerage",
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
    await _register_and_login(
        client,
        email="investing-conflict@example.com",
        username="investing-conflict",
        password="TestPass123!",
    )

    payload = {
        "symbol": "AAPL",
        "account_name": "brokerage",
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
async def test_investing_workspace_isolation(client: AsyncClient):
    # User A creates investing data
    await _register_and_login(
        client,
        email="investing-iso-a@example.com",
        username="investing-iso-a",
        password="TestPass123!",
    )
    create_res = await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "MSFT",
            "account_name": "brokerage",
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

    patch_res = await client.patch(
        f"/v1/investing/holdings/{holding_id}",
        json={"quantity": "4.00000000"},
    )
    assert patch_res.status_code == 404

    delete_res = await client.delete(f"/v1/investing/holdings/{holding_id}")
    assert delete_res.status_code == 404


@pytest.mark.asyncio
async def test_investing_cash_balance_update_and_delete(client: AsyncClient):
    await _register_and_login(
        client,
        email="investing-cash@example.com",
        username="investing-cash",
        password="TestPass123!",
    )

    create_cash = {
        "account_name": "wallet",
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
    await _register_and_login(
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
            "account_name": "brokerage",
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
            "account_name": "brokerage",
            "quantity": "5.00000000",
            "avg_cost": "100.00",
            "currency": "gbp",
        },
    )
    # 3. Create USD cash balance
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_name": "usd-wallet",
            "balance": "1000.00",
            "currency": "usd",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    # 4. Create GBP cash balance
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_name": "gbp-wallet",
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
async def test_investing_lookthrough_exposure_and_overlap(client: AsyncClient):
    await _register_and_login(
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
            "account_name": "brokerage",
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
            "account_name": "wallet",
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
