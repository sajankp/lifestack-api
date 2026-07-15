from unittest.mock import AsyncMock, MagicMock

import pytest

from app.investing.models import Company
from app.investing.repository import CompanyRepository, normalize_company_name


def _session_with_candidates(candidates: list[Company]) -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = candidates
    result = MagicMock()
    result.scalars.return_value = scalars
    session.execute.return_value = result
    return session


class TestNormalizeCompanyName:
    def test_collapses_punctuation_and_case(self) -> None:
        assert normalize_company_name("Apple Inc.") == normalize_company_name("Apple Inc")
        assert normalize_company_name("Apple Inc.") == "apple inc"

    def test_collapses_extra_whitespace(self) -> None:
        assert normalize_company_name("  Apple   Inc  ") == "apple inc"


@pytest.mark.asyncio
class TestResolveOrCreateCompany:
    async def test_isin_match_wins_over_ticker(self) -> None:
        existing = Company(
            id=1, workspace_id=1, name="Apple Inc", ticker="OLD", isin="US0378331005"
        )
        session = _session_with_candidates([existing])
        repo = CompanyRepository(session)

        resolved = await repo.resolve_or_create_company(
            1, name="Apple", ticker="AAPL", isin="US0378331005"
        )

        assert resolved is existing
        session.add.assert_not_called()

    async def test_ticker_match_when_no_isin_given(self) -> None:
        existing = Company(id=1, workspace_id=1, name="Apple Inc", ticker="AAPL")
        session = _session_with_candidates([existing])
        repo = CompanyRepository(session)

        resolved = await repo.resolve_or_create_company(1, name="Apple", ticker="AAPL")

        assert resolved is existing

    async def test_ticker_match_backfills_missing_isin(self) -> None:
        existing = Company(id=1, workspace_id=1, name="Apple Inc", ticker="AAPL", isin=None)
        session = _session_with_candidates([existing])
        repo = CompanyRepository(session)

        resolved = await repo.resolve_or_create_company(
            1, name="Apple", ticker="AAPL", isin="US0378331005"
        )

        assert resolved is existing
        assert existing.isin == "US0378331005"
        session.add.assert_called_once_with(existing)

    async def test_normalized_name_collapses_variants(self) -> None:
        existing = Company(id=1, workspace_id=1, name="Apple Inc.")
        session = _session_with_candidates([existing])
        repo = CompanyRepository(session)

        resolved = await repo.resolve_or_create_company(1, name="Apple Inc")

        assert resolved is existing

    async def test_no_match_creates_new_company(self) -> None:
        session = _session_with_candidates([])
        repo = CompanyRepository(session)

        resolved = await repo.resolve_or_create_company(1, name="Brand New Co", ticker="BNC")

        assert resolved.name == "Brand New Co"
        assert resolved.ticker == "BNC"
        session.add.assert_called_once()

    async def test_same_ticker_different_market_does_not_merge(self) -> None:
        existing = Company(id=1, workspace_id=1, name="State Bank", ticker="SBI", country_code="ZA")
        session = _session_with_candidates([existing])
        repo = CompanyRepository(session)

        resolved = await repo.resolve_or_create_company(
            1, name="State Bank of India", ticker="SBI", country_code="IN"
        )

        assert resolved is not existing
        assert resolved.country_code == "IN"
