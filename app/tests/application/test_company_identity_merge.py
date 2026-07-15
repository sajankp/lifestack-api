from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.application.company_identity_merge import merge_company_identities
from app.auth.models import User
from app.core.database import postgres
from app.investing.models import Company, Instrument, InstrumentConstituent
from app.platform.models import Workspace, WorkspaceMembership


async def _seed_workspace(session) -> int:
    user = User(username="merge-test", email="merge-test@example.com", hashed_password="x")
    session.add(user)
    await session.flush()

    workspace = Workspace(name="Merge Test WS")
    session.add(workspace)
    await session.flush()

    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="owner"))
    await session.flush()
    return workspace.id


async def _seed_instrument(session, workspace_id: int, symbol: str) -> Instrument:
    instrument = Instrument(
        workspace_id=workspace_id, symbol=symbol, name=symbol, instrument_type="etf"
    )
    session.add(instrument)
    await session.flush()
    return instrument


@pytest.mark.asyncio
async def test_merge_combines_duplicate_companies_by_normalized_name(override_database_url):
    async with postgres.async_session_maker() as session:
        workspace_id = await _seed_workspace(session)
        survivor = Company(workspace_id=workspace_id, name="Apple Inc")
        loser = Company(workspace_id=workspace_id, name="Apple Inc.")
        session.add_all([survivor, loser])
        await session.flush()
        survivor_id, loser_id = survivor.id, loser.id

        instrument = await _seed_instrument(session, workspace_id, "UMMA")
        session.add(
            InstrumentConstituent(
                instrument_id=instrument.id,
                constituent_company_id=loser_id,
                weight=Decimal("0.5"),
                as_of_date=date(2026, 6, 14),
                source="csv_import",
                fetched_at=datetime.now(UTC),
            )
        )
        await session.flush()

        summary = await merge_company_identities(session, workspace_id)

        assert summary.groups_merged == 1
        assert summary.companies_deleted == 1
        assert summary.constituent_rows_repointed == 1
        assert summary.constituent_rows_dropped_collision == 0

    async with postgres.async_session_maker() as verify_session:
        remaining = (
            (
                await verify_session.execute(
                    select(Company).where(Company.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(remaining) == 1
        assert remaining[0].id == survivor_id

        const_rows = (
            (
                await verify_session.execute(
                    select(InstrumentConstituent).where(
                        InstrumentConstituent.constituent_company_id == survivor_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(const_rows) == 1


@pytest.mark.asyncio
async def test_merge_resolves_colliding_constituent_rows_without_raising(override_database_url):
    """spec-083 §9 step 3: when survivor and loser both have a constituent row
    for the same (instrument_id, as_of_date, source), a naive FK repoint
    violates uq_investing_constituent_snapshot — assert the merge instead
    drops the loser's duplicate row per the documented collision policy.
    """
    async with postgres.async_session_maker() as session:
        workspace_id = await _seed_workspace(session)
        survivor = Company(workspace_id=workspace_id, name="Apple Inc", ticker="AAPL")
        loser = Company(workspace_id=workspace_id, name="Apple Inc.", ticker="AAPL")
        session.add_all([survivor, loser])
        await session.flush()
        survivor_id, loser_id = survivor.id, loser.id

        instrument = await _seed_instrument(session, workspace_id, "UMMA")
        as_of = date(2026, 6, 14)
        session.add_all([
            InstrumentConstituent(
                instrument_id=instrument.id,
                constituent_company_id=survivor_id,
                weight=Decimal("0.4"),
                as_of_date=as_of,
                source="csv_import",
                fetched_at=datetime.now(UTC),
            ),
            InstrumentConstituent(
                instrument_id=instrument.id,
                constituent_company_id=loser_id,
                weight=Decimal("0.1"),
                as_of_date=as_of,
                source="csv_import",
                fetched_at=datetime.now(UTC),
            ),
        ])
        await session.flush()

        summary = await merge_company_identities(session, workspace_id)

        assert summary.groups_merged == 1
        assert summary.constituent_rows_dropped_collision == 1
        assert summary.constituent_rows_repointed == 0

    async with postgres.async_session_maker() as verify_session:
        const_rows = (
            (
                await verify_session.execute(
                    select(InstrumentConstituent).where(
                        InstrumentConstituent.instrument_id == instrument.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(const_rows) == 1
        assert const_rows[0].constituent_company_id == survivor_id
        assert const_rows[0].weight == Decimal("0.4")


@pytest.mark.asyncio
async def test_merge_repoints_instrument_company_id(override_database_url):
    async with postgres.async_session_maker() as session:
        workspace_id = await _seed_workspace(session)
        survivor = Company(workspace_id=workspace_id, name="Apple Inc", isin="US0378331005")
        loser = Company(workspace_id=workspace_id, name="Apple", isin="US0378331005")
        session.add_all([survivor, loser])
        await session.flush()
        survivor_id, loser_id = survivor.id, loser.id

        stock = Instrument(
            workspace_id=workspace_id,
            symbol="AAPL",
            name="Apple",
            instrument_type="stock",
            company_id=loser_id,
        )
        session.add(stock)
        await session.flush()
        stock_id = stock.id

        summary = await merge_company_identities(session, workspace_id)
        assert summary.instruments_repointed == 1

    async with postgres.async_session_maker() as verify_session:
        refreshed = await verify_session.get(Instrument, stock_id)
        assert refreshed.company_id == survivor_id


@pytest.mark.asyncio
async def test_merge_dry_run_reports_without_mutating(override_database_url):
    async with postgres.async_session_maker() as session:
        workspace_id = await _seed_workspace(session)
        session.add_all([
            Company(workspace_id=workspace_id, name="Apple Inc"),
            Company(workspace_id=workspace_id, name="Apple Inc."),
        ])
        await session.commit()

        summary = await merge_company_identities(session, workspace_id, dry_run=True)
        assert summary.groups_merged == 1
        assert summary.companies_deleted == 1

    async with postgres.async_session_maker() as verify_session:
        remaining = (
            (
                await verify_session.execute(
                    select(Company).where(Company.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(remaining) == 2


@pytest.mark.asyncio
async def test_merge_no_duplicates_is_noop(override_database_url):
    async with postgres.async_session_maker() as session:
        workspace_id = await _seed_workspace(session)
        session.add(Company(workspace_id=workspace_id, name="Solo Co"))
        await session.flush()

        summary = await merge_company_identities(session, workspace_id)

        assert summary.groups_merged == 0
        assert summary.companies_deleted == 0
