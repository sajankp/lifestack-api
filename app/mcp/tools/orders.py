"""Investment orders and dividends."""

import decimal
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastmcp import FastMCP
from pydantic import ValidationError as PydanticValidationError

from app.auth.models import User
from app.capture.spending_batch import local_day_range_utc
from app.capture.tools import _safe_timezone
from app.core.audit import AuditLogger
from app.core.database import postgres
from app.core.exceptions import APIError
from app.finance.repository import AccountRepository
from app.investing.repository import (
    InstrumentRepository,
    InvestingOrderRepository,
    PortfolioSnapshotRepository,
)
from app.investing.schemas import (
    DividendCreate,
    DividendResponse,
    InstrumentType,
    InvestingOrderCreate,
    InvestingOrderUpdate,
    OrderType,
)
from app.mcp import security
from app.mcp.support import services


async def _possible_duplicate_orders(
    session: Any, workspace_id: int, user_id: int, payload: InvestingOrderCreate
) -> list[Any]:
    """Orders that look like ``payload``: same account + symbol + side + qty + price
    on the same workspace-local calendar day. Used for the ``possible_duplicate``
    warning (orders without a ``source_ref``); never to skip a write silently.
    """
    account = await AccountRepository(session).get_by_public_id(workspace_id, payload.account_id)
    if account is None or account.id is None:
        return []
    user = await session.get(User, user_id)
    tz = _safe_timezone(user.timezone if user and user.timezone else "UTC")
    occurred_at = payload.occurred_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    local_day = occurred_at.astimezone(tz).date()
    day_start, day_end = local_day_range_utc(local_day, local_day, tz)
    return list(
        await InvestingOrderRepository(session).find_possible_duplicate_orders(
            workspace_id,
            account_id=account.id,
            symbol=payload.symbol,
            order_type=payload.order_type.value,
            quantity=payload.quantity,
            price_per_unit=payload.price_per_unit,
            from_date=day_start,
            to_date=day_end,
        )
    )


async def _source_ref_replay_mismatches(
    session: Any, workspace_id: int, existing: Any, payload: InvestingOrderCreate
) -> list[str]:
    """Fields on which a ``source_ref`` replay differs from the order it matches.

    A retry must be byte-for-byte the same order. If the caller reuses a
    ``source_ref`` for a *different* order, reporting success would make it
    believe the new order exists while nothing was recorded.
    """
    account = await AccountRepository(session).get_by_id(workspace_id, existing.account_id)
    occurred_at = payload.occurred_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    comparisons = {
        "account_id": account is not None and account.public_id == payload.account_id,
        "order_type": existing.order_type == payload.order_type.value,
        "symbol": existing.symbol == payload.symbol,
        "quantity": existing.quantity == payload.quantity,
        "price_per_unit": existing.price_per_unit == payload.price_per_unit,
        "currency": existing.currency == payload.currency,
        "occurred_at": existing.occurred_at == occurred_at,
    }
    return [name for name, same in comparisons.items() if not same]


def _source_ref_conflict_message(source_ref: str, fields: list[str]) -> str:
    return (
        f"source_ref '{source_ref}' already belongs to a different order "
        f"(differs in: {', '.join(fields)}). Nothing was recorded. Use a new "
        "source_ref for a new order, or resend the original details to retry it."
    )


def _order_preview_fields(payload: InvestingOrderCreate, gross: Decimal, net: Decimal) -> dict:
    return {
        "entity_type": "investing_order",
        "account_id": str(payload.account_id),
        "order_type": payload.order_type.value,
        "symbol": payload.symbol,
        "quantity": str(payload.quantity),
        "price_per_unit": str(payload.price_per_unit),
        "gross_amount": str(gross),
        "brokerage_fee": str(payload.brokerage_fee),
        "tax_amount": str(payload.tax_amount),
        "other_fees": str(payload.other_fees),
        "net_amount": str(net),
        "currency": payload.currency,
        "occurred_at": payload.occurred_at.isoformat(),
        "source_ref": payload.source_ref,
    }


def _order_response(order: Any, account: Any, instrument: Any | None = None) -> dict[str, Any]:
    return {
        "public_id": str(order.public_id),
        "account_id": str(account.public_id),
        "account_name": account.name,
        "order_type": str(order.order_type),
        "symbol": order.symbol,
        "instrument_type": instrument.instrument_type if instrument else None,
        "quantity": str(order.quantity),
        "price_per_unit": str(order.price_per_unit),
        "gross_amount": str(order.gross_amount),
        "brokerage_fee": str(order.brokerage_fee),
        "tax_amount": str(order.tax_amount),
        "other_fees": str(order.other_fees),
        "net_amount": str(order.net_amount),
        "currency": order.currency,
        "exchange_name": order.exchange_name,
        "occurred_at": order.occurred_at.isoformat(),
        "notes": order.notes,
        "realized_gain_loss": str(order.realized_gain_loss)
        if order.realized_gain_loss is not None
        else None,
        "avg_cost_at_sale": str(order.avg_cost_at_sale)
        if order.avg_cost_at_sale is not None
        else None,
        "source_type": order.source_type,
        "source_ref": order.source_ref,
        "created_at": order.created_at.isoformat(),
    }


def _dividend_response(dividend: Any, account: Any) -> dict[str, Any]:
    return DividendResponse.model_validate({
        "public_id": dividend.public_id,
        "account_id": account.public_id,
        "account_name": account.name,
        "holding_id": None,
        "symbol": dividend.symbol,
        "income_type": dividend.income_type,
        "gross_amount": dividend.gross_amount,
        "tax_withheld": dividend.tax_withheld,
        "net_amount": dividend.net_amount,
        "currency": dividend.currency,
        "pay_date": dividend.pay_date,
        "external_ref": dividend.external_ref,
        "notes": dividend.notes,
        "created_at": dividend.created_at,
        "updated_at": dividend.updated_at,
    }).model_dump(mode="json")


def register(mcp: FastMCP) -> None:
    """Register the orders tools on ``mcp``."""

    async def _preview_order(
        workspace_id: int,
        payload: InvestingOrderCreate,
        gross: Decimal,
        net: Decimal,
        allow_duplicate: bool = False,
    ) -> dict[str, Any]:
        """``create_investment_order`` dry run: validate + dedupe, persist nothing."""
        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="create_investment_order"
            )
            status, match_reason, existing_id = "would_create", None, None
            errors: list[str] = []
            order_repo = InvestingOrderRepository(session)
            if payload.source_ref:
                existing = await order_repo.get_by_source_ref(workspace_id, payload.source_ref)
                if existing is not None:
                    status, match_reason, existing_id = (
                        "duplicate",
                        "source_ref",
                        str(existing.public_id),
                    )
                    mismatches = await _source_ref_replay_mismatches(
                        session, workspace_id, existing, payload
                    )
                    if mismatches:
                        status = "invalid"
                        errors = [_source_ref_conflict_message(payload.source_ref, mismatches)]
            elif not allow_duplicate:
                similar = await _possible_duplicate_orders(session, workspace_id, user_id, payload)
                if similar:
                    status, match_reason, existing_id = (
                        "possible_duplicate",
                        "possible_duplicate",
                        str(similar[0].public_id),
                    )
            if status == "would_create":
                # Run the real business validation (account, currency, cash, holdings)
                # inside a SAVEPOINT that is always rolled back -- nothing persists.
                nested = await session.begin_nested()
                try:
                    await services.order_service(session).place_order(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        order_in=payload,
                        audit_logger=AuditLogger(session),
                        source_type="mcp_agent",
                    )
                except (APIError, ValueError) as exc:
                    status = "invalid"
                    errors = [str(getattr(exc, "detail", None) or exc)]
                finally:
                    await nested.rollback()
            await session.rollback()
            counts = {"create": 0, "duplicate": 0, "possible_duplicate": 0, "invalid": 0}
            counts["create" if status == "would_create" else status] += 1
            return {
                "status": "success",
                "dry_run": True,
                "counts": counts,
                "items": [
                    {
                        "index": 0,
                        "status": status,
                        "match_reason": match_reason,
                        "existing_id": existing_id,
                        "errors": errors,
                        "warnings": [],
                    }
                ],
                **_order_preview_fields(payload, gross, net),
            }

    @mcp.tool
    async def list_investment_dividends(
        workspace_id: int,
        symbol: str | None = None,
        account_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List bounded dividend, interest, and coupon events."""
        security.validate_page(limit, offset)
        try:
            account_public_id = uuid.UUID(account_id) if account_id else None
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid account_id."}

        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="list_investment_dividends",
            )
            rows, total, accounts = await services.dividend_service(session).list_dividends(
                workspace_id,
                limit,
                offset,
                account_id=account_public_id,
                symbol=symbol,
            )
            items = [
                _dividend_response(row, accounts[row.account_id])
                for row in rows
                if row.account_id in accounts
            ]
            return {
                "status": "success",
                "workspace_id": workspace_id,
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    @mcp.tool
    async def list_investment_orders(
        workspace_id: int,
        account_id: str | None = None,
        symbol: str | None = None,
        order_type: str | None = None,
        search: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List recorded stock, ETF, and mutual-fund orders."""
        security.validate_page(limit, offset)
        if order_type is not None and order_type not in {"buy", "sell"}:
            return {"status": "error", "message": "order_type must be 'buy' or 'sell'."}
        try:
            account_public_id = uuid.UUID(account_id) if account_id else None
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid account_id."}

        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_investment_orders"
            )
            account_repo = AccountRepository(session)
            account_internal_id = None
            if account_public_id is not None:
                account = await account_repo.get_by_public_id(workspace_id, account_public_id)
                if account is None or account.id is None:
                    return {"status": "error", "message": "Investment account not found."}
                account_internal_id = account.id
            order_repo = InvestingOrderRepository(session)
            orders, total = await order_repo.list_by_workspace(
                workspace_id,
                limit,
                offset,
                symbol=symbol,
                account_id=account_internal_id,
                order_type=order_type,
                search=search,
            )
            accounts = await account_repo.get_by_ids(
                workspace_id, {order.account_id for order in orders}
            )
            instruments = await InstrumentRepository(session).get_by_ids([
                order.instrument_id for order in orders if order.instrument_id is not None
            ])
            items = [
                _order_response(
                    order, accounts[order.account_id], instruments.get(order.instrument_id)
                )
                for order in orders
                if order.account_id in accounts
            ]
            return {
                "status": "success",
                "workspace_id": workspace_id,
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    @mcp.tool
    async def create_investment_order(
        workspace_id: int,
        account_id: str,
        order_type: str,
        symbol: str,
        quantity: str,
        price_per_unit: str,
        currency: str,
        occurred_at: str,
        instrument_type: str = "stock",
        instrument_name: str | None = None,
        brokerage_fee: str = "0",
        tax_amount: str = "0",
        other_fees: str = "0",
        exchange_name: str | None = None,
        notes: str | None = None,
        source_ref: str | None = None,
        confirmed: bool = False,
        dry_run: bool = False,
        allow_duplicate: bool = False,
    ) -> dict[str, Any]:
        """Record a historical buy or sell order after explicit confirmation.

        This records the trade in Lifestack and updates holdings and brokerage
        cash; it does not submit a live order to a broker.

        Idempotency: a repeated ``source_ref`` returns the existing order and
        writes nothing. Without a ``source_ref``, an order with the same
        account + symbol + side + quantity + price on the same local day is
        reported as ``possible_duplicate`` (with the existing order's ID) and is
        NOT recorded -- fees and rounding differ too much to skip it silently;
        set ``allow_duplicate=true`` to record a genuine repeat.

        ``dry_run=true`` runs every validation (including cash and holding
        checks) and the duplicate checks, writes nothing, needs no confirmation,
        and returns ``{counts, items:[{index, status, match_reason, existing_id,
        errors}]}`` with the calculated amounts.
        """
        try:
            payload = InvestingOrderCreate(
                account_id=uuid.UUID(account_id),
                order_type=OrderType(order_type.strip().lower()),
                symbol=symbol,
                instrument_type=InstrumentType(instrument_type.strip().lower()),
                instrument_name=instrument_name,
                quantity=quantity,
                price_per_unit=price_per_unit,
                currency=currency,
                brokerage_fee=brokerage_fee,
                tax_amount=tax_amount,
                other_fees=other_fees,
                exchange_name=exchange_name,
                occurred_at=occurred_at,
                notes=notes,
                source_ref=source_ref,
            )
        except (TypeError, ValueError, PydanticValidationError) as exc:
            return {"status": "error", "message": f"Invalid investment order: {exc}"}

        gross_amount = payload.quantity * payload.price_per_unit
        fees = payload.brokerage_fee + payload.tax_amount + payload.other_fees
        net_amount = (
            gross_amount + fees if payload.order_type == OrderType.buy else gross_amount - fees
        )
        if net_amount <= 0:
            return {"status": "error", "message": "Fees must leave a positive net amount."}

        if dry_run:
            return await _preview_order(
                workspace_id, payload, gross_amount, net_amount, allow_duplicate
            )

        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    f"Record {payload.order_type.value} order for {payload.quantity} {payload.symbol} "
                    f"at {payload.price_per_unit} {payload.currency} per unit; net amount "
                    f"{net_amount} {payload.currency} on {payload.occurred_at.isoformat()}?"
                ),
                "account_id": str(payload.account_id),
                "order_type": payload.order_type.value,
                "symbol": payload.symbol,
                "instrument_type": payload.instrument_type.value,
                "quantity": str(payload.quantity),
                "price_per_unit": str(payload.price_per_unit),
                "gross_amount": str(gross_amount),
                "brokerage_fee": str(payload.brokerage_fee),
                "tax_amount": str(payload.tax_amount),
                "other_fees": str(payload.other_fees),
                "net_amount": str(net_amount),
                "currency": payload.currency,
                "occurred_at": payload.occurred_at.isoformat(),
                "source_ref": payload.source_ref,
            }

        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="create_investment_order"
            )
            order_repo = InvestingOrderRepository(session)
            # Check-then-insert below: hold a per-workspace lock until commit so a
            # concurrent identical request waits and then sees this order.
            await order_repo.lock_idempotent_writes(workspace_id)
            if payload.source_ref:
                existing = await order_repo.get_by_source_ref(workspace_id, payload.source_ref)
                if existing is not None:
                    mismatches = await _source_ref_replay_mismatches(
                        session, workspace_id, existing, payload
                    )
                    if mismatches:
                        return {
                            "status": "error",
                            "match_reason": "source_ref",
                            "existing_id": str(existing.public_id),
                            "message": _source_ref_conflict_message(payload.source_ref, mismatches),
                        }
                    account = await AccountRepository(session).get_by_id(
                        workspace_id, existing.account_id
                    )
                    return {
                        "status": "success",
                        "idempotent": True,
                        "match_reason": "source_ref",
                        "existing_id": str(existing.public_id),
                        "entity_type": "investing_order",
                        "entity_public_id": str(existing.public_id),
                        "item": _order_response(existing, account),
                    }
            elif not allow_duplicate:
                similar = await _possible_duplicate_orders(session, workspace_id, user_id, payload)
                if similar:
                    return {
                        "status": "possible_duplicate",
                        "match_reason": "possible_duplicate",
                        "existing_id": str(similar[0].public_id),
                        "possible_duplicate_count": len(similar),
                        "message": (
                            "An order with the same account, symbol, side, quantity and price "
                            "already exists on this day. Nothing was recorded. If this is a "
                            "genuine second order, retry with allow_duplicate=true; otherwise "
                            "pass a source_ref to make retries idempotent."
                        ),
                    }
            try:
                order = await services.order_service(session).place_order(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    order_in=payload,
                    audit_logger=AuditLogger(session),
                    source_type="mcp_agent",
                )
                await PortfolioSnapshotRepository(session).delete_for_date(
                    workspace_id, datetime.now(UTC).date()
                )
                account = await AccountRepository(session).get_by_id(workspace_id, order.account_id)
                instrument = (
                    await InstrumentRepository(session).get_by_ids([order.instrument_id])
                    if order.instrument_id is not None
                    else {}
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            item = _order_response(order, account, instrument.get(order.instrument_id))
            return {
                "status": "success",
                "entity_type": "investing_order",
                "entity_public_id": item["public_id"],
                "item": item,
                "summary": f"Recorded {payload.order_type.value} order for {payload.symbol}.",
            }

    @mcp.tool
    async def update_investment_order(
        workspace_id: int,
        public_id: str,
        order_type: str | None = None,
        quantity: str | None = None,
        price_per_unit: str | None = None,
        brokerage_fee: str | None = None,
        tax_amount: str | None = None,
        other_fees: str | None = None,
        exchange_name: str | None = None,
        occurred_at: str | None = None,
        notes: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Update a recorded investment order after explicit confirmation."""
        try:
            order_public_id = uuid.UUID(public_id)
            payload = InvestingOrderUpdate(
                order_type=OrderType(order_type.strip().lower()) if order_type else None,
                quantity=quantity,
                price_per_unit=price_per_unit,
                brokerage_fee=brokerage_fee,
                tax_amount=tax_amount,
                other_fees=other_fees,
                exchange_name=exchange_name,
                occurred_at=occurred_at,
                notes=notes,
            )
        except (TypeError, ValueError, PydanticValidationError) as exc:
            return {"status": "error", "message": f"Invalid investment order update: {exc}"}
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": f"Update investment order {public_id}? Confirm to continue.",
                "public_id": public_id,
                "changes": payload.model_dump(exclude_unset=True, mode="json"),
            }
        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="update_investment_order"
            )
            try:
                order = await services.order_service(session).update_order(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    order_public_id=order_public_id,
                    order_update=payload,
                    audit_logger=AuditLogger(session),
                )
                await PortfolioSnapshotRepository(session).delete_for_date(
                    workspace_id, datetime.now(UTC).date()
                )
                account = await AccountRepository(session).get_by_id(workspace_id, order.account_id)
                instrument = (
                    await InstrumentRepository(session).get_by_ids([order.instrument_id])
                    if order.instrument_id is not None
                    else {}
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            item = _order_response(order, account, instrument.get(order.instrument_id))
            return {
                "status": "success",
                "entity_type": "investing_order",
                "entity_public_id": item["public_id"],
                "item": item,
            }

    @mcp.tool
    async def delete_investment_order(
        workspace_id: int, public_id: str, confirmed: bool = False
    ) -> dict[str, Any]:
        """Delete a recorded investment order after explicit confirmation."""
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    f"Delete investment order {public_id}? This reverses its holding and "
                    "cash impact. Confirm to continue."
                ),
                "public_id": public_id,
            }
        try:
            order_public_id = uuid.UUID(public_id)
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid public_id."}
        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="delete_investment_order"
            )
            try:
                await services.order_service(session).delete_order(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    order_public_id=order_public_id,
                    audit_logger=AuditLogger(session),
                )
                await PortfolioSnapshotRepository(session).delete_for_date(
                    workspace_id, datetime.now(UTC).date()
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
        return {
            "status": "success",
            "entity_type": "investing_order",
            "entity_public_id": public_id,
            "summary": f"Deleted investment order {public_id}.",
        }

    @mcp.tool
    async def create_investment_dividend(
        workspace_id: int,
        account_id: str,
        gross_amount: str,
        currency: str,
        pay_date: str,
        symbol: str | None = None,
        income_type: str = "dividend",
        tax_withheld: str = "0",
        external_ref: str | None = None,
        notes: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Create a confirmed dividend or investment-income event.

        This credits the linked brokerage cash balance through the existing
        DividendService and never accepts bank or wallet accounts.
        """
        norm_type = income_type.strip().lower() if income_type else "dividend"
        if norm_type not in ("dividend", "interest", "coupon"):
            return {
                "status": "error",
                "message": "income_type must be one of 'dividend', 'interest', 'coupon'.",
            }

        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    f"Record {norm_type} income of {gross_amount} {currency} for "
                    f"{symbol or 'the brokerage account'} on {pay_date}? Confirm to continue."
                ),
                "account_id": account_id,
                "symbol": symbol,
                "income_type": norm_type,
                "gross_amount": gross_amount,
                "tax_withheld": tax_withheld,
                "currency": currency,
                "pay_date": pay_date,
            }

        try:
            payload = DividendCreate(
                account_id=uuid.UUID(account_id),
                symbol=symbol,
                income_type=norm_type,
                gross_amount=gross_amount,
                tax_withheld=tax_withheld,
                currency=currency,
                pay_date=pay_date,
                external_ref=external_ref,
                notes=notes,
            )
        except (TypeError, ValueError, PydanticValidationError) as exc:
            return {"status": "error", "message": f"Invalid dividend: {exc}"}

        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:write",
                tool="create_investment_dividend",
            )
            try:
                dividend, account = await services.dividend_service(session).create_dividend(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    dividend_in=payload,
                    audit_logger=AuditLogger(session),
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            item = _dividend_response(dividend, account)
            return {
                "status": "success",
                "entity_type": "investment_dividend",
                "entity_public_id": item["public_id"],
                "item": item,
                "summary": f"Recorded {dividend.income_type} income of {dividend.net_amount} {dividend.currency}.",
            }

    @mcp.tool
    async def delete_investment_dividend(
        workspace_id: int,
        public_id: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Delete a dividend/investment-income event by public ID.

        This removes the dividend record and its associated cash balance credit.
        Requires explicit confirmation.
        """
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": f"Delete dividend {public_id}? This will also remove its cash balance credit. Confirm to continue.",
                "public_id": public_id,
            }

        try:
            div_public_id = uuid.UUID(public_id)
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid public_id."}

        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:write",
                tool="delete_investment_dividend",
            )
            try:
                await services.dividend_service(session).delete_dividend(
                    workspace_id=workspace_id,
                    public_id=div_public_id,
                    actor_id=user_id,
                    audit_logger=AuditLogger(session),
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                "entity_type": "investment_dividend",
                "entity_public_id": public_id,
                "summary": f"Deleted dividend {public_id}.",
            }
