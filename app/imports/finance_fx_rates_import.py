"""`ImportModule.finance_fx_rates` row validation and commit adapter."""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from app.finance.schemas import FxRateHistoryImportRequest, FxRateHistoryImportRow
from app.finance.service import FxRateService
from app.imports.models import ImportBatch, ImportPreviewRow
from app.imports.shared import AddErrorFn, norm


def validate_finance_fx_rate_row(
    row: dict,
    add_error: AddErrorFn,
    *,
    currency_set: set[str],
) -> tuple[dict, None]:
    base_raw = norm(row.get("base_currency_code"))
    quote_raw = norm(row.get("quote_currency_code"))
    rate_raw = norm(row.get("rate"))
    as_of_raw = norm(row.get("as_of_date"))

    # 1. Base currency validation
    base = None
    if base_raw:
        base = base_raw.upper()
        if base not in currency_set:
            add_error(
                "base_currency_code",
                "not_found",
                "base currency not enabled in workspace",
                base_raw,
            )
    else:
        add_error("base_currency_code", "required", "base_currency_code is required", base_raw)

    # 2. Quote currency validation
    quote = None
    if quote_raw:
        quote = quote_raw.upper()
        if quote not in currency_set:
            add_error(
                "quote_currency_code",
                "not_found",
                "quote currency not enabled in workspace",
                quote_raw,
            )
    else:
        add_error("quote_currency_code", "required", "quote_currency_code is required", quote_raw)

    # 3. Rate validation
    rate = None
    try:
        rate = Decimal(rate_raw)
        if rate <= 0:
            raise InvalidOperation
    except Exception:
        add_error("rate", "invalid_decimal", "rate must be a positive decimal", rate_raw)

    # 4. As of date validation
    as_of_date = None
    if as_of_raw:
        try:
            as_of_date = date.fromisoformat(as_of_raw)
        except Exception:
            try:
                as_of_date = datetime.fromisoformat(as_of_raw.replace("Z", "+00:00")).date()
            except Exception:
                add_error(
                    "as_of_date", "invalid_date", "as_of_date must be YYYY-MM-DD date", as_of_raw
                )

        if as_of_date and as_of_date > datetime.now(UTC).date():
            add_error("as_of_date", "future_date", "date must not be in the future", as_of_raw)
    else:
        add_error("as_of_date", "required", "as_of_date is required", as_of_raw)

    payload = {
        "base_currency_code": base,
        "quote_currency_code": quote,
        "rate": str(rate) if rate is not None else None,
        "as_of_date": as_of_date.isoformat() if as_of_date else None,
    }
    return payload, None


async def commit_finance_fx_rates_chunk(
    fx_rate_service: FxRateService,
    workspace_id: int,
    user_id: int,
    batch: ImportBatch,
    rows: list[ImportPreviewRow],
) -> tuple[int, dict]:
    rows_to_import = []
    for r in rows:
        p = r.payload_json
        rows_to_import.append(
            FxRateHistoryImportRow(
                base_currency_code=p["base_currency_code"],
                quote_currency_code=p["quote_currency_code"],
                rate=Decimal(p["rate"]),
                as_of_date=date.fromisoformat(p["as_of_date"]),
            )
        )

    req = FxRateHistoryImportRequest(rows=rows_to_import)
    result = await fx_rate_service.import_historical_rates(workspace_id, req)

    success_count = result.imported
    extra = {
        "imported": result.imported,
        "skipped": result.skipped,
        "rejected": [{"row": r.row, "reason": r.reason} for r in result.rejected],
    }
    return success_count, extra
