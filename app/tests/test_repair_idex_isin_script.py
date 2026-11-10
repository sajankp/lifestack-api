"""One-time data repair: IDEX Corp wrongly carried Indian Energy Exchange's ISIN.

Data corrections are scripts, not migrations: a migration runs unattended on every
database (including fresh ones) and cannot be reviewed against live data first. The
script is dry-run by default and refuses when the rows no longer look like the ones
that were reviewed.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import postgres
from app.investing.models import Company, Instrument, InstrumentConstituent
from app.platform.models import Workspace
from scripts.repair_idex_isin import RepairRefusedError, RepairTarget, repair_idex_isin

WS = 8601
ISIN = "INE022Q01020"


async def _seed(session) -> dict:
    session.add(Workspace(id=WS, name="Repair"))
    await session.flush()
    idex = Company(workspace_id=WS, name="IDEX Corp", ticker="IEX", isin=ISIN, sector="Financials")
    other = Company(workspace_id=WS, name="Some Other Co", ticker="OTH", sector="Energy")
    hlal = Instrument(workspace_id=WS, symbol="HLAL", name="Wahed HLAL", instrument_type="etf")
    ppfas = Instrument(
        workspace_id=WS, symbol="122639", name="PPFAS", instrument_type="mutual_fund"
    )
    small = Instrument(
        workspace_id=WS, symbol="SMALLCAP", name="Mirae", instrument_type="mutual_fund"
    )
    session.add_all([idex, other, hlal, ppfas, small])
    await session.flush()

    def row(instrument, company, weight):
        return InstrumentConstituent(
            instrument_id=instrument.id,
            constituent_company_id=company.id,
            weight=Decimal(weight),
            as_of_date=date(2026, 9, 30),
            source="test",
            fetched_at=datetime(2026, 11, 1, tzinfo=UTC),
        )

    r_ppfas, r_small, r_hlal = (
        row(ppfas, idex, "0.00620000"),
        row(small, idex, "0.00882363"),
        row(hlal, idex, "0.00050000"),  # genuinely IDEX Corp -- must not move
    )
    session.add_all([r_ppfas, r_small, r_hlal])
    await session.flush()
    return {"idex": idex, "ppfas": r_ppfas, "small": r_small, "hlal": r_hlal}


def _targets(seed) -> list[RepairTarget]:
    return [
        RepairTarget(seed["ppfas"].id, "122639", Decimal("0.0062")),
        RepairTarget(seed["small"].id, "SMALLCAP", Decimal("0.00882363")),
    ]


async def _company(session, **where) -> list[Company]:
    stmt = select(Company)
    for key, value in where.items():
        stmt = stmt.where(getattr(Company, key) == value)
    return list((await session.execute(stmt)).scalars())


@pytest.mark.asyncio
async def test_dry_run_reports_the_plan_and_writes_nothing(override_database_url):
    async with postgres.async_session_maker() as session:
        seed = await _seed(session)
        report = await repair_idex_isin(
            session,
            workspace_id=WS,
            idex_company_id=seed["idex"].id,
            targets=_targets(seed),
            apply=False,
        )
        await session.refresh(seed["idex"])
        assert seed["idex"].isin == ISIN and seed["idex"].sector == "Financials"
        assert await _company(session, isin=ISIN, name="Indian Energy Exchange Ltd") == []

    assert report.applied is False
    assert report.actions, "a dry run must say what it would do"


@pytest.mark.asyncio
async def test_apply_repairs_idex_creates_the_indian_company_and_repoints_only_the_two_rows(
    override_database_url,
):
    async with postgres.async_session_maker() as session:
        seed = await _seed(session)
        report = await repair_idex_isin(
            session,
            workspace_id=WS,
            idex_company_id=seed["idex"].id,
            targets=_targets(seed),
            apply=True,
        )
        for obj in (seed["idex"], seed["ppfas"], seed["small"], seed["hlal"]):
            await session.refresh(obj)

        assert seed["idex"].isin is None and seed["idex"].sector == "Industrials"
        indian = await _company(session, workspace_id=WS, isin=ISIN)
        assert [c.name for c in indian] == ["Indian Energy Exchange Ltd"]
        assert indian[0].ticker == "IEX" and indian[0].sector == "Financials"
        assert seed["ppfas"].constituent_company_id == indian[0].id
        assert seed["small"].constituent_company_id == indian[0].id
        assert seed["hlal"].constituent_company_id == seed["idex"].id, (
            "IDEX Corp's own HLAL holding must not move"
        )
    assert report.applied is True


@pytest.mark.asyncio
async def test_a_second_run_is_a_no_op(override_database_url):
    async with postgres.async_session_maker() as session:
        seed = await _seed(session)
        kwargs = {
            "workspace_id": WS,
            "idex_company_id": seed["idex"].id,
            "targets": _targets(seed),
            "apply": True,
        }
        await repair_idex_isin(session, **kwargs)
        again = await repair_idex_isin(session, **kwargs)

        assert again.actions == [] and again.already_repaired is True
        assert len(await _company(session, workspace_id=WS, isin=ISIN)) == 1


@pytest.mark.asyncio
async def test_refuses_and_writes_nothing_when_a_row_no_longer_matches_what_was_reviewed(
    override_database_url,
):
    async with postgres.async_session_maker() as session:
        seed = await _seed(session)
        drifted = [
            RepairTarget(seed["ppfas"].id, "122639", Decimal("0.0062")),
            RepairTarget(seed["small"].id, "SMALLCAP", Decimal("0.5")),  # weight moved
        ]
        with pytest.raises(RepairRefusedError, match="SMALLCAP"):
            await repair_idex_isin(
                session,
                workspace_id=WS,
                idex_company_id=seed["idex"].id,
                targets=drifted,
                apply=True,
            )
        await session.refresh(seed["idex"])
        await session.refresh(seed["ppfas"])
        assert seed["idex"].isin == ISIN, "a refused run must not detach the ISIN"
        assert seed["ppfas"].constituent_company_id == seed["idex"].id


@pytest.mark.asyncio
async def test_refuses_when_a_row_points_at_a_different_company(override_database_url):
    async with postgres.async_session_maker() as session:
        seed = await _seed(session)
        other = (await _company(session, name="Some Other Co"))[0]
        seed["small"].constituent_company_id = other.id
        await session.flush()

        with pytest.raises(RepairRefusedError, match="not IDEX Corp"):
            await repair_idex_isin(
                session,
                workspace_id=WS,
                idex_company_id=seed["idex"].id,
                targets=_targets(seed),
                apply=True,
            )
