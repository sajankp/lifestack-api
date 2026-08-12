"""`ImportModule.investing_orders` row validation and commit.

The commit side is also used for `ImportModule.investing_cams_cas` — a CAMS
CAS batch's preview rows are shaped identically to a CSV orders batch (see
`app/imports/cams_cas_import.py`), so both modules feed the same
`InvestingOrderCreate` pipeline via `commit_investing_orders_chunk`.
"""

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from app.core.audit import AuditLogger
from app.core.exceptions import ValidationError
from app.imports.models import ImportBatch, ImportPreviewRow
from app.imports.repository import ImportRepository
from app.imports.shared import AddErrorFn, WeightEntry, norm
from app.investing.models import InstrumentType
from app.investing.order_service import InvestingOrderService
from app.investing.schemas import InvestingOrderCreate

TEMPLATE_ROWS = (
    "buy,AAPL,stock,,Primary Brokerage,10,150.00,USD,1.99,0,0,2026-01-15T10:30:00+00:00,NASDAQ,First purchase",
    "buy,122639,mutual_fund,Parag Parikh Flexi Cap Fund Direct Growth,GROWW,222.03,90.07,INR,0,0,0,2026-04-09T00:00:00+00:00,,Parag Parikh Flexi Cap Fund Direct Growth | Amount: 19999",
)


def validate_investing_order_row(
    row: dict,
    add_error: AddErrorFn,
    *,
    order_account_pub_map: dict[str, uuid.UUID],
    currency_set: set[str],
) -> tuple[dict, WeightEntry | None]:
    order_type_raw = norm(row.get("order_type"))
    symbol_raw = norm(row.get("symbol"))
    instrument_type_raw = norm(row.get("instrument_type")) or "stock"
    instrument_name_raw = norm(row.get("instrument_name")) or None
    account_name_raw = norm(row.get("account_name"))
    quantity_raw = norm(row.get("quantity"))
    price_raw = norm(row.get("price_per_unit"))
    currency_raw = norm(row.get("currency"))
    occurred_raw = norm(row.get("occurred_at"))
    fee_raw = norm(row.get("brokerage_fee")) or "0"
    tax_raw = norm(row.get("tax_amount")) or "0"
    other_raw = norm(row.get("other_fees")) or "0"
    exchange_raw = norm(row.get("exchange_name")) or None
    notes_raw = norm(row.get("notes")) or None

    order_type_val = order_type_raw.lower() if order_type_raw else ""
    if order_type_val not in {"buy", "sell"}:
        add_error(
            "order_type",
            "invalid_enum",
            "order_type must be 'buy' or 'sell'",
            order_type_raw,
        )

    valid_instrument_types = {t.value for t in InstrumentType}
    instrument_type_val = instrument_type_raw.lower()
    if instrument_type_val not in valid_instrument_types:
        add_error(
            "instrument_type",
            "invalid_enum",
            f"instrument_type must be one of: {', '.join(sorted(valid_instrument_types))}",
            instrument_type_raw,
        )
        instrument_type_val = "stock"

    if instrument_type_val == InstrumentType.mutual_fund and not instrument_name_raw:
        add_error(
            "instrument_name",
            "required",
            "instrument_name is required when instrument_type is mutual_fund",
            instrument_name_raw,
        )

    if not symbol_raw:
        add_error("symbol", "required", "symbol is required", symbol_raw)

    account_public_id = None
    if account_name_raw:
        account_public_id = order_account_pub_map.get(account_name_raw.lower())
        if account_public_id is None:
            add_error(
                "account_name",
                "not_found",
                "account not found in workspace",
                account_name_raw,
            )
    else:
        add_error("account_name", "required", "account_name is required", account_name_raw)

    try:
        quantity = Decimal(quantity_raw)
        if quantity <= 0:
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError):
        add_error(
            "quantity",
            "invalid_decimal",
            "quantity must be a positive decimal",
            quantity_raw,
        )
        quantity = None

    try:
        price = Decimal(price_raw)
        if price <= 0:
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError):
        add_error(
            "price_per_unit",
            "invalid_decimal",
            "price_per_unit must be a positive decimal",
            price_raw,
        )
        price = None

    currency = None
    if currency_raw:
        currency = currency_raw.upper()
        if currency not in currency_set:
            add_error(
                "currency",
                "not_found",
                "currency not enabled in workspace",
                currency_raw,
            )
    else:
        add_error("currency", "required", "currency is required", currency_raw)

    occurred_at = None
    try:
        occurred_at = datetime.fromisoformat(occurred_raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        add_error(
            "occurred_at",
            "invalid_datetime",
            "occurred_at must be ISO datetime",
            occurred_raw,
        )

    def _safe_decimal(raw: str, field: str) -> Decimal:
        try:
            val = Decimal(raw)
            if val < 0:
                raise InvalidOperation
            return val
        except (InvalidOperation, TypeError, ValueError):
            add_error(
                field,
                "invalid_decimal",
                f"{field} must be a non-negative decimal",
                raw,
            )
            return Decimal("0")

    fee = _safe_decimal(fee_raw, "brokerage_fee")
    tax = _safe_decimal(tax_raw, "tax_amount")
    other = _safe_decimal(other_raw, "other_fees")

    payload = {
        "order_type": order_type_val,
        "symbol": symbol_raw.upper() if symbol_raw else None,
        "instrument_type": instrument_type_val,
        "instrument_name": instrument_name_raw,
        "account_name": account_name_raw,
        "account_public_id": str(account_public_id) if account_public_id else None,
        "quantity": str(quantity) if quantity is not None else None,
        "price_per_unit": str(price) if price is not None else None,
        "currency": currency,
        "brokerage_fee": str(fee),
        "tax_amount": str(tax),
        "other_fees": str(other),
        "occurred_at": occurred_at.isoformat() if occurred_at else None,
        "exchange_name": exchange_raw,
        "notes": notes_raw,
    }
    return payload, None


async def commit_investing_orders_chunk(
    order_service: InvestingOrderService,
    workspace_id: int,
    user_id: int,
    batch: ImportBatch,
    rows: list[ImportPreviewRow],
    audit_logger: AuditLogger,
) -> int:
    """Commit one chunk of `investing-orders` or `investing-cams-cas` preview
    rows via `InvestingOrderService.bulk_import_orders`. Returns the number of
    orders created in this chunk.
    """
    order_ins: list[InvestingOrderCreate] = []
    for row in rows:
        p = row.payload_json
        required = (
            "order_type",
            "symbol",
            "account_public_id",
            "quantity",
            "price_per_unit",
            "currency",
            "occurred_at",
        )
        if any(p.get(f) is None for f in required):
            raise ValidationError(
                detail=f"Row {row.row_number}: missing required order field in preview payload"
            )
        order_ins.append(
            InvestingOrderCreate(
                account_id=uuid.UUID(p["account_public_id"]),
                order_type=p["order_type"],
                symbol=p["symbol"],
                instrument_type=InstrumentType(p.get("instrument_type") or "stock"),
                instrument_name=p.get("instrument_name") or None,
                quantity=Decimal(p["quantity"]),
                price_per_unit=Decimal(p["price_per_unit"]),
                currency=p["currency"],
                brokerage_fee=Decimal(p.get("brokerage_fee") or "0"),
                tax_amount=Decimal(p.get("tax_amount") or "0"),
                other_fees=Decimal(p.get("other_fees") or "0"),
                exchange_name=p.get("exchange_name"),
                occurred_at=datetime.fromisoformat(p["occurred_at"]),
                notes=p.get("notes"),
            )
        )
    created = await order_service.bulk_import_orders(
        workspace_id=workspace_id,
        user_id=user_id,
        orders=order_ins,
        source_import_id=batch.id,
        audit_logger=audit_logger,
    )
    return len(created)


async def rollback_investing_orders_import(
    repository: ImportRepository,
    order_service: InvestingOrderService | None,
    workspace_id: int,
    user_id: int,
    import_batch_id: int,
) -> int:
    """Roll back a committed investing-orders (or CAMS CAS) import.

    Placing orders has side effects (cash-balance snapshots and holding
    avg_cost/quantity changes), so deleting the order rows alone would orphan
    cash balances and leave holdings incorrect. This removes the order-triggered
    cash balances and recomputes the affected holdings from the remaining orders.
    """
    if order_service is None:
        raise ValidationError(detail="Order service is required to roll back an order import")

    orders = await repository.list_investing_orders_for_batch(workspace_id, import_batch_id)
    if not orders:
        return 0

    affected = {(o.symbol, o.account_id) for o in orders}
    await repository.delete_cash_balances_by_trigger_refs(
        workspace_id, "order", [o.public_id for o in orders]
    )
    deleted_records = await repository.delete_investing_orders_for_batch(
        workspace_id, import_batch_id
    )
    for symbol, account_id in affected:
        await order_service._recompute_holding(workspace_id, user_id, symbol, account_id)
    return deleted_records
