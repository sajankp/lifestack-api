import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import postgres
from app.investing.models import CashBalance, Holding


@pytest.mark.asyncio
async def test_investing_crud_summary_and_audit(client: AsyncClient):
    user = {
        "email": "investing-e2e@example.com",
        "username": "investing-e2e",
        "password": "password123",
    }
    register_res = await client.post("/v1/auth/register", json=user)
    assert register_res.status_code == 200

    login_res = await client.post(
        "/v1/auth/login", data={"username": user["username"], "password": user["password"]}
    )
    assert login_res.status_code == 200

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
