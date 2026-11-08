"""Sector vocabulary helpers (spec-100).

Sectors are free text (max 100 chars) but GICS names are recommended. To stop
``"Health Care"``, ``"health care"`` and ``" Health  Care "`` from splitting
into separate allocation buckets, every sector/industry string is normalised
on the way in (writes) and grouped on a case-insensitive key on the way out
(allocation reads, which also covers legacy rows written before this rule).

Normalisation rules:

* leading/trailing whitespace is stripped and runs of internal whitespace
  collapse to one space;
* a case-insensitive match against a GICS sector name (or the reserved
  ``Other / not itemised`` bucket label) is replaced by its canonical
  spelling;
* any other text keeps the caller's casing (we cannot guess proper-noun
  casing), but is still grouped case-insensitively when aggregated.
"""

GICS_SECTORS: tuple[str, ...] = (
    "Information Technology",
    "Health Care",
    "Financials",
    "Industrials",
    "Energy",
    "Consumer Staples",
    "Consumer Discretionary",
    "Materials",
    "Utilities",
    "Communication Services",
    "Real Estate",
)

OTHER_NOT_ITEMISED = "Other / not itemised"

_CANONICAL_BY_KEY: dict[str, str] = {
    name.casefold(): name for name in (*GICS_SECTORS, OTHER_NOT_ITEMISED)
}


def sector_key(value: str) -> str:
    """Case/whitespace-insensitive grouping key for a sector label."""
    return " ".join(value.split()).casefold()


def normalise_sector(value: str | None) -> str | None:
    """Return the canonical form of ``value`` or ``None`` when it is blank."""
    if value is None:
        return None
    collapsed = " ".join(value.split())
    if not collapsed:
        return None
    return _CANONICAL_BY_KEY.get(collapsed.casefold(), collapsed)
