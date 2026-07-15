from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.investing.models import ReferenceSecurity
from app.investing.reference_resolve_service import ReferenceResolveService


def _security(**overrides) -> ReferenceSecurity:
    base = {
        "id": 1,
        "isin": "US0378331005",
        "ticker": "AAPL",
        "exchange": "XNAS",
        "amfi_code": None,
        "security_type": "stock",
        "name": "Apple Inc",
        "aliases": [],
        "country_code": "US",
        "source": "bundled:manual",
        "fetched_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ReferenceSecurity(**base)


@pytest.mark.asyncio
class TestReferenceResolveService:
    async def test_resolves_by_isin(self):
        repo = AsyncMock()
        repo.get_by_isin.return_value = _security()
        service = ReferenceResolveService(repo)

        match, status = await service.resolve(isin="US0378331005")

        assert status == "resolved"
        assert match.ticker == "AAPL"
        repo.get_by_ticker_exchange.assert_not_awaited()

    async def test_resolves_by_ticker_and_exchange(self):
        repo = AsyncMock()
        repo.get_by_isin.return_value = None
        repo.get_by_ticker_exchange.return_value = _security()
        service = ReferenceResolveService(repo)

        match, status = await service.resolve(ticker="AAPL", exchange="XNAS")

        assert status == "resolved"
        assert match is not None

    async def test_ambiguous_ticker_without_exchange(self):
        repo = AsyncMock()
        repo.get_by_ticker_exchange.return_value = None
        repo.list_by_ticker.return_value = [
            _security(id=1, exchange="XNSE", country_code="IN"),
            _security(id=2, exchange="XBOM", country_code="IN"),
        ]
        service = ReferenceResolveService(repo)

        match, status = await service.resolve(ticker="RELIANCE")

        assert status == "ambiguous"
        assert match is None

    async def test_unresolved_when_nothing_matches_and_api_disabled(self):
        repo = AsyncMock()
        repo.get_by_ticker_exchange.return_value = None
        repo.list_by_ticker.return_value = []
        service = ReferenceResolveService(repo)

        with patch("app.investing.reference_resolve_service.settings") as mock_settings:
            mock_settings.REFERENCE_DATA_API_ENABLED = False
            match, status = await service.resolve(ticker="NOPE")

        assert status == "unresolved"
        assert match is None

    async def test_api_fallback_caches_result_when_enabled(self):
        repo = AsyncMock()
        repo.get_by_ticker_exchange.return_value = None
        repo.list_by_ticker.return_value = []
        cached = _security(ticker="NEWCO", name="New Co")
        repo.upsert.return_value = cached
        service = ReferenceResolveService(repo)

        identity = {
            "ticker": "NEWCO",
            "name": "New Co",
            "exchange": "NASDAQ",
            "country_code": None,
            "security_type": "stock",
        }
        with (
            patch("app.investing.reference_resolve_service.settings") as mock_settings,
            patch(
                "app.investing.reference_resolve_service.fetch_yahoo_identity",
                return_value=identity,
            ),
        ):
            mock_settings.REFERENCE_DATA_API_ENABLED = True
            match, status = await service.resolve(ticker="NEWCO")

        assert status == "resolved"
        assert match is cached
        repo.upsert.assert_awaited_once()
        upserted = repo.upsert.await_args.args[0]
        assert upserted.source == "api:yahoo"

    async def test_no_identifiers_given_is_unresolved(self):
        repo = AsyncMock()
        service = ReferenceResolveService(repo)

        with patch("app.investing.reference_resolve_service.settings") as mock_settings:
            mock_settings.REFERENCE_DATA_API_ENABLED = False
            match, status = await service.resolve()

        assert status == "unresolved"
        assert match is None
        repo.get_by_isin.assert_not_awaited()
