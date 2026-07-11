"""`ImportModule.finance_net_worth_history` row validation and commit adapter."""

from datetime import date, datetime
from decimal import Decimal

from app.finance.schemas import NetWorthHistoryImportRequest, NetWorthHistoryImportRow
from app.finance.service import NetWorthService
from app.imports.models import ImportBatch, ImportPreviewRow
from app.imports.shared import AddErrorFn, norm


def validate_finance_net_worth_history_row(
    row: dict,
    add_error: AddErrorFn,
    *,
    currency_set: set[str],
    earliest_live_nw_date: date,
) -> tuple[dict, None]:
    date_raw = norm(row.get("date"))
    currency_raw = norm(row.get("reporting_currency"))
    total_raw = norm(row.get("total_net_worth"))
    holdings_raw = norm(row.get("holdings_value"))
    investing_raw = norm(row.get("investing_cash"))
    spending_raw = norm(row.get("spending_cash"))

    # 1. Date validation
    as_of_date = None
    if date_raw:
        try:
            as_of_date = date.fromisoformat(date_raw)
        except Exception:
            try:
                as_of_date = datetime.fromisoformat(date_raw.replace("Z", "+00:00")).date()
            except Exception:
                add_error("date", "invalid_date", "date must be YYYY-MM-DD date", date_raw)

        if as_of_date and as_of_date >= earliest_live_nw_date:
            add_error(
                "date",
                "date_not_backfill",
                "date must be strictly before the earliest live net worth snapshot date",
                date_raw,
            )
    else:
        add_error("date", "required", "date is required", date_raw)

    # 2. Currency validation
    currency = None
    if currency_raw:
        currency = currency_raw.upper()
        if currency not in currency_set:
            add_error(
                "reporting_currency",
                "not_found",
                "reporting currency not enabled in workspace",
                currency_raw,
            )
    else:
        add_error("reporting_currency", "required", "reporting_currency is required", currency_raw)

    # 3. Total net worth validation
    total_net_worth = None
    try:
        total_net_worth = Decimal(total_raw)
    except Exception:
        add_error(
            "total_net_worth",
            "invalid_decimal",
            "total_net_worth must be a valid decimal",
            total_raw,
        )

    # 4. Components validation (all-or-none)
    holdings = None
    if holdings_raw:
        try:
            holdings = Decimal(holdings_raw)
        except Exception:
            add_error(
                "holdings_value",
                "invalid_decimal",
                "holdings_value must be a valid decimal",
                holdings_raw,
            )

    investing = None
    if investing_raw:
        try:
            investing = Decimal(investing_raw)
        except Exception:
            add_error(
                "investing_cash",
                "invalid_decimal",
                "investing_cash must be a valid decimal",
                investing_raw,
            )

    spending = None
    if spending_raw:
        try:
            spending = Decimal(spending_raw)
        except Exception:
            add_error(
                "spending_cash",
                "invalid_decimal",
                "spending_cash must be a valid decimal",
                spending_raw,
            )

    # norm() returns "" (not None) for omitted CSV cells, so filter on
    # truthiness — otherwise every row looks like it supplied all three
    # components and the all-or-none check below never fires.
    components = [holdings_raw, investing_raw, spending_raw]
    given_components = [c for c in components if c]
    if given_components and len(given_components) != 3:
        add_error(
            "total_net_worth",
            "components_mismatch",
            "holdings_value, investing_cash, and spending_cash must be all given or all omitted",
            None,
        )
    # All three components given: total must equal their sum.
    elif (
        len(given_components) == 3
        and total_net_worth is not None
        and holdings is not None
        and investing is not None
        and spending is not None
        and total_net_worth != (holdings + investing + spending)
    ):
        add_error(
            "total_net_worth",
            "total_mismatch",
            "total_net_worth does not equal the sum of holdings_value, investing_cash, and spending_cash",
            total_raw,
        )

    payload = {
        "date": as_of_date.isoformat() if as_of_date else None,
        "reporting_currency": currency,
        "total_net_worth": str(total_net_worth) if total_net_worth is not None else None,
        "holdings_value": str(holdings) if holdings is not None else None,
        "investing_cash": str(investing) if investing is not None else None,
        "spending_cash": str(spending) if spending is not None else None,
    }
    return payload, None


async def commit_finance_net_worth_history_chunk(
    net_worth_service: NetWorthService,
    workspace_id: int,
    user_id: int,
    batch: ImportBatch,
    rows: list[ImportPreviewRow],
) -> tuple[int, dict]:
    rows_to_import = []
    for r in rows:
        p = r.payload_json
        rows_to_import.append(
            NetWorthHistoryImportRow(
                date=date.fromisoformat(p["date"]),
                total_net_worth=Decimal(p["total_net_worth"]),
                holdings_value=Decimal(p["holdings_value"])
                if p.get("holdings_value") is not None
                else None,
                investing_cash=Decimal(p["investing_cash"])
                if p.get("investing_cash") is not None
                else None,
                spending_cash=Decimal(p["spending_cash"])
                if p.get("spending_cash") is not None
                else None,
                reporting_currency=p["reporting_currency"],
            )
        )

    req = NetWorthHistoryImportRequest(rows=rows_to_import)
    result = await net_worth_service.import_backfill_points(workspace_id, req)

    success_count = result.imported
    extra = {
        "imported": result.imported,
        "skipped": result.skipped,
        "rejected": [{"row": r.row, "reason": r.reason} for r in result.rejected],
    }
    return success_count, extra
