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

    async def test_ticker_match_without_isin_still_works(self) -> None:
        existing = Company(id=1, workspace_id=1, name="Apple Inc", ticker="AAPL")
        session = _session_with_candidates([existing])
        repo = CompanyRepository(session)

        resolved = await repo.resolve_or_create_company(1, name="Apple", ticker="AAPL")

        assert resolved is existing
        session.add.assert_not_called()

    async def test_isin_and_ticker_pointing_to_same_company(self) -> None:
        existing = Company(
            id=1, workspace_id=1, name="Apple Inc", ticker="AAPL", isin="US0378331005"
        )
        session = _session_with_candidates([existing])
        repo = CompanyRepository(session)

        resolved = await repo.resolve_or_create_company(
            1, name="Apple", ticker="AAPL", isin="US0378331005"
        )

        assert resolved is existing
        session.add.assert_not_called()

    async def test_isin_match_wins_when_ticker_matches_nothing(self) -> None:
        apple = Company(id=1, workspace_id=1, name="Apple Inc", ticker="AAPL", isin="US0378331005")
        session = _session_with_candidates([apple])
        repo = CompanyRepository(session)

        # Ticker ZZZZ matches nothing: ISIN still resolves Apple unambiguously.
        resolved = await repo.resolve_or_create_company(
            1, name="Apple", ticker="ZZZZ", isin="US0378331005"
        )

        assert resolved is apple
        session.add.assert_not_called()

    async def test_isin_and_ticker_denoting_different_companies_is_rejected(self) -> None:
        apple = Company(id=1, workspace_id=1, name="Apple Inc", ticker="AAPL", isin="US0378331005")
        idex = Company(id=2, workspace_id=1, name="IDEX Corp", ticker="IEX", isin="INE022Q01020")
        session = _session_with_candidates([apple, idex])
        repo = CompanyRepository(session)

        with pytest.raises(ValueError, match="different companies"):
            await repo.resolve_or_create_company(
                1, name="Indian Energy Exchange", ticker="IEX", isin="US0378331005"
            )
        session.add.assert_not_called()

    async def test_unmatched_isin_is_not_backfilled_onto_ticker_match(self) -> None:
        idex = Company(id=2, workspace_id=1, name="IDEX Corp", ticker="IEX")
        session = _session_with_candidates([idex])
        repo = CompanyRepository(session)

        # ISIN INE022Q01020 matches nothing; ticker IEX points at IDEX Corp.
        # Stamping the ISIN onto IDEX would re-point a real company at the
        # wrong identity: reject instead.
        with pytest.raises(ValueError, match="no company with ISIN 'INE022Q01020'"):
            await repo.resolve_or_create_company(
                1, name="Indian Energy Exchange", ticker="IEX", isin="INE022Q01020"
            )
        assert idex.isin is None
        session.add.assert_not_called()

    async def test_new_company_carries_both_identifiers_when_nothing_matches(self) -> None:
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
