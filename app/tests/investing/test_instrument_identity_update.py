import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.investing.models import Company, Instrument
from app.investing.repository import CompanyRepository, InstrumentRepository
from app.investing.schemas import InstrumentType, InstrumentUpdate
from app.investing.service import InstrumentService


def _service_for(
    instrument: Instrument, companies: list[Company]
) -> tuple[InstrumentService, AsyncMock]:
    instrument_repo = AsyncMock(spec=InstrumentRepository)
    instrument_repo.get_by_public_id.return_value = instrument
    instrument_repo.save.side_effect = lambda inst: inst

    company_session = AsyncMock()
    company_session.add = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = companies
    result = MagicMock()
    result.scalars.return_value = scalars
    company_session.execute.return_value = result
    company_repo = CompanyRepository(company_session)

    return InstrumentService(instrument_repo, company_repo), company_session


@pytest.mark.asyncio
async def test_update_instrument_ticker_repoints_without_mutating_shared_company() -> None:
    """spec-083 §5.2: changing ticker/isin on an instrument whose Company is
    shared by a second instrument must repoint only the edited instrument's
    company_id — the shared Company row (and the other instrument's identity)
    stays untouched.
    """
    shared_company = Company(id=1, workspace_id=7, name="Old Co", ticker="OLDT")
    other_company = Company(id=2, workspace_id=7, name="New Co", ticker="NEWT")
    instrument = Instrument(
        id=10,
        public_id=uuid.uuid4(),
        workspace_id=7,
        symbol="OLDT",
        name="Old Co",
        instrument_type=InstrumentType.stock.value,
        company_id=shared_company.id,
    )

    service, _ = _service_for(instrument, [shared_company, other_company])

    updated = await service.update_instrument(
        7, instrument.public_id, InstrumentUpdate(ticker="NEWT")
    )

    assert updated.company_id == other_company.id
    # The old shared company row must be untouched — no ticker overwrite.
    assert shared_company.ticker == "OLDT"
    assert shared_company.name == "Old Co"


@pytest.mark.asyncio
async def test_update_instrument_isin_resolves_to_existing_company_by_isin() -> None:
    existing_by_isin = Company(id=3, workspace_id=7, name="Apple Inc", isin="US0378331005")
    instrument = Instrument(
        id=11,
        public_id=uuid.uuid4(),
        workspace_id=7,
        symbol="AAPL",
        name="Apple",
        instrument_type=InstrumentType.stock.value,
        company_id=None,
    )

    service, _ = _service_for(instrument, [existing_by_isin])

    updated = await service.update_instrument(
        7, instrument.public_id, InstrumentUpdate(isin="US0378331005")
    )

    assert updated.company_id == existing_by_isin.id
    assert updated.isin == "US0378331005"


@pytest.mark.asyncio
async def test_update_instrument_name_only_does_not_touch_company() -> None:
    company = Company(id=1, workspace_id=7, name="Old Co", ticker="OLDT")
    instrument = Instrument(
        id=10,
        public_id=uuid.uuid4(),
        workspace_id=7,
        symbol="OLDT",
        name="Old Co",
        instrument_type=InstrumentType.stock.value,
        company_id=company.id,
    )

    service, session = _service_for(instrument, [company])

    updated = await service.update_instrument(
        7, instrument.public_id, InstrumentUpdate(name="Old Co Renamed")
    )

    assert updated.name == "Old Co Renamed"
    assert updated.company_id == company.id
    session.execute.assert_not_called()
