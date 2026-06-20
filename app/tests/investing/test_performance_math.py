from decimal import Decimal

from app.investing.service import _value_change


def test_value_change_calculates_positive_and_negative_returns():
    assert _value_change(Decimal("125.00"), Decimal("100.00")) == (
        Decimal("25.00"),
        Decimal("25.00"),
    )
    assert _value_change(Decimal("80.00"), Decimal("100.00")) == (
        Decimal("-20.00"),
        Decimal("-20.00"),
    )


def test_value_change_keeps_amount_but_omits_percentage_for_zero_baseline():
    assert _value_change(Decimal("50.00"), Decimal("0")) == (Decimal("50.00"), None)
