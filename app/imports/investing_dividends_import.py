"""`ImportModule.investing_dividends` row validation and commit adapter."""

import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.core.audit import AuditLogger
from app.imports.models import ImportBatch, ImportPreviewRow
from app.imports.shared import AddErrorFn, norm
from app.investing.schemas import DividendBulkImportRequest, DividendBulkImportRow
from app.investing.service import DividendService


def validate_investing_dividend_row(
    row: dict,
    add_error: AddErrorFn,
    *,
    account_obj_map: dict[str, object],
    currency_set: set[str],
) -> tuple[dict, None]:
    account_raw = norm(row.get("account"))
    symbol_raw = norm(row.get("symbol"))
    income_type_raw = norm(row.get("income_type")) or "dividend"
    gross_raw = norm(row.get("gross"))
    tax_raw = norm(row.get("tax")) or "0"
    currency_raw = norm(row.get("currency"))
    pay_date_raw = norm(row.get("pay_date"))
    external_ref_raw = norm(row.get("external_ref"))
    notes_raw = norm(row.get("notes"))

    # 1. Account validation
    account_public_id = None
    account_currency = None
    if account_raw:
        acc = account_obj_map.get(account_raw.lower())
        if acc is None:
            add_error("account", "not_found", "account not found in workspace", account_raw)
        else:
            if getattr(acc, "account_type", None) != "brokerage":
                add_error(
                    "account",
                    "invalid_type",
                    f"Dividends/income can only be recorded on brokerage accounts; '{getattr(acc, 'name', '')}' is a {getattr(acc, 'account_type', '')} account.",
                    account_raw,
                )
            account_public_id = getattr(acc, "public_id", None)
            account_currency = getattr(acc, "default_currency_code", None)
    else:
        add_error("account", "required", "account is required", account_raw)

    # 2. Income type validation
    income_type_val = income_type_raw.lower() if income_type_raw else ""
    if income_type_val not in {"dividend", "interest", "coupon"}:
        add_error(
            "income_type",
            "invalid_enum",
            "income_type must be 'dividend', 'interest', or 'coupon'",
            income_type_raw,
        )
        income_type_val = "dividend"

    # 3. Symbol validation based on type
    symbol_val = symbol_raw.upper() if symbol_raw else None
    if income_type_val == "interest":
        if symbol_val is not None:
            add_error(
                "symbol",
                "symbol_not_allowed_for_interest",
                "symbol must be empty/null for interest (account-level income)",
                symbol_raw,
            )
    else:  # dividend or coupon
        if not symbol_val:
            add_error(
                "symbol",
                "required",
                f"symbol is required when income_type is {income_type_val}",
                symbol_raw,
            )

    # 4. Gross and Tax validations
    gross = None
    try:
        gross = Decimal(gross_raw)
        if gross <= 0:
            raise InvalidOperation
    except Exception:
        add_error("gross", "invalid_decimal", "gross must be a positive decimal", gross_raw)

    tax = None
    try:
        tax = Decimal(tax_raw)
        if tax < 0:
            raise InvalidOperation
    except Exception:
        add_error("tax", "invalid_decimal", "tax must be a non-negative decimal", tax_raw)

    if gross is not None and tax is not None and tax >= gross:
        add_error("tax", "tax_ge_gross", "tax_withheld must be less than gross_amount", tax_raw)

    # 5. Currency validations
    currency = None
    if currency_raw:
        currency = currency_raw.upper()
        if currency not in currency_set:
            add_error("currency", "not_found", "currency not enabled in workspace", currency_raw)
        elif account_currency and currency != account_currency.upper():
            add_error(
                "currency",
                "currency_mismatch",
                f"Currency '{currency}' does not match account '{account_raw}' ({account_currency})",
                currency_raw,
            )
    else:
        add_error("currency", "required", "currency is required", currency_raw)

    # 6. Date validation
    pay_date = None
    if pay_date_raw:
        try:
            pay_date = date.fromisoformat(pay_date_raw)
        except Exception:
            try:
                pay_date = datetime.fromisoformat(pay_date_raw.replace("Z", "+00:00")).date()
            except Exception:
                add_error(
                    "pay_date", "invalid_date", "pay_date must be YYYY-MM-DD date", pay_date_raw
                )
    else:
        add_error("pay_date", "required", "pay_date is required", pay_date_raw)

    payload = {
        "account_name": account_raw,
        "account_public_id": str(account_public_id) if account_public_id else None,
        "symbol": symbol_val,
        "income_type": income_type_val,
        "gross_amount": str(gross) if gross is not None else None,
        "tax_withheld": str(tax) if tax is not None else "0",
        "currency": currency,
        "pay_date": pay_date.isoformat() if pay_date else None,
        "external_ref": external_ref_raw,
        "notes": notes_raw,
    }
    return payload, None


async def commit_investing_dividends_chunk(
    dividend_service: DividendService,
    workspace_id: int,
    user_id: int,
    batch: ImportBatch,
    rows: list[ImportPreviewRow],
    audit_logger: AuditLogger,
) -> tuple[int, dict]:
    rows_to_import = []
    for r in rows:
        p = r.payload_json
        rows_to_import.append(
            DividendBulkImportRow(
                account_id=uuid.UUID(p["account_public_id"]),
                symbol=p["symbol"],
                income_type=p["income_type"],
                gross_amount=Decimal(p["gross_amount"]),
                tax_withheld=Decimal(p["tax_withheld"]),
                currency=p["currency"],
                pay_date=date.fromisoformat(p["pay_date"]),
                external_ref=p["external_ref"],
                notes=p["notes"],
            )
        )

    req = DividendBulkImportRequest(rows=rows_to_import)
    result = await dividend_service.bulk_import(workspace_id, user_id, req, audit_logger)

    success_count = result.imported + result.updated
    extra = {
        "imported": result.imported,
        "updated": result.updated,
        "skipped": result.skipped,
        "rejected": [{"row": r.row, "reason": r.reason} for r in result.rejected],
    }
    return success_count, extra
