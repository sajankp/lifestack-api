"""Pure value, unit and range rules for health measurements (spec-110 §B).

No database access here: everything is a function of its arguments so the rules are
cheap to test exhaustively.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

VALUE_SCALE = Decimal("0.000001")  # Numeric(18, 6)
_MAX_DECIMAL_PLACES = 6
_MAX_INTEGER_DIGITS = 12

COMPARATORS = ("<=", ">=", "<", ">")
_NUMBER_RE = re.compile(r"^\s*(<=|>=|<|>)?\s*([+-]?\d+(?:\.\d+)?)\s*$")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


class UnitError(ValueError):
    """A unit is missing, unregistered, or not allowed for the metric."""


@dataclass(frozen=True)
class ParsedValue:
    comparator: str | None
    value_num: Decimal | None
    value_text: str | None


def normalise_name(text: str) -> str:
    """Lowercase, punctuation/underscores to single spaces: 'ALT (SGPT)' -> 'alt sgpt'."""
    return _NON_ALNUM_RE.sub(" ", text.lower()).strip()


def slugify_code(text: str) -> str:
    """A valid metric code (``^[a-z][a-z0-9_]{1,63}$``) from a display name."""
    slug = _NON_ALNUM_RE.sub("_", text.lower()).strip("_")
    if not slug:
        raise ValueError(f"Cannot derive a metric code from {text!r}.")
    if not slug[0].isalpha():
        slug = f"m_{slug}"
    slug = slug[:64].rstrip("_")
    if len(slug) < 2:
        raise ValueError(f"Cannot derive a metric code from {text!r}.")
    return slug


def parse_value(raw: str, value_type: str) -> ParsedValue:
    """Parse one result string for a ``numeric`` or ``text`` metric. Raises ValueError."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("value is required.")
    if value_type == "text":
        return ParsedValue(None, None, raw.strip())
    match = _NUMBER_RE.match(raw)
    if match is None:
        raise ValueError(
            f"value {raw.strip()!r} is not a number (optionally prefixed with <, <=, > or >=). "
            "Use a text metric for non-numeric results."
        )
    comparator, number = match.group(1), match.group(2)
    try:
        value = Decimal(number)
    except InvalidOperation as exc:  # pragma: no cover - the regex already guards this
        raise ValueError(f"value {raw.strip()!r} is not a number.") from exc
    _check_precision(value, raw.strip())
    return ParsedValue(comparator, value, None)


def parse_decimal(raw: object, label: str) -> Decimal:
    """Parse a ref-range bound (string or number) with the same precision limits."""
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if not value.is_finite():
        raise ValueError(f"{label} must be a finite number.")
    _check_precision(value, label)
    return value


def _check_precision(value: Decimal, label: str) -> None:
    if not value.is_finite():
        raise ValueError(f"{label} must be a finite number.")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > _MAX_DECIMAL_PLACES:
        raise ValueError(f"{label} has more than {_MAX_DECIMAL_PLACES} decimal places.")
    if len(value.as_tuple().digits) + (exponent if isinstance(exponent, int) else 0) > (
        _MAX_INTEGER_DIGITS
    ):
        raise ValueError(f"{label} is too large.")


def normalise_unit(unit: str | None) -> str:
    return " ".join((unit or "").split()).lower()


def to_canonical(
    value: Decimal,
    unit: str | None,
    canonical_unit: str | None,
    conversions: dict[str, dict[str, str]] | None,
) -> Decimal:
    """Convert ``value`` from ``unit`` to the metric's canonical unit.

    Never guesses: a missing unit on a metric with a canonical unit, or an unregistered
    unit, raises ``UnitError`` listing what is accepted.
    """
    conversions = conversions or {}
    given = normalise_unit(unit)
    if canonical_unit is None:
        if given:
            raise UnitError(f"This metric is unitless; got unit {unit!r}.")
        return value
    if not given:
        raise UnitError(f"unit is required for this metric (canonical unit: {canonical_unit}).")
    if given == normalise_unit(canonical_unit):
        return value
    for registered, rule in conversions.items():
        if normalise_unit(registered) == given:
            factor = Decimal(str(rule["factor"]))
            offset = Decimal(str(rule.get("offset", "0")))
            return (value * factor + offset).quantize(VALUE_SCALE)
    accepted = ", ".join([canonical_unit, *conversions.keys()])
    raise UnitError(f"Unit {unit!r} is not registered for this metric. Accepted units: {accepted}.")


def validate_conversions(raw: object) -> dict[str, dict[str, str]]:
    """Normalise ``{unit: {"factor": n, "offset": n}}`` to string numbers. Raises ValueError."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("unit_conversions must be an object keyed by unit.")
    cleaned: dict[str, dict[str, str]] = {}
    seen: set[str] = set()
    for unit, rule in raw.items():
        key = str(unit).strip()
        if not key or len(key) > 32:
            raise ValueError("Conversion units must be 1-32 characters.")
        if normalise_unit(key) in seen:
            raise ValueError(f"Duplicate conversion unit {key!r}.")
        seen.add(normalise_unit(key))
        if not isinstance(rule, dict) or "factor" not in rule:
            raise ValueError(f"Conversion for {key!r} needs a 'factor' (and optional 'offset').")
        try:
            factor = Decimal(str(rule["factor"]))
            offset = Decimal(str(rule.get("offset", "0")))
        except InvalidOperation as exc:
            raise ValueError(f"Conversion for {key!r} must use numbers.") from exc
        if not factor.is_finite() or not offset.is_finite() or factor == 0:
            raise ValueError(f"Conversion for {key!r} needs a finite, non-zero factor.")
        cleaned[key] = {"factor": format(factor, "f"), "offset": format(offset, "f")}
    return cleaned


def range_flag(
    value_num: Decimal | None,
    comparator: str | None,
    ref_low: Decimal | None,
    ref_high: Decimal | None,
) -> str | None:
    """``low`` / ``normal`` / ``high`` against the range printed on the report, else None.

    Threshold rows (``<0.5``) and text rows are never flagged. This is arithmetic on the
    report's own range, not an interpretation (spec-069 trust boundary).
    """
    if value_num is None or comparator is not None:
        return None
    if ref_low is None and ref_high is None:
        return None
    if ref_low is not None and value_num < ref_low:
        return "low"
    if ref_high is not None and value_num > ref_high:
        return "high"
    return "normal"
