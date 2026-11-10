"""merge_companies: re-point, sum weights, enrich, retire (spec-083 §9).

Service-level tests for ``CompanyMergeService.merge_companies`` against the
real (transactional) test DB. Mirrors the style of test_constituent_merge.py.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.database import postgres
from app.investing.company_merge_service import CompanyMergeService, MergeConflictError
from app.investing.models import Company, Instrument, InstrumentConstituent, InstrumentType
from app.investing.repository import (
    CompanyRepository,
    InstrumentConstituentRepository,
    InstrumentRepository,
)
from app.platform.models import Workspace


async def _make_workspace(session) -> int:
    session.add(Workspace(name="merge-ws"))
    await session.flush()
    result = await session.execute(select(Workspace).where(Workspace.name == "merge-ws"))
    return result.scalar_one().id


async def _make_company(session, workspace_id: int, name: str, ticker: str, isin: str | None) -> Company:
    company = Company(workspace_id=workspace_id, name=name, ticker=ticker, isin=isin)
    session.add(company)
    await session.flush()
    return company


async def _make_instrument(
    session, workspace_id: int, symbol: str, company_id: int
) -> Instrument:
    instrument = Instrument(
        workspace_id=workspace_id,
        symbol=symbol,
        name=symbol,
        instrument_type=InstrumentType.stock.value,
        company_id=company_id,
    )
    session.add(instrument)
    await session.flush()
    return instrument


async def _make_constituent(
    session,
    instrument_id: int,
    company_id: int,
    weight: str,
    as_of: date,
    source: str,
) -> InstrumentConstituent:
    row = InstrumentConstituent(
        instrument_id=instrument_id,
        constituent_company_id=company_id,
        weight=Decimal(weight),
        as_of_date=as_of,
        source=source,
        fetched_at=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
    )
    session.add(row)
    await session.flush()
    return row


def _service(session) -> CompanyMergeService:
    return CompanyMergeService(
        session,
        CompanyRepository(session),
        InstrumentRepository(session),
        InstrumentConstituentRepository(session),
    )


@pytest.mark.asyncio
async def test_merge_repoints_instruments_constituents_copies_fields_retires_drop(client):
    async with postgres.async_session_maker() as session:
        ws = await _make_workspace(session)
        keep = await _make_company(session, ws, "ASML Holding N.V.", "ASML", None)
        drop = await _make_company(session, ws, "ASML Holding NV", "ASML NA", "NL0010273215")
        third = await _make_company(session, ws, "Other Co", "OTHER", "NL0000000003")
        fund = await _make_instrument(session, ws, "UMMA", keep.id)  # already on keep
        drop_instrument = await _make_instrument(session, ws, "HIEU.L", drop.id)
        await _make_constituent(session, drop_instrument.id, drop.id, "0.12905280", date(2026, 11, 3), "s1")
        await _make_constituent(session, fund.id, drop.id, "0.03610000", date(2026, 11, 6), "s1")
        await session.commit()

    service = _service  # repositories rebound per session below
    async with postgres.async_session_maker() as session:
        outcome = await CompanyMergeService(
            session,
            CompanyRepository(session),
            InstrumentRepository(session),
            InstrumentConstituentRepository(session),
        ).merge_companies(ws, keep.public_id, drop.public_id, actor_id=1)
        await session.commit()

    assert outcome["changed"] is True
    assert outcome["before"]["keep"]["isin"] is None
    assert outcome["after"]["keep"]["isin"] == "NL0010273215"
    assert outcome["after"]["keep"]["ticker"] == "ASML"
    assert outcome["summary"]["instruments_repointed"] == 1
    assert outcome["summary"]["constituent_rows_repointed"] == 2
    assert outcome["summary"]["drop_retired"] is True

    async with postgres.async_session_maker() as session:
        # Drop company is gone; nothing references it anymore.
        assert (
            await session.execute(select(Company).where(Company.public_id == drop.public_id))
        ).scalar_one_or_none() is None
        kept_instrument = (
            await session.execute(
                select(Instrument).where(Instrument.symbol == "HIEU.L")
            )
        ).scalar_one()
        assert kept_instrument.company_id == keep.id
        rows = (
            await session.execute(
                select(InstrumentConstituent).where(
                    InstrumentConstituent.instrument_id == kept_instrument.id
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].constituent_company_id == keep.id
        assert rows[0].weight == Decimal("0.12905280")
        fund_rows = (
            await session.execute(
                select(InstrumentConstituent).where(
                    InstrumentConstituent.instrument_id == fund.id
                )
            )
        ).scalars().all()
        assert len(fund_rows) == 1
        assert fund_rows[0].constituent_company_id == keep.id
        assert fund_rows[0].weight == Decimal("0.03610000")
        # The untouched third company is unaffected.
        assert (await session.get(Company, third.id)) is not None


@pytest.mark.asyncio
async def test_merge_sums_weights_on_snapshot_collision(client):
    async with postgres.async_session_maker() as session:
        ws = await _make_workspace(session)
        keep = await _make_company(session, ws, "Keep Co", "KEEP", None)
        drop = await _make_company(session, ws, "Drop Co", "DROP", None)
        fund = await _make_instrument(session, ws, "FUND", keep.id)
        await _make_constituent(session, fund.id, keep.id, "0.10", date(2026, 11, 1), "s1")
        await _make_constituent(session, fund.id, drop.id, "0.05", date(2026, 11, 1), "s1")
        await session.commit()

    async with postgres.async_session_maker() as session:
        outcome = await CompanyMergeService(
            session,
            CompanyRepository(session),
            InstrumentRepository(session),
            InstrumentConstituentRepository(session),
        ).merge_companies(ws, keep.public_id, drop.public_id, actor_id=1)
        await session.commit()

    assert outcome["summary"]["constituent_weights_summed"] == 1
    assert outcome["summary"]["constituent_rows_repointed"] == 0
    async with postgres.async_session_maker() as session:
        rows = (
            await session.execute(
                select(InstrumentConstituent).where(
                    InstrumentConstituent.instrument_id == fund.id,
                    InstrumentConstituent.as_of_date == date(2026, 11, 1),
                    InstrumentConstituent.source == "s1",
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].constituent_company_id == keep.id
        assert rows[0].weight == Decimal("0.15000000")


@pytest.mark.asyncio
async def test_merge_rejects_global_company(client):
    async with postgres.async_session_maker() as session:
        ws = await _make_workspace(session)
        keep = await _make_company(session, ws, "Keep Co", "KEEP", None)
        drop = await _make_company(session, None, "Global Co", "GBL", None)  # global
        await session.commit()

    with pytest.raises(MergeConflictError, match="workspace-scoped"):
        async with postgres.async_session_maker() as session:
            await CompanyMergeService(
                session,
                CompanyRepository(session),
                InstrumentRepository(session),
                InstrumentConstituentRepository(session),
            ).merge_companies(ws, keep.public_id, drop.public_id, actor_id=1)


@pytest.mark.asyncio
async def test_merge_rejects_same_company_and_unknown(client):
    async with postgres.async_session_maker() as session:
        ws = await _make_workspace(session)
        keep = await _make_company(session, ws, "Keep Co", "KEEP", None)
        await session.commit()

    with pytest.raises(MergeConflictError, match="different companies"):
        async with postgres.async_session_maker() as session:
            await CompanyMergeService(
                session,
                CompanyRepository(session),
                InstrumentRepository(session),
                InstrumentConstituentRepository(session),
            ).merge_companies(ws, keep.public_id, keep.public_id, actor_id=1)

    with pytest.raises(MergeConflictError, match="not found in this workspace"):
        async with postgres.async_session_maker() as session:
            await CompanyMergeService(
                session,
                CompanyRepository(session),
                InstrumentRepository(session),
                InstrumentConstituentRepository(session),
            ).merge_companies(ws, keep.public_id, uuid4(), actor_id=1)


@pytest.mark.asyncio
async def test_merge_rejects_isin_clash_with_third_company(client):
    async with postgres.async_session_maker() as session:
        ws = await _make_workspace(session)
        keep = await _make_company(session, ws, "Keep Co", "KEEP", None)
        drop = await _make_company(session, ws, "Drop Co", "DROP", "US0378331005")
        third = await _make_company(session, ws, "Third Co", "AAPL", "US0378331005")
        await session.commit()

    with pytest.raises(MergeConflictError, match="already used"):
        async with postgres.async_session_maker() as session:
            await CompanyMergeService(
                session,
                CompanyRepository(session),
                InstrumentRepository(session),
                InstrumentConstituentRepository(session),
            ).merge_companies(ws, keep.public_id, drop.public_id, actor_id=1)
