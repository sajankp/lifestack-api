"""FxRateRepository.get_latest_rates_for_pairs must not load the whole rate history.

Production measured ~315 fx_rates rows per call (25,522 rows over 81 calls) and growing daily:
the query fetched every historical row of every requested base x quote combination and picked the
latest in Python. Cost is observed directly as the number of FxRate rows the database hands back.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.core.database import postgres
from app.finance.models import Currency, FxRate
from app.finance.repository import FxRateRepository

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


async def _ensure_currencies(session, *codes: str) -> None:
    for code in codes:
        if await session.get(Currency, code) is None:
            session.add(Currency(code=code, name=code))
    await session.flush()


def _history(base: str, quote: str, days: int, newest_rate: Decimal) -> list[FxRate]:
    """``days`` daily system rows; day 0 (NOW) carries ``newest_rate``, older ones differ."""
    return [
        FxRate(
            base_currency_code=base,
            quote_currency_code=quote,
            rate=newest_rate if d == 0 else newest_rate - Decimal(d) / Decimal("100"),
            as_of=NOW - timedelta(days=d),
            fetched_at=NOW,
            source="test",
        )
        for d in range(days)
    ]


class _LoadCounter:
    """Counts FxRate instances materialised from query results (one per row returned)."""

    def __enter__(self):
        self.loaded = 0
        event.listen(FxRate, "load", self._on_load)
        return self

    def __exit__(self, *exc):
        event.remove(FxRate, "load", self._on_load)

    def _on_load(self, target, context):
        self.loaded += 1


@pytest.mark.asyncio
async def test_latest_rate_for_a_pair_loads_one_row_not_the_whole_history(override_database_url):
    async with postgres.async_session_maker() as session:
        await _ensure_currencies(session, "USD", "INR")
        session.add_all(_history("USD", "INR", days=60, newest_rate=Decimal("84")))
        await session.flush()

        with _LoadCounter() as counter:
            latest = await FxRateRepository(session).get_latest_rates_for_pairs([("USD", "INR")])

        assert latest[("USD", "INR")].rate == Decimal("84")
        assert counter.loaded == 1


@pytest.mark.asyncio
async def test_only_the_requested_pairs_are_fetched_not_their_cross_product(override_database_url):
    async with postgres.async_session_maker() as session:
        await _ensure_currencies(session, "USD", "INR", "EUR", "GBP")
        session.add_all(
            _history("USD", "INR", days=30, newest_rate=Decimal("84"))
            + _history("EUR", "GBP", days=30, newest_rate=Decimal("0.85"))
            # Not requested, but base x quote of the requested pairs would sweep them in.
            + _history("USD", "GBP", days=30, newest_rate=Decimal("0.79"))
            + _history("EUR", "INR", days=30, newest_rate=Decimal("90"))
        )
        await session.flush()

        with _LoadCounter() as counter:
            latest = await FxRateRepository(session).get_latest_rates_for_pairs([
                ("USD", "INR"),
                ("EUR", "GBP"),
                ("usd", "inr"),
            ])

        assert {k: v.rate for k, v in latest.items()} == {
            ("USD", "INR"): Decimal("84"),
            ("EUR", "GBP"): Decimal("0.85"),
        }
        assert counter.loaded == 2


@pytest.mark.asyncio
async def test_as_of_picks_the_newest_rate_on_or_before_that_instant(override_database_url):
    async with postgres.async_session_maker() as session:
        await _ensure_currencies(session, "USD", "INR")
        session.add_all(_history("USD", "INR", days=30, newest_rate=Decimal("84")))
        await session.flush()

        latest = await FxRateRepository(session).get_latest_rates_for_pairs(
            [("USD", "INR")], as_of=NOW - timedelta(days=10, hours=1)
        )

        # Newest row on or before NOW-10d-1h is the NOW-11d row.
        assert latest[("USD", "INR")].as_of == NOW - timedelta(days=11)
        assert latest[("USD", "INR")].rate == Decimal("84") - Decimal("11") / Decimal("100")


@pytest.mark.asyncio
async def test_no_pairs_returns_empty_without_querying(override_database_url):
    async with postgres.async_session_maker() as session:
        assert await FxRateRepository(session).get_latest_rates_for_pairs([]) == {}
        assert await FxRateRepository(session).get_latest_rates_for_pairs([("", "INR")]) == {}
