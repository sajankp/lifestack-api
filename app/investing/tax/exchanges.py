"""Exchange-name to MIC code and country.

The reference security master uses ISO 10383 MIC codes (``XNAS``,
``XNSE``). Users and brokers use names (``NASDAQ``, ``NSE``). This map
translates between them.

**An unrecognised name yields no country.** Guessing a country would
silently pick a tax regime, which is the one error this feature must not
make.
"""

from __future__ import annotations

# Canonical exchange name -> (MIC code, ISO country code)
EXCHANGE_MAP: dict[str, tuple[str, str]] = {
    # India
    "NSE": ("XNSE", "IN"),
    "NSE INDIA": ("XNSE", "IN"),
    "XNSE": ("XNSE", "IN"),
    "BSE": ("XBOM", "IN"),
    "BSE INDIA": ("XBOM", "IN"),
    "XBOM": ("XBOM", "IN"),
    # United States
    "NASDAQ": ("XNAS", "US"),
    "XNAS": ("XNAS", "US"),
    "NYSE": ("XNYS", "US"),
    "XNYS": ("XNYS", "US"),
    "NYSE ARCA": ("ARCX", "US"),
    "ARCA": ("ARCX", "US"),
    "ARCX": ("ARCX", "US"),
    "AMEX": ("XASE", "US"),
    "NYSE AMERICAN": ("XASE", "US"),
    "XASE": ("XASE", "US"),
    # United Kingdom
    "LSE": ("XLON", "GB"),
    "XLON": ("XLON", "GB"),
}


def resolve_exchange(name: str | None) -> tuple[str | None, str | None]:
    """Map an exchange name to ``(mic_code, country_code)``.

    Returns ``(None, None)`` for an unrecognised or missing name — never a
    guess.
    """
    if not name:
        return None, None
    key = name.strip().upper()
    hit = EXCHANGE_MAP.get(key)
    if hit is None:
        return None, None
    return hit


def country_for_exchange(name: str | None) -> str | None:
    """The ISO country code for an exchange name, or ``None`` if unknown."""
    return resolve_exchange(name)[1]
