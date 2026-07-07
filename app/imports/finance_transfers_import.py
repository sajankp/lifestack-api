"""`ImportModule.finance_transfers` row validation and commit.

Committing a transfer whose `to_module` is `investing` also writes an
`investing_cash_balances` snapshot row, mirroring what
`finance.service.create_transfer` does for interactively-created transfers
(spec-049/050). The per-instance `cash_balance_cache` is owned by
`ImportService` (cleared on session change — see
`ImportService._ensure_cache_session`) and passed in by reference so multiple
transfers in the same batch/chunk accumulate correctly without N+1 queries.
"""

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.finance.models import CapitalTransfer, TransferModule
from app.imports.models import ImportBatch, ImportPreviewRow
from app.imports.shared import AddErrorFn, WeightEntry, norm
from app.investing.models import CashBalance as InvestingCashBalance
from app.investing.order_service import InvestingOrderService

TEMPLATE_ROW = "2026-05-01T09:30:00Z,ICICI,GROWW,INR,INR,50000.00,50000.00,SIP Investment"


def validate_finance_transfer_row(
    row: dict,
    add_error: AddErrorFn,
    *,
    order_account_pub_map: dict[str, uuid.UUID],
    account_map: dict[str, int],
    currency_set: set[str],
) -> tuple[dict, WeightEntry | None]:
    occurred_raw = norm(row.get("occurred_at"))
    from_account_raw = norm(row.get("from_account"))
    to_account_raw = norm(row.get("to_account"))
    from_currency_raw = norm(row.get("from_currency"))
    to_currency_raw = norm(row.get("to_currency"))
    gross_amount_raw = norm(row.get("gross_amount"))
    net_amount_raw = norm(row.get("net_amount_received"))
    notes_raw = norm(row.get("notes")) or None
    from_module_raw = norm(row.get("from_module")) or "spending"
    to_module_raw = norm(row.get("to_module")) or "investing"
    fx_rate_raw = norm(row.get("fx_rate_used")) or None
    fx_fee_raw = norm(row.get("fx_fee_amount")) or "0"
    platform_fee_raw = norm(row.get("platform_fee_amount")) or "0"
    tax_raw = norm(row.get("tax_amount")) or "0"

    occurred_at = None
    try:
        occurred_at = datetime.fromisoformat(occurred_raw.replace("Z", "+00:00"))
    except Exception:
        add_error(
            "occurred_at",
            "invalid_datetime",
            "occurred_at must be ISO datetime",
            occurred_raw,
        )

    from_account_pub_id = None
    from_account_id = None
    if from_account_raw:
        from_account_pub_id = order_account_pub_map.get(from_account_raw.lower())
        from_account_id = account_map.get(from_account_raw.lower())
        if from_account_pub_id is None:
            add_error(
                "from_account",
                "not_found",
                "from_account not found in workspace",
                from_account_raw,
            )
    else:
        add_error("from_account", "required", "from_account is required", from_account_raw)

    to_account_pub_id = None
    to_account_id = None
    if to_account_raw:
        to_account_pub_id = order_account_pub_map.get(to_account_raw.lower())
        to_account_id = account_map.get(to_account_raw.lower())
        if to_account_pub_id is None:
            add_error(
                "to_account",
                "not_found",
                "to_account not found in workspace",
                to_account_raw,
            )
    else:
        add_error("to_account", "required", "to_account is required", to_account_raw)

    from_currency = None
    if from_currency_raw:
        from_currency = from_currency_raw.upper()
        if from_currency not in currency_set:
            add_error(
                "from_currency",
                "not_found",
                "from_currency not enabled in workspace",
                from_currency_raw,
            )
    else:
        add_error(
            "from_currency",
            "required",
            "from_currency is required",
            from_currency_raw,
        )

    to_currency = None
    if to_currency_raw:
        to_currency = to_currency_raw.upper()
        if to_currency not in currency_set:
            add_error(
                "to_currency",
                "not_found",
                "to_currency not enabled in workspace",
                to_currency_raw,
            )
    else:
        add_error("to_currency", "required", "to_currency is required", to_currency_raw)

    try:
        gross_amount = Decimal(gross_amount_raw)
        if gross_amount < 0:
            raise InvalidOperation
    except Exception:
        add_error(
            "gross_amount",
            "invalid_decimal",
            "gross_amount must be a non-negative decimal",
            gross_amount_raw,
        )
        gross_amount = None

    try:
        net_amount_received = Decimal(net_amount_raw)
        if net_amount_received < 0:
            raise InvalidOperation
    except Exception:
        add_error(
            "net_amount_received",
            "invalid_decimal",
            "net_amount_received must be a non-negative decimal",
            net_amount_raw,
        )
        net_amount_received = None

    fx_rate_used = None
    if fx_rate_raw:
        try:
            fx_rate_used = Decimal(fx_rate_raw)
            if fx_rate_used <= 0:
                raise InvalidOperation
        except Exception:
            add_error(
                "fx_rate_used",
                "invalid_decimal",
                "fx_rate_used must be a positive decimal",
                fx_rate_raw,
            )

    def _safe_decimal(raw: str, field: str) -> Decimal:
        try:
            val = Decimal(raw)
            if val < 0:
                raise InvalidOperation
            return val
        except Exception:
            add_error(
                field,
                "invalid_decimal",
                f"{field} must be a non-negative decimal",
                raw,
            )
            return Decimal("0")

    fx_fee_amount = _safe_decimal(fx_fee_raw, "fx_fee_amount")
    platform_fee_amount = _safe_decimal(platform_fee_raw, "platform_fee_amount")
    tax_amount = _safe_decimal(tax_raw, "tax_amount")

    if from_module_raw not in {"spending", "investing"}:
        add_error(
            "from_module",
            "invalid_enum",
            "from_module must be spending or investing",
            from_module_raw,
        )
    if to_module_raw not in {"spending", "investing"}:
        add_error(
            "to_module",
            "invalid_enum",
            "to_module must be spending or investing",
            to_module_raw,
        )

    if gross_amount is not None and net_amount_received is not None:
        if (
            from_currency is not None
            and to_currency is not None
            and from_currency == to_currency
            and fx_rate_used is not None
            and fx_rate_used != Decimal("1")
        ):
            add_error(
                "fx_rate_used",
                "invalid_value",
                "FX rate must be 1.0 when transferring between the same currency",
                str(fx_rate_used),
            )

        gross = gross_amount
        fx_rate = fx_rate_used if fx_rate_used is not None else Decimal("1")
        converted_gross = gross * fx_rate
        total_fees = fx_fee_amount + platform_fee_amount + tax_amount
        net = net_amount_received
        difference = abs(converted_gross - total_fees - net)
        if difference > Decimal("0.01"):
            add_error(
                "net_amount_received",
                "invalid_value",
                f"Transfer arithmetic inconsistent: gross ({gross:.2f}) * rate ({fx_rate:.4f}) - fees ({total_fees:.2f}) ≠ net ({net:.2f}). Difference: {difference:.4f}",
                str(net_amount_received),
            )

    payload = {
        "occurred_at": occurred_at.isoformat() if occurred_at else None,
        "from_account": from_account_raw,
        "from_account_public_id": str(from_account_pub_id) if from_account_pub_id else None,
        "from_account_id": from_account_id,
        "to_account": to_account_raw,
        "to_account_public_id": str(to_account_pub_id) if to_account_pub_id else None,
        "to_account_id": to_account_id,
        "from_currency": from_currency,
        "to_currency": to_currency,
        "gross_amount": str(gross_amount) if gross_amount is not None else None,
        "net_amount_received": str(net_amount_received)
        if net_amount_received is not None
        else None,
        "notes": notes_raw,
        "from_module": from_module_raw,
        "to_module": to_module_raw,
        "fx_rate_used": str(fx_rate_used) if fx_rate_used is not None else None,
        "fx_fee_amount": str(fx_fee_amount),
        "platform_fee_amount": str(platform_fee_amount),
        "tax_amount": str(tax_amount),
    }
    return payload, None


async def commit_finance_transfers_chunk(
    session: AsyncSession,
    order_service: InvestingOrderService | None,
    workspace_id: int,
    user_id: int,
    batch: ImportBatch,
    rows: list[ImportPreviewRow],
    cash_balance_cache: dict[tuple[int, str], Decimal],
) -> int:
    inserted = 0
    for row in rows:
        p = row.payload_json
        from_account_id = p.get("from_account_id")
        to_account_id = p.get("to_account_id")
        if from_account_id is None or to_account_id is None:
            raise ValidationError(detail="from_account or to_account not found in workspace")

        transfer = CapitalTransfer(
            workspace_id=workspace_id,
            actor_id=user_id,
            from_module=TransferModule(p["from_module"]),
            to_module=TransferModule(p["to_module"]),
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            from_currency_code=p["from_currency"],
            to_currency_code=p["to_currency"],
            gross_amount=Decimal(p["gross_amount"]),
            fx_rate_used=Decimal(p.get("fx_rate_used")) if p.get("fx_rate_used") else None,
            fx_fee_amount=Decimal(p.get("fx_fee_amount") or "0"),
            platform_fee_amount=Decimal(p.get("platform_fee_amount") or "0"),
            tax_amount=Decimal(p.get("tax_amount") or "0"),
            net_amount_received=Decimal(p["net_amount_received"]),
            occurred_at=datetime.fromisoformat(p["occurred_at"]),
            notes=p.get("notes"),
            source_type="imported",
            source_import_id=batch.id,
            source_ref=f"{batch.public_id}:{row.row_number}",
        )
        session.add(transfer)
        inserted += 1

        # Mirror what finance.service.create_transfer does: update
        # investing cash balance when money flows into an investing account.
        # Use an in-memory cache so multiple transfers in the same batch
        # accumulate correctly without N+1 DB queries.
        if transfer.to_module == TransferModule.investing and order_service is not None:
            # public_id is a uuid.uuid4 default_factory — already populated on
            # Python instantiation, no flush needed before reading it.
            cache_key = (to_account_id, transfer.to_currency_code)
            if cache_key not in cash_balance_cache:
                cash_repo = order_service.cash_balance_repository
                latest = await cash_repo.get_latest_for_account_currency(
                    workspace_id, to_account_id, transfer.to_currency_code
                )
                cash_balance_cache[cache_key] = (
                    latest.balance if latest is not None else Decimal("0")
                )
            new_balance = cash_balance_cache[cache_key] + transfer.net_amount_received
            cash_balance_cache[cache_key] = new_balance
            new_cash = InvestingCashBalance(
                workspace_id=workspace_id,
                user_id=user_id,
                account_id=to_account_id,
                balance=new_balance,
                currency=transfer.to_currency_code,
                as_of=transfer.occurred_at,
                source_type="imported",
                source_import_id=batch.id,
                trigger_type="transfer",
                trigger_ref=transfer.public_id,
            )
            session.add(new_cash)
    return inserted
