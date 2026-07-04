"""Pure FX / currency-conversion helpers shared across investing modules.

These functions are stateless — they do not need a DB session or repository.
They take a pre-fetched ``fx_lookup`` dict (keyed by ``(from_currency,
to_currency)`` tuples) and return Decimal amounts or rate strings.

Moving them here (from ``app/investing/service.py``) allows both
``app/investing/service.py`` and ``app/investing/performance_service.py`` to
import from a single authoritative location, and makes them available to the
``finance`` module if currency helpers are needed there in future.
"""

from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.finance.models import FxRate


def build_required_pairs(
    used_currencies: list[str], reporting_currency: str
) -> set[tuple[str, str]]:
    """Return the full set of (from, to) currency pairs needed to convert
    *used_currencies* into *reporting_currency*, routing through USD when a
    direct pair is unavailable.
    """
    required_pairs: set[tuple[str, str]] = set()
    all_currencies = set(used_currencies) | {reporting_currency}
    for curr in all_currencies:
        if curr == "USD":
            continue
        required_pairs.add((curr, "USD"))
        required_pairs.add(("USD", curr))
    for curr in used_currencies:
        if curr == reporting_currency:
            continue
        required_pairs.add((curr, reporting_currency))
        required_pairs.add((reporting_currency, curr))
    return required_pairs


def conversion_rate(
    source_currency: str,
    reporting_currency: str,
    fx_lookup: dict[tuple[str, str], FxRate],
) -> Decimal | None:
    """Return the conversion rate from *source_currency* to
    *reporting_currency*, or ``None`` when no path can be found.

    Resolution order:
    1. Direct pair.
    2. Inverse of the direct pair.
    3. Cross via USD (source→USD→reporting).
    """
    if source_currency == reporting_currency:
        return Decimal("1")

    direct = fx_lookup.get((source_currency, reporting_currency))
    if direct is not None:
        return Decimal(str(direct.rate))

    inverse = fx_lookup.get((reporting_currency, source_currency))
    if inverse is not None:
        inverse_rate = Decimal(str(inverse.rate))
        if inverse_rate != 0:
            return Decimal("1") / inverse_rate

    def rate_to_usd(currency: str) -> Decimal | None:
        if currency == "USD":
            return Decimal("1")
        direct_to_usd = fx_lookup.get((currency, "USD"))
        if direct_to_usd is not None:
            return Decimal(str(direct_to_usd.rate))
        usd_to_currency = fx_lookup.get(("USD", currency))
        if usd_to_currency is not None:
            usd_to_currency_rate = Decimal(str(usd_to_currency.rate))
            if usd_to_currency_rate != 0:
                return Decimal("1") / usd_to_currency_rate
        return None

    source_to_usd = rate_to_usd(source_currency)
    reporting_to_usd = rate_to_usd(reporting_currency)
    if source_to_usd is not None and reporting_to_usd is not None and reporting_to_usd != 0:
        return source_to_usd / reporting_to_usd

    return None


def convert_amount(
    amount: Decimal,
    source_currency: str,
    reporting_currency: str,
    fx_lookup: dict[tuple[str, str], FxRate],
) -> Decimal | None:
    """Convert *amount* from *source_currency* to *reporting_currency*.
    Returns ``None`` when no conversion path is available.
    """
    rate = conversion_rate(source_currency, reporting_currency, fx_lookup)
    if rate is None:
        return None
    return amount * rate


def fx_rates_used(
    used_currencies: list[str],
    reporting_currency: str,
    fx_lookup: dict[tuple[str, str], FxRate],
) -> dict[str, str]:
    """Build a ``{currency: rate_str}`` mapping for all *used_currencies*
    that differ from *reporting_currency*.  Used to populate the
    ``fx_rates_used`` field in API responses.
    """
    rates: dict[str, str] = {}
    for curr in used_currencies:
        if curr == reporting_currency:
            continue
        rate = conversion_rate(curr, reporting_currency, fx_lookup)
        if rate is not None:
            rates[curr] = str(rate)
    return rates
