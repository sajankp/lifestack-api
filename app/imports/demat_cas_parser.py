"""Parser for NSDL Demat Consolidated Account Statement (CAS) PDFs (spec-060).

An NSDL CAS lists, per demat account, every security held with the exact
share balance the depository holds — no price, no transaction history. This
module extracts the plain text of every page and walks it line by line,
tracking whether the current line is inside the "Equities (E)" section
(the only section parsed; mutual-fund folio sections use CAMS's layout and
are covered by ``cams_cas_parser`` instead) and matching holding rows
against a fixed ISIN-anchored layout.

Like ``cams_cas_parser``, this is a text-line regex parser, not a
table-extraction one — registrar PDF table structures are not reliable
enough for ``pdfplumber.extract_tables()``.
"""

import contextlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import pdfplumber

_EQUITIES_SECTION_RE = re.compile(r"^Equities\b", re.IGNORECASE)
# Any other named section (Mutual Fund Folios, Corporate Bonds, ...) ends
# the Equities section. NSDL CAS section headers look like "Name (X)".
_OTHER_SECTION_RE = re.compile(r"^[A-Za-z][A-Za-z /]+\([A-Z]{1,4}\)\s*$")

_STATEMENT_DATE_RE = re.compile(
    r"Statement Date[:\s]+(?P<date>\d{2}-[A-Za-z]{3}-\d{4})", re.IGNORECASE
)
_HOLDING_RE = re.compile(
    r"^(?P<isin>IN[A-Z0-9]{9}\d)\s+(?P<name>.+?)\s+(?P<quantity>[\d,]+\.\d{3})(?:\s|$)"
)

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


def _parse_cas_date(value: str) -> str:
    day_str, month_str, year_str = value.split("-")
    return (
        datetime(int(year_str), _MONTHS[month_str.lower()], int(day_str), tzinfo=UTC)
        .date()
        .isoformat()
    )


def _to_decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


@dataclass
class DematCasParseResult:
    holdings: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    statement_date: str | None = None


def parse_demat_cas_nsdl(file_path: str, password: str | None = None) -> DematCasParseResult:
    result = DematCasParseResult()
    in_equities = False

    lines: list[str] = []
    with pdfplumber.open(file_path, password=password or "") as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(text.split("\n"))

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if result.statement_date is None:
            date_match = _STATEMENT_DATE_RE.search(line)
            if date_match:
                with contextlib.suppress(ValueError, KeyError):
                    result.statement_date = _parse_cas_date(date_match.group("date"))

        if _EQUITIES_SECTION_RE.match(line):
            in_equities = True
            continue
        if _OTHER_SECTION_RE.match(line):
            in_equities = False
            continue

        holding_match = _HOLDING_RE.match(line)
        if not holding_match:
            continue

        if not in_equities:
            result.skipped.append({
                "isin": holding_match.group("isin").upper(),
                "reason": "outside Equities (E) section (mutual-fund folios are "
                "covered by the CAMS CAS import, spec-056)",
                "raw_line": line,
            })
            continue

        try:
            quantity = _to_decimal(holding_match.group("quantity"))
        except InvalidOperation:
            result.skipped.append({
                "isin": holding_match.group("isin").upper(),
                "reason": "unparseable quantity",
                "raw_line": line,
            })
            continue

        result.holdings.append({
            "isin": holding_match.group("isin").upper(),
            "security_name": holding_match.group("name").strip(),
            "quantity": str(quantity),
        })

    return result
