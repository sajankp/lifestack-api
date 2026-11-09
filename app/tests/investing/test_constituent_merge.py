"""Constituent upsert: rows that resolve to the same company are merged by
summing their weights instead of raising uq_investing_constituent_snapshot.

Bug 2: real fund files list share classes separately (GOOGL/GOOG, Roche
ROP/RO, Atlas Copco A/B, common/pref, two ISINs for one issuer). The old code
wrote one InstrumentConstituent row per input row keyed by
(instrument_id, constituent_company_id, as_of_date, source), so two rows
resolving to one company raised a raw IntegrityError / 500. The fix groups by
resolved company and sums the weights, so look-through math stays exact.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import postgres
from app.investing.models import Company, Instrument, InstrumentConstituent, InstrumentType
from app.investing.repository import (
    CompanyRepository,
    InstrumentConstituentRepository,
    InstrumentRepository,
)
from app.investing.schemas import InstrumentConstituentCreate, InstrumentConstituentUpsert
from app.investing.service import ConstituentService
from app.platform.models import Workspace


async def _make_workspace(session) -> int:
    session.add(Workspace(name="merge-ws"))
    await session.flush()
    result = await session.execute(select(Workspace).where(Workspace.name == "merge-ws"))
    return result.scalar_one().id


async def _make_company(session, workspace_id, name, ticker, isin) -> Company:
    company = Company(workspace_id=workspace_id, name=name, ticker=ticker, isin=isin)
    session.add(company)
    await session.flush()
    return company


async def _make_mutual_fund(session, workspace_id: int, symbol: str) -> Instrument:
    instrument = Instrument(
        workspace_id=workspace_id,
        symbol=symbol,
        name=f"{symbol} Fund",
        instrument_type=InstrumentType.mutual_fund.value,
    )
    session.add(instrument)
    await session.flush()
    return instrument


@pytest.mark.asyncio
async def test_duplicate_company_rows_are_merged_by_summing_weights(client):
    async with postgres.async_session_maker() as session:
        ws = await _make_workspace(session)
        # Two share-class rows for the same company (Alphabet), same ISIN.
        company = await _make_company(session, ws, "Alphabet Inc", "GOOG", "US02079K3059")
        mf = await _make_mutual_fund(session, ws, "TECHF")
        await session.commit()

    service = ConstituentService(
        InstrumentRepository(None),  # type: ignore[arg-type]
        CompanyRepository(None),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )

    payload = InstrumentConstituentUpsert(
        as_of_date=date(2026, 11, 1),
        source="sec",
        fetched_at=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        constituents=[
            InstrumentConstituentCreate(
                company_name="Alphabet Inc Class C",
                company_ticker="GOOG",
                company_isin="US02079K3059",
                weight=Decimal("0.60"),
            ),
            InstrumentConstituentCreate(
                company_name="Alphabet Inc Class A",
                company_ticker="GOOGL",
                company_isin="US02079K3059",
                weight=Decimal("0.40"),
            ),
        ],
    )

    async with postgres.async_session_maker() as session:
        service.instrument_repo = InstrumentRepository(session)
        service.company_repo = CompanyRepository(session)
        service.constituent_repo = InstrumentConstituentRepository(session)
        rows = await service.upsert_constituents(ws, mf.public_id, payload)
        await session.commit()

    assert len(rows) == 1, rows
    assert rows[0].constituent_company_id == company.id
    assert rows[0].weight == Decimal("1.00000000")
    assert rows[0].instrument_id == mf.id

    async with postgres.async_session_maker() as session:
        stored = (
            (
                await session.execute(
                    select(InstrumentConstituent).where(
                        InstrumentConstituent.instrument_id == mf.id,
                        InstrumentConstituent.as_of_date == date(2026, 11, 1),
                        InstrumentConstituent.source == "sec",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(stored) == 1
    assert stored[0].weight == Decimal("1.00000000")


@pytest.mark.asyncio
async def test_distinct_companies_still_one_row_each(client):
    async with postgres.async_session_maker() as session:
        ws = await _make_workspace(session)
        apple = await _make_company(session, ws, "Apple Inc", "AAPL", "US0378331005")
        msft = await _make_company(session, ws, "Microsoft Corp", "MSFT", "US5949181045")
        mf = await _make_mutual_fund(session, ws, "TECHF")
        await session.commit()

    service = ConstituentService(
        InstrumentRepository(None),  # type: ignore[arg-type]
        CompanyRepository(None),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    payload = InstrumentConstituentUpsert(
        as_of_date=date(2026, 11, 1),
        source="sec",
        fetched_at=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        constituents=[
            InstrumentConstituentCreate(
                company_name="Apple Inc",
                company_ticker="AAPL",
                company_isin="US0378331005",
                weight=Decimal("0.60"),
            ),
            InstrumentConstituentCreate(
                company_name="Microsoft Corp",
                company_ticker="MSFT",
                company_isin="US5949181045",
                weight=Decimal("0.40"),
            ),
        ],
    )

    async with postgres.async_session_maker() as session:
        service.instrument_repo = InstrumentRepository(session)
        service.company_repo = CompanyRepository(session)
        service.constituent_repo = InstrumentConstituentRepository(session)
        rows = await service.upsert_constituents(ws, mf.public_id, payload)

    assert len(rows) == 2
    weights = {r.constituent_company_id: r.weight for r in rows}
    assert weights[apple.id] == Decimal("0.60000000")
    assert weights[msft.id] == Decimal("0.40000000")
