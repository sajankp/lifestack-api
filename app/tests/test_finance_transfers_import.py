"""Unit tests for finance_transfers import row validation and template.

Regression suite for the module-guessing bug: when the CSV omits
from_module/to_module, the validator previously defaulted to
spending->investing regardless of the actual account types, so a
brokerage->bank transfer got a bogus to_module='investing' that wrote an
investing_cash_balances snapshot for the bank account during commit.
"""

import uuid

from app.imports.finance_transfers_import import (
    TEMPLATE_ROW,
    validate_finance_transfer_row,
)
from app.imports.models import ImportModule
from app.imports.schemas import TEMPLATE_HEADERS


def _maps(account_types: dict[str, str]):
    """Build the lookup maps the validator consumes, keyed by lowercased name."""
    names = list(account_types)
    order_account_pub_map = {n: uuid.uuid4() for n in names}
    account_map = {n: i + 1 for i, n in enumerate(names)}
    account_type_map = dict(account_types)
    return order_account_pub_map, account_map, account_type_map


def _row(**overrides) -> dict:
    row = {
        "occurred_at": "2026-05-01T09:30:00Z",
        "from_account": "ICICI",
        "to_account": "HDFC",
        "from_currency": "INR",
        "to_currency": "INR",
        "gross_amount": "1000.00",
        "net_amount_received": "1000.00",
        "notes": "test",
    }
    row.update(overrides)
    return row


def _validate(row, account_types):
    errors = []
    order_map, account_map, type_map = _maps(account_types)
    payload, _weight = validate_finance_transfer_row(
        row,
        errors.append,
        order_account_pub_map=order_map,
        account_map=account_map,
        currency_set={"INR"},
        account_type_map=type_map,
    )
    return payload, errors


def test_missing_modules_derive_from_account_types():
    """Bank->bank with no module columns must derive spending->spending.

    The old default guessed to_module='investing' for any bank destination,
    and commit then wrote an investing cash snapshot for the bank account.
    """
    payload, errors = _validate(
        _row(),
        account_types={"icici": "bank", "hdfc": "bank"},
    )
    assert errors == []
    assert payload["from_module"] == "spending"
    assert payload["to_module"] == "spending"


def test_brokerage_to_bank_derives_investing_to_spending():
    """Brokerage->bank must derive investing->spending, not spending->investing."""
    payload, errors = _validate(
        _row(from_account="GROWW", to_account="ICICI"),
        account_types={"groww": "brokerage", "icici": "bank"},
    )
    assert errors == []
    assert payload["from_module"] == "investing"
    assert payload["to_module"] == "spending"


def test_explicit_modules_are_respected():
    """Explicit CSV module values must win over derivation."""
    payload, errors = _validate(
        _row(from_module="spending", to_module="investing"),
        account_types={"icici": "bank", "hdfc": "brokerage"},
    )
    assert errors == []
    assert payload["from_module"] == "spending"
    assert payload["to_module"] == "investing"


def test_template_row_matches_header_columns():
    """The downloadable template row must have one value per header column.

    TEMPLATE_HEADERS for finance_transfers has 14 columns; the example row
    previously had only 8, so users following the example produced module-less
    rows that all classified as spending->investing.
    """
    headers = TEMPLATE_HEADERS[ImportModule.finance_transfers]
    values = TEMPLATE_ROW.split(",")
    assert len(values) == len(headers), (
        f"template row has {len(values)} values for {len(headers)} headers"
    )
