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


@pytest.mark.asyncio
async def test_investing_crud_summary_and_audit(client: AsyncClient):
    await _register_and_login(
        client,
        email="investing-e2e@example.com",
        username="investing-e2e",
        password="password123",
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
async def test_investing_duplicate_holding_conflict(client: AsyncClient):
    await _register_and_login(
        client,
        email="investing-conflict@example.com",
        username="investing-conflict",
        password="password123",
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
        password="password123",
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
        password="password123",
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
        password="password123",
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
        password="password123",
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
    # 2. Create EUR asset (Holding)
    await client.post(
        "/v1/investing/holdings",
        json={
            "symbol": "SAP",
            "account_name": "brokerage",
            "quantity": "5.00000000",
            "avg_cost": "100.00",
            "currency": "eur",
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
    # 4. Create EUR cash balance
    await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_name": "eur-wallet",
            "balance": "500.00",
            "currency": "eur",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )

    # Fetch summary and assert aggregates are only computed for the dominant currency (USD)
    summary_res = await client.get("/v1/investing/summary")
    assert summary_res.status_code == 200
    summary = summary_res.json()
    assert summary["holdings_count"] == 2
    # USD only sums
    assert Decimal(summary["portfolio_value"]) == Decimal("1500.00")
    assert Decimal(summary["cash_total"]) == Decimal("1000.00")

    # Breakdown contains correct currency mappings for both
    assert Decimal(summary["currency_breakdown"]["USD"]) == Decimal("2500.00")
    assert Decimal(summary["currency_breakdown"]["EUR"]) == Decimal("1000.00")
