"""Parser for NSDL/CDSL Demat Consolidated Account Statement (CAS) PDFs
(spec-060 NSDL, spec-063 CDSL).

A Demat CAS lists, per demat account, every security held with the exact
share balance the depository holds — no price, no transaction history. This
module extracts the plain text of every page and walks it line by line,
tracking whether the current line is inside the equities holdings section
(the only section parsed; mutual-fund folio sections use CAMS's layout and
are covered by ``cams_cas_parser`` instead) and matching holding rows
against a fixed ISIN-anchored layout.

Like ``cams_cas_parser``, this is a text-line regex parser, not a
table-extraction one — registrar PDF table structures are not reliable
enough for ``pdfplumber.extract_tables()``.

NSDL and CDSL share every part of this walk (statement-date extraction,
multi-account scoping, skip bookkeeping) except the three regexes that
recognize the holdings section and a holding row — see ``_REGISTRARS``.
The CDSL side (spec-063) is best-effort: no real CDSL statement was
available at implementation time, so ``_CDSL_HOLDING_RE`` in particular
should be the first thing revisited once one is tested against it.
"""

import contextlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import pdfplumber

# NSDL's combined CAS separates equities from other asset classes with a
# "Equities (E)" style header; CDSL's statement of holding has no such
# sub-split — the whole statement (spec-063 problem statement) is a single
# "CDSL" section, so its section-open pattern matches that literal label.
_NSDL_SECTION_RE = re.compile(r"^Equities\b", re.IGNORECASE)
_CDSL_SECTION_RE = re.compile(r"^CDSL\s*$", re.IGNORECASE)
# Any other named section (Mutual Fund Folios, Corporate Bonds, ...) ends
# the current holdings section. NSDL CAS section headers look like "Name (X)".
_OTHER_SECTION_RE = re.compile(r"^[A-Za-z][A-Za-z /]+\([A-Z]{1,4}\)\s*$")
# A CAS can cover multiple demat accounts (spec-060 explicitly scopes this
# to "only the chosen target account this pass"); each account's block is
# introduced by a DP ID/Client ID header. CDSL statements identify an
# account the same way (DP ID + Client ID, sometimes labeled BO ID).
_ACCOUNT_HEADER_RE = re.compile(
    r"(?:DP|BO) ID:\s*(?P<dp_id>\S+)\s+Client ID:\s*(?P<client_id>\S+)", re.IGNORECASE
)

_STATEMENT_DATE_RE = re.compile(
    r"Statement Date[:\s]+(?P<date>\d{2}-[A-Za-z]{3}-\d{4})", re.IGNORECASE
)
# NSDL row: ISIN  Security Name  Current Bal.  Market Price  Value(Rs.)
_NSDL_HOLDING_RE = re.compile(
    r"^(?P<isin>IN[A-Z0-9]{9}\d)\s+(?P<name>.+?)\s+(?P<quantity>[\d,]+\.\d{3})(?:\s|$)"
)
# CDSL row: a leading serial number, then ISIN, name, and balance — CDSL's
# column order and decimal precision differ from NSDL's (spec-060's problem
# statement) but the ISIN anchor and the "name then a decimal quantity"
# shape hold across both. See module docstring: unconfirmed against a real
# CDSL statement.
_CDSL_HOLDING_RE = re.compile(
    r"^(?:\d+\s+)?(?P<isin>IN[A-Z0-9]{9}\d)\s+(?P<name>.+?)\s+(?P<quantity>[\d,]+\.\d{2,3})(?:\s|$)"
)

# Registrar detection + the two regexes each registrar's row format needs.
# Order matters only in that both are checked; a statement naming neither
# (or an ambiguous one naming both) is a hard failure, never a guess.
_NSDL_MARKER_RE = re.compile(r"\bNSDL\b", re.IGNORECASE)
_CDSL_MARKER_RE = re.compile(r"\bCDSL\b", re.IGNORECASE)

_REGISTRARS: dict[str, dict] = {
    "nsdl_cas": {"section_re": _NSDL_SECTION_RE, "holding_re": _NSDL_HOLDING_RE},
    "cdsl_cas": {"section_re": _CDSL_SECTION_RE, "holding_re": _CDSL_HOLDING_RE},
}


class UnrecognizedRegistrarError(ValueError):
    """Raised when a Demat CAS PDF names neither (or both) NSDL and CDSL."""


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


def _extract_lines(file_path: str, password: str | None) -> list[str]:
    lines: list[str] = []
    with pdfplumber.open(file_path, password=password or "") as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(text.split("\n"))
    return lines


def detect_registrar(lines: list[str]) -> str:
    """Return ``"nsdl_cas"`` or ``"cdsl_cas"`` from the statement's own text.

    A statement naming neither registrar (unrecognized format) or both
    (ambiguous — e.g. a page referencing the other registrar in passing)
    raises rather than guessing, per spec-063: silently routing to the
    wrong parser would produce a plausible-looking but wrong verification
    report, which is worse than a clean upload-time error.
    """
    # Restrict to the first 50 lines where the issuing registrar's header is
    # guaranteed to appear. Searching the full document risks false positives:
    # NSDL statements sometimes mention CDSL in footers/disclaimers (and vice
    # versa), which would cause both markers to match and trigger an
    # UnrecognizedRegistrarError on a valid statement.
    header_text = "\n".join(lines[:50])
    is_nsdl = bool(_NSDL_MARKER_RE.search(header_text))
    is_cdsl = bool(_CDSL_MARKER_RE.search(header_text))
    if is_nsdl and not is_cdsl:
        return "nsdl_cas"
    if is_cdsl and not is_nsdl:
        return "cdsl_cas"
    raise UnrecognizedRegistrarError(
        "Could not identify this Demat CAS as an NSDL or CDSL statement"
    )


def _walk_lines(
    lines: list[str], section_re: re.Pattern, holding_re: re.Pattern
) -> DematCasParseResult:
    result = DematCasParseResult()
    in_holdings_section = False
    # The first DP ID/Client ID header seen is "the" account for this parse.
    # A CAS listing several demat accounts is scoped out (spec-060): once a
    # DIFFERENT account header appears, holding rows are recorded as skipped
    # rather than silently merged into the target account's report — an
    # un-flagged merge would corrupt the verification with another account's
    # numbers. Demat CAS statements list each account's sections once, in
    # sequence (not interleaved), so this flag is monotonic: it never flips
    # back to "in target account" once left.
    target_account_key: tuple[str, str] | None = None
    in_target_account = True

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if result.statement_date is None:
            date_match = _STATEMENT_DATE_RE.search(line)
            if date_match:
                with contextlib.suppress(ValueError, KeyError):
                    result.statement_date = _parse_cas_date(date_match.group("date"))

        account_match = _ACCOUNT_HEADER_RE.search(line)
        if account_match:
            account_key = (account_match.group("dp_id"), account_match.group("client_id"))
            if target_account_key is None:
                target_account_key = account_key
            elif account_key != target_account_key:
                in_target_account = False
            continue

        if section_re.match(line):
            in_holdings_section = True
            continue
        if _OTHER_SECTION_RE.match(line):
            in_holdings_section = False
            continue

        holding_match = holding_re.match(line)
        if not holding_match:
            continue

        if not in_target_account:
            result.skipped.append({
                "isin": holding_match.group("isin").upper(),
                "reason": "belongs to a different demat account in a multi-account "
                "statement — only the first account is verified this pass",
                "raw_line": line,
            })
            continue

        if not in_holdings_section:
            result.skipped.append({
                "isin": holding_match.group("isin").upper(),
                "reason": "outside the equities holdings section (mutual-fund folios "
                "are covered by the CAMS CAS import, spec-056)",
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


def parse_demat_cas_nsdl(file_path: str, password: str | None = None) -> DematCasParseResult:
    lines = _extract_lines(file_path, password)
    return _walk_lines(lines, _NSDL_SECTION_RE, _NSDL_HOLDING_RE)


def parse_demat_cas_cdsl(file_path: str, password: str | None = None) -> DematCasParseResult:
    lines = _extract_lines(file_path, password)
    return _walk_lines(lines, _CDSL_SECTION_RE, _CDSL_HOLDING_RE)


def parse_demat_cas(file_path: str, password: str | None = None) -> tuple[DematCasParseResult, str]:
    """Detect the registrar (NSDL vs CDSL) and parse accordingly.

    This is the entry point `demat_cas_import.py` calls — spec-060 shipped
    NSDL-only via `parse_demat_cas_nsdl` directly; spec-063 adds CDSL and
    this auto-detecting wrapper on top, without changing either registrar's
    own parsing behavior.
    """
    lines = _extract_lines(file_path, password)
    source = detect_registrar(lines)
    registrar = _REGISTRARS[source]
    result = _walk_lines(lines, registrar["section_re"], registrar["holding_re"])
    return result, source
