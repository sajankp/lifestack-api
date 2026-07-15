from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.investing.schemas import InstrumentConstituentCreate


class TestInstrumentConstituentIdentifierMandate:
    def test_name_only_is_rejected(self):
        with pytest.raises(ValidationError, match="company_isin or company_ticker"):
            InstrumentConstituentCreate(company_name="Apple Inc", weight=Decimal("0.5"))

    def test_ticker_alone_is_accepted(self):
        row = InstrumentConstituentCreate(
            company_name="Apple Inc", company_ticker="AAPL", weight=Decimal("0.5")
        )
        assert row.company_ticker == "AAPL"

    def test_isin_alone_is_accepted(self):
        row = InstrumentConstituentCreate(
            company_name="Apple Inc", company_isin="US0378331005", weight=Decimal("0.5")
        )
        assert row.company_isin == "US0378331005"

    def test_whitespace_only_ticker_still_rejected(self):
        """A whitespace-only ticker normalizes to None (`_normalize_identifier`
        strips it), so it must not slip past the mandate as if it were set.
        """
        with pytest.raises(ValidationError, match="company_isin or company_ticker"):
            InstrumentConstituentCreate(
                company_name="Apple Inc", company_ticker="   ", weight=Decimal("0.5")
            )
