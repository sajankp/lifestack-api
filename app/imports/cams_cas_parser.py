"""Parser for CAMS Consolidated Account Statement (CAS) PDFs (spec-056).

A CAS PDF is organized as nested sections: one block per folio, one
sub-block per scheme (identified by its ISIN) within that folio, then one
row per transaction. This module extracts the plain text of every page and
walks it line by line, tracking the current (folio, scheme, isin) context
and matching transaction rows against a fixed column layout.

This is deliberately a text-line regex parser, not a table-extraction one —
CAMS/registrar PDF table structures vary enough between AMCs that
``pdfplumber.extract_tables()`` is less reliable in practice than matching
the printed column layout directly.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import pdfplumber

_FOLIO_RE = re.compile(r"Folio No[:\s]+([\w/-]+)", re.IGNORECASE)
_SCHEME_RE = re.compile(
    r"^(?P<name>.+?)\s*\(ISIN:\s*(?P<isin>[A-Z]{2}[A-Z0-9]{9}\d)\)", re.IGNORECASE
)
_TXN_RE = re.compile(
    r"^(?P<date>\d{2}-[A-Za-z]{3}-\d{4})\s+(?P<description>.+?)\s+"
    r"(?P<amount>-?[\d,]+\.\d{2})\s+(?P<units>-?[\d,]+\.\d{3})\s+"
    r"(?P<nav>[\d,]+\.\d{2})\s+(?P<balance>-?[\d,]+\.\d{3})\s*$"
)

# Wide enough that ordinary NAV volatility never trips it, but any 2:1-class
# split/bonus/reverse-split always does (see spec-051).
_DISCONTINUITY_LOW = Decimal("0.6")
_DISCONTINUITY_HIGH = Decimal("1.67")

# CAMS CAS PDFs always use English month abbreviations regardless of the
# generating locale; parse them directly rather than via strptime's "%b",
# which is locale-dependent and would raise ValueError under a non-English
# locale (e.g. LC_TIME=hi_IN).
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _parse_txn_date(value: str) -> datetime:
    day_str, month_str, year_str = value.split("-")
    return datetime(int(year_str), _MONTHS[month_str.lower()], int(day_str), tzinfo=UTC)


def _classify(description: str) -> str | None:
    lowered = description.lower()
    if "redemption" in lowered:
        return "sell"
    if "purchase" in lowered:
        return "buy"
    return None


def _to_decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


@dataclass
class CamsCasParseResult:
    orders: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    corporate_action_suspected: list[dict] = field(default_factory=list)


def parse_cams_cas(file_path: str) -> CamsCasParseResult:
    result = CamsCasParseResult()
    current_folio: str | None = None
    current_scheme: str | None = None
    current_isin: str | None = None
    # Per-ISIN NAV history in encountered (== chronological, per the CAS
    # layout) order, used only for the price-discontinuity heuristic.
    nav_history: dict[str, list[tuple[datetime, Decimal]]] = {}

    lines: list[str] = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(text.split("\n"))

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        folio_match = _FOLIO_RE.search(line)
        if folio_match:
            current_folio = folio_match.group(1)
            continue

        scheme_match = _SCHEME_RE.match(line)
        if scheme_match:
            current_scheme = scheme_match.group("name").strip()
            current_isin = scheme_match.group("isin").upper()
            continue

        txn_match = _TXN_RE.match(line)
        if not txn_match or current_isin is None or current_scheme is None:
            continue

        try:
            txn_date = _parse_txn_date(txn_match.group("date"))
            units = _to_decimal(txn_match.group("units"))
            nav = _to_decimal(txn_match.group("nav"))
        except (InvalidOperation, ValueError, KeyError):
            continue

        description = txn_match.group("description").strip()
        order_type = _classify(description)

        if order_type is None:
            result.skipped.append({
                "folio": current_folio,
                "scheme_name": current_scheme,
                "isin": current_isin,
                "date": txn_date.date().isoformat(),
                "description": description,
                "reason": f"unsupported transaction type: '{description}'",
            })
            continue

        history = nav_history.setdefault(current_isin, [])
        if history:
            prev_date, prev_nav = history[-1]
            if prev_nav > 0:
                ratio = nav / prev_nav
                if ratio < _DISCONTINUITY_LOW or ratio > _DISCONTINUITY_HIGH:
                    result.corporate_action_suspected.append({
                        "symbol": current_isin,
                        "scheme_name": current_scheme,
                        "from_date": prev_date.date().isoformat(),
                        "from_nav": str(prev_nav),
                        "to_date": txn_date.date().isoformat(),
                        "to_nav": str(nav),
                        "ratio": str(ratio),
                    })
        history.append((txn_date, nav))

        result.orders.append({
            "symbol": current_isin,
            "order_type": order_type,
            "instrument_type": "mutual_fund",
            "instrument_name": current_scheme,
            "quantity": str(abs(units)),
            "price_per_unit": str(nav),
            "currency": "INR",
            "occurred_at": txn_date.isoformat(),
            "notes": f"CAMS CAS import — folio {current_folio}",
        })

    return result
