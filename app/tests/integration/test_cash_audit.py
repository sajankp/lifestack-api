"""Read-only brokerage cash audit: detects the drift the old transfer
delete/update path left behind, and stays quiet on a consistent ledger."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.auth.models import User
from app.core.database import postgres
from app.finance.models import CapitalTransfer
from app.investing.cash_audit import CorrectionRefusedError, apply_correction, audit_brokerage_cash
from app.investing.models import CashBalance
from app.platform.models import WorkspaceMembership
from app.tests.integration.test_finance import (
    _create_bank_and_brokerage,
    _create_investing_transfer,
    _place_buy,
    _register_and_login,
)


async def _workspace_id(email: str) -> int:
    async with postgres.async_session_maker() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        membership = (
            (
                await session.execute(
                    select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
                )
            )
            .scalars()
            .first()
        )
        return membership.workspace_id


@pytest.mark.asyncio
async def test_audit_is_clean_for_consistent_ledger(client: AsyncClient):
    await _register_and_login(
        client, email="audit-clean@example.com", username="audit-clean", password="TestPass123!"
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix="-ac")
    await _create_investing_transfer(client, from_id=bank_id, to_id=broker_id)
    await _place_buy(client, broker_id=broker_id)
    ws = await _workspace_id("audit-clean@example.com")

    async with postgres.async_session_maker() as session:
        rows = await audit_brokerage_cash(session, ws)

    assert len(rows) == 1
    assert rows[0].latest_balance == Decimal("900.00")
    assert rows[0].expected_balance == Decimal("900.00")
    assert rows[0].drift == 0


@pytest.mark.asyncio
async def test_audit_reports_drift_left_by_mid_chain_transfer_delete(client: AsyncClient):
    await _register_and_login(
        client, email="audit-drift@example.com", username="audit-drift", password="TestPass123!"
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix="-ad")
    transfer = await _create_investing_transfer(client, from_id=bank_id, to_id=broker_id)
    await _place_buy(client, broker_id=broker_id)  # chain: 1000 -> 900
    ws = await _workspace_id("audit-drift@example.com")

    # Reproduce what the old delete path did: drop the transfer and ITS snapshot
    # only, leaving the later order snapshot (900) untouched.
    transfer_uuid = uuid.UUID(transfer["public_id"])
    async with postgres.async_session_maker() as session:
        await session.execute(delete(CashBalance).where(CashBalance.trigger_ref == transfer_uuid))
        await session.execute(
            delete(CapitalTransfer).where(CapitalTransfer.public_id == transfer_uuid)
        )
        await session.commit()
        rows = await audit_brokerage_cash(session, ws)

    assert len(rows) == 1
    drift = rows[0]
    assert drift.latest_balance == Decimal("900.00")  # still shows the deleted transfer's cash
    assert drift.expected_balance == Decimal("-100.00")  # only the buy remains
    assert drift.drift == Decimal("1000.00")


@pytest.mark.asyncio
async def test_audit_treats_manual_snapshot_as_anchor(client: AsyncClient):
    await _register_and_login(
        client, email="audit-anchor@example.com", username="audit-anchor", password="TestPass123!"
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix="-aa")
    await _create_investing_transfer(client, from_id=bank_id, to_id=broker_id)
    res = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": broker_id,
            "balance": "10.72",
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    assert res.status_code == 201
    ws = await _workspace_id("audit-anchor@example.com")

    async with postgres.async_session_maker() as session:
        rows = await audit_brokerage_cash(session, ws)

    # The manual statement balance wins; earlier entities are already inside it.
    assert rows[0].latest_balance == Decimal("10.72")
    assert rows[0].expected_balance == Decimal("10.72")
    assert rows[0].drift == 0


async def _make_drift(client: AsyncClient, email: str, suffix: str) -> tuple[int, int]:
    """Build the post-bug state: transfer + snapshot gone, later order snapshot kept."""
    await _register_and_login(
        client, email=email, username=email.split("@")[0], password="TestPass123!"
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix=suffix)
    transfer = await _create_investing_transfer(client, from_id=bank_id, to_id=broker_id)
    await _place_buy(client, broker_id=broker_id)
    ws = await _workspace_id(email)
    transfer_uuid = uuid.UUID(transfer["public_id"])
    async with postgres.async_session_maker() as session:
        await session.execute(delete(CashBalance).where(CashBalance.trigger_ref == transfer_uuid))
        await session.execute(
            delete(CapitalTransfer).where(CapitalTransfer.public_id == transfer_uuid)
        )
        await session.commit()
        (row,) = await audit_brokerage_cash(session, ws)
    return ws, row.account_id


@pytest.mark.asyncio
async def test_apply_correction_appends_one_snapshot_and_clears_drift(client: AsyncClient):
    ws, acct = await _make_drift(client, "audit-apply@example.com", "-ap")

    async with postgres.async_session_maker() as session:
        before = len((await session.execute(select(CashBalance))).scalars().all())
        row = await apply_correction(
            session,
            workspace_id=ws,
            account_id=acct,
            currency="USD",
            reviewed_expected=Decimal("-100.00"),
            source_ref="audit-repair-test",
        )
        await session.commit()
        assert row.balance == Decimal("-100.00")
        after = len((await session.execute(select(CashBalance))).scalars().all())
        (checked,) = await audit_brokerage_cash(session, ws)

    assert after == before + 1  # append-only: nothing edited or deleted
    assert checked.latest_balance == Decimal("-100.00")
    assert checked.drift == 0


@pytest.mark.asyncio
async def test_apply_correction_refuses_when_reviewed_value_is_stale(client: AsyncClient):
    ws, acct = await _make_drift(client, "audit-stale@example.com", "-st")

    async with postgres.async_session_maker() as session:
        with pytest.raises(CorrectionRefusedError, match="not the reviewed"):
            await apply_correction(
                session,
                workspace_id=ws,
                account_id=acct,
                currency="USD",
                reviewed_expected=Decimal("999.00"),
                source_ref="audit-repair-test",
            )


@pytest.mark.asyncio
async def test_apply_correction_refuses_when_there_is_no_drift(client: AsyncClient):
    await _register_and_login(
        client, email="audit-nodrift@example.com", username="audit-nodrift", password="TestPass123!"
    )
    bank_id, broker_id = await _create_bank_and_brokerage(client, suffix="-nd")
    await _create_investing_transfer(client, from_id=bank_id, to_id=broker_id)
    ws = await _workspace_id("audit-nodrift@example.com")

    async with postgres.async_session_maker() as session:
        (row,) = await audit_brokerage_cash(session, ws)
        with pytest.raises(CorrectionRefusedError, match="no drift"):
            await apply_correction(
                session,
                workspace_id=ws,
                account_id=row.account_id,
                currency="USD",
                reviewed_expected=row.expected_balance,
                source_ref="audit-repair-test",
            )
