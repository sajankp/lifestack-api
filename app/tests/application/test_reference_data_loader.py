from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.application.reference_data_loader import (
    SecuritiesJsonValidationError,
    load_reference_securities,
    validate_securities_payload,
)
from app.core.database import postgres
from app.investing.models import ReferenceSecurity
from app.investing.repository import ReferenceSecurityRepository


def _entry(**overrides) -> dict:
    base = {
        "isin": "US0378331005",
        "ticker": "AAPL",
        "exchange": "XNAS",
        "amfi_code": None,
        "security_type": "stock",
        "name": "Apple Inc",
        "aliases": ["Apple Inc.", "Apple"],
        "country_code": "US",
        "source": "bundled:manual",
    }
    base.update(overrides)
    return base


class TestValidateSecuritiesPayload:
    def test_valid_payload_passes(self):
        entries = validate_securities_payload({"version": "1", "securities": [_entry()]})
        assert len(entries) == 1

    def test_missing_securities_key_raises(self):
        with pytest.raises(SecuritiesJsonValidationError):
            validate_securities_payload({"version": "1"})

    def test_missing_required_field_raises(self):
        bad = _entry()
        del bad["name"]
        with pytest.raises(SecuritiesJsonValidationError, match="missing required fields"):
            validate_securities_payload({"securities": [bad]})

    def test_invalid_security_type_raises(self):
        bad = _entry(security_type="bond")
        with pytest.raises(SecuritiesJsonValidationError, match="invalid security_type"):
            validate_securities_payload({"securities": [bad]})

    def test_no_identifier_raises(self):
        bad = _entry(isin=None, ticker=None, amfi_code=None)
        with pytest.raises(SecuritiesJsonValidationError, match="no isin, ticker, or amfi_code"):
            validate_securities_payload({"securities": [bad]})

    def test_empty_name_raises(self):
        bad = _entry(name="")
        with pytest.raises(SecuritiesJsonValidationError, match="empty name"):
            validate_securities_payload({"securities": [bad]})


@pytest.mark.asyncio
async def test_load_creates_then_is_idempotent(override_database_url):
    payload = {
        "version": "test",
        "securities": [
            _entry(),
            _entry(
                isin="INF209K01165",
                ticker=None,
                exchange=None,
                amfi_code="100033",
                security_type="mutual_fund",
                name="Aditya Birla Sun Life Large & Mid Cap Fund",
                aliases=[],
                country_code="IN",
                source="bundled:amfi",
            ),
        ],
    }

    with patch("app.application.reference_data_loader._read_bundled_json", return_value=payload):
        async with postgres.async_session_maker() as session:
            summary = await load_reference_securities(session)
            assert summary.total_entries == 2
            assert summary.created == 2
            assert summary.updated == 0

        async with postgres.async_session_maker() as session:
            rows = (await session.execute(select(ReferenceSecurity))).scalars().all()
            assert len(rows) == 2

        # Re-run: nothing changed -> idempotent, no duplicate rows.
        async with postgres.async_session_maker() as session:
            summary = await load_reference_securities(session)
            assert summary.created == 0
            assert summary.updated == 0
            assert summary.unchanged == 2

        async with postgres.async_session_maker() as session:
            rows = (await session.execute(select(ReferenceSecurity))).scalars().all()
            assert len(rows) == 2


@pytest.mark.asyncio
async def test_load_updates_changed_entry(override_database_url):
    payload_v1 = {"version": "1", "securities": [_entry(name="Apple Inc")]}
    payload_v2 = {"version": "2", "securities": [_entry(name="Apple Incorporated")]}

    with patch("app.application.reference_data_loader._read_bundled_json", return_value=payload_v1):
        async with postgres.async_session_maker() as session:
            await load_reference_securities(session)

    with patch("app.application.reference_data_loader._read_bundled_json", return_value=payload_v2):
        async with postgres.async_session_maker() as session:
            summary = await load_reference_securities(session)
            assert summary.updated == 1
            assert summary.created == 0

    async with postgres.async_session_maker() as session:
        rows = (await session.execute(select(ReferenceSecurity))).scalars().all()
        assert len(rows) == 1
        assert rows[0].name == "Apple Incorporated"


@pytest.mark.asyncio
async def test_aliases_resolve_to_canonical_record(override_database_url):
    payload = {"version": "1", "securities": [_entry()]}
    with patch("app.application.reference_data_loader._read_bundled_json", return_value=payload):
        async with postgres.async_session_maker() as session:
            await load_reference_securities(session)

    async with postgres.async_session_maker() as session:
        repo = ReferenceSecurityRepository(session)
        match = await repo.find_by_normalized_name("Apple Inc.")
        assert match is not None
        assert match.ticker == "AAPL"

        match_alias = await repo.find_by_normalized_name("Apple")
        assert match_alias is not None
        assert match_alias.isin == "US0378331005"


@pytest.mark.asyncio
async def test_load_reads_the_real_bundled_file(override_database_url):
    """No patching here: exercises the actual `securities.json` shipped in
    the package via `importlib.resources`, proving the loader works fully
    offline against the real bundled data (spec-083 §7.1/§7.4).
    """
    async with postgres.async_session_maker() as session:
        summary = await load_reference_securities(session)
        assert summary.total_entries > 15000  # AMFI + NSE + S&P + curated ETFs
        assert summary.created == summary.total_entries
        assert not summary.errors
