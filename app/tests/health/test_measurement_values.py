"""Pure value/unit/range rules for health measurements (spec-111 §B)."""

from decimal import Decimal

import pytest

from app.health.measurements.values import (
    ParsedValue,
    UnitError,
    normalise_name,
    parse_value,
    range_flag,
    slugify_code,
    to_canonical,
)


class TestNormaliseName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ALT (SGPT)", "alt sgpt"),
            ("  Vitamin   D, 25-OH ", "vitamin d 25 oh"),
            ("body_weight", "body weight"),
            ("HbA1c", "hba1c"),
        ],
    )
    def test_collapses_case_punctuation_and_underscores(self, raw, expected):
        assert normalise_name(raw) == expected


class TestSlugifyCode:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("LDL Cholesterol", "ldl_cholesterol"),
            ("ALT (SGPT)", "alt_sgpt"),
            ("25-OH Vitamin D", "m_25_oh_vitamin_d"),
            ("  spaced  out ", "spaced_out"),
        ],
    )
    def test_slug_is_valid_code(self, raw, expected):
        assert slugify_code(raw) == expected

    def test_slug_is_capped_at_64_chars(self):
        assert len(slugify_code("a" * 200)) == 64

    def test_unslugifiable_name_is_rejected(self):
        with pytest.raises(ValueError):
            slugify_code("!!!")


class TestParseValue:
    @pytest.mark.parametrize(
        ("raw", "comparator", "num"),
        [
            ("72.4", None, Decimal("72.4")),
            ("  5 ", None, Decimal("5")),
            ("-3.5", None, Decimal("-3.5")),
            ("<0.5", "<", Decimal("0.5")),
            ("> 100", ">", Decimal("100")),
            (">=7.25", ">=", Decimal("7.25")),
            ("<=1", "<=", Decimal("1")),
        ],
    )
    def test_numeric_metric_parses_comparator_and_number(self, raw, comparator, num):
        assert parse_value(raw, "numeric") == ParsedValue(comparator, num, None)

    @pytest.mark.parametrize("raw", ["Negative", "5 mg", "1,5", "", "  ", "<", "1.2.3", "NaN"])
    def test_numeric_metric_rejects_non_numbers(self, raw):
        with pytest.raises(ValueError):
            parse_value(raw, "numeric")

    def test_numeric_metric_rejects_more_than_six_decimal_places(self):
        with pytest.raises(ValueError, match="decimal places"):
            parse_value("1.1234567", "numeric")

    def test_text_metric_keeps_text_verbatim_trimmed(self):
        assert parse_value("  Negative ", "text") == ParsedValue(None, None, "Negative")

    def test_text_metric_rejects_empty(self):
        with pytest.raises(ValueError):
            parse_value("   ", "text")


class TestToCanonical:
    def test_same_unit_is_unchanged_case_insensitively(self):
        assert to_canonical(Decimal("95"), "MG/DL", "mg/dL", {}) == Decimal("95")

    def test_factor_conversion(self):
        conv = {"mmol/L": {"factor": "18.0182", "offset": "0"}}
        assert to_canonical(Decimal("5.5"), "mmol/l", "mg/dL", conv) == Decimal("99.1001")

    def test_affine_conversion_with_offset(self):
        conv = {"F": {"factor": "0.5555555556", "offset": "-17.7777777778"}}
        assert to_canonical(Decimal("98.6"), "F", "C", conv) == Decimal("37.000000")

    def test_unknown_unit_lists_registered_units(self):
        conv = {"mmol/L": {"factor": "18.0182", "offset": "0"}}
        with pytest.raises(UnitError) as exc:
            to_canonical(Decimal("5"), "g/L", "mg/dL", conv)
        message = str(exc.value)
        assert "g/L" in message
        assert "mg/dL" in message
        assert "mmol/L" in message

    def test_missing_unit_is_rejected_when_metric_has_a_canonical_unit(self):
        with pytest.raises(UnitError, match="unit is required"):
            to_canonical(Decimal("5"), None, "mg/dL", {})

    def test_unitless_metric_accepts_only_empty_unit(self):
        assert to_canonical(Decimal("7.4"), None, None, {}) == Decimal("7.4")
        assert to_canonical(Decimal("7.4"), "", None, {}) == Decimal("7.4")
        with pytest.raises(UnitError, match="unitless"):
            to_canonical(Decimal("7.4"), "pH", None, {})


class TestRangeFlag:
    @pytest.mark.parametrize(
        ("value", "low", "high", "expected"),
        [
            ("5", "4", "6", "normal"),
            ("4", "4", "6", "normal"),
            ("6", "4", "6", "normal"),
            ("3.9", "4", "6", "low"),
            ("6.1", "4", "6", "high"),
            ("250", None, "200", "high"),
            ("150", None, "200", "normal"),
            ("8", "10", None, "low"),
            ("12", "10", None, "normal"),
        ],
    )
    def test_flags_against_the_reports_own_range(self, value, low, high, expected):
        flag = range_flag(
            Decimal(value), None, Decimal(low) if low else None, Decimal(high) if high else None
        )
        assert flag == expected

    def test_no_range_gives_none(self):
        assert range_flag(Decimal("5"), None, None, None) is None

    def test_comparator_rows_are_never_flagged(self):
        assert range_flag(Decimal("0.5"), "<", None, Decimal("5")) is None

    def test_text_rows_are_never_flagged(self):
        assert range_flag(None, None, Decimal("1"), Decimal("2")) is None
