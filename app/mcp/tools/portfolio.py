"""Holdings, investing summary, performance, reconciliation and report history."""

import decimal
import json
import re
import uuid
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from fastmcp import FastMCP
from pydantic import ValidationError as PydanticValidationError

from app.core.currency import (
    build_required_pairs,
    conversion_rate,
    convert_amount,
    effective_display_as_of,
    fx_rates_used,
)
from app.core.audit import AuditLogger
from app.core.database import postgres
from app.finance.repository import (
    AccountRepository,
    CurrencyRepository,
    FinanceSettingRepository,
    FxRateRepository,
)
from app.investing.performance_report import VALID_PERIODS, build_performance_report, period_start
from app.investing.reconciliation import (
    ReconciliationInputError,
    parse_external_list,
    parse_tolerances,
)
from app.investing.repository import (
    CompanyRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InstrumentRepository,
    PortfolioSnapshotRepository,
)
from app.investing.tax.exchanges import resolve_exchange
from app.mcp import security
from app.mcp.support import common, services

_HOLDINGS_QUANTITY_STATES = {"all", "nonzero", "zero"}


_HOLDINGS_SORT_FIELDS = {
    "symbol",
    "quantity",
    "current_value",
    "book_value",
    "gain_loss",
    "gain_loss_pct",
    "created_at",
    "updated_at",
}


_HOLDINGS_VALUE_SORT_FIELDS = {
    "current_value",
    "book_value",
    "gain_loss",
    "gain_loss_pct",
}


_HOLDINGS_INSTRUMENT_TYPES = {"stock", "etf", "mutual_fund"}


MAX_PORTFOLIO_REPORT_CHARS = 2_000_000


MAX_LRS_HEADROOM_INR = Decimal("1e16")  # numeric(18,2) holds < 10^16


def _decimal_string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _add_holding_reporting_valuation(
    data: dict[str, Any],
    holding: Any,
    price_row: Any,
    reporting_currency: str | None,
    fx_lookup: dict[tuple[str, str], Any],
    fx_as_of: datetime | None,
    stale_days: int | None = None,
) -> None:
    """Add explicit reporting-currency values without changing REST fields."""
    quantity = Decimal(str(holding.quantity))
    avg_cost = Decimal(str(holding.avg_cost))
    native_currency = holding.currency.upper()
    book_value = quantity * avg_cost
    market_value = quantity * Decimal(str(price_row.unit_price)) if price_row else None

    data["price_available"] = price_row is not None
    data["price_as_of"] = price_row.price_date.isoformat() if price_row else None
    data["price_source"] = price_row.source if price_row else None
    data["reporting_currency"] = reporting_currency
    data["fx_as_of"] = fx_as_of.isoformat() if fx_as_of else None

    if quantity == 0:
        data.update({
            "reporting_current_value": "0",
            "reporting_book_value": "0",
            "reporting_gain_loss": "0",
            "reporting_gain_loss_pct": "0",
            "valuation_status": "zero_quantity",
        })
        return

    if reporting_currency is None:
        data.update({
            "reporting_current_value": None,
            "reporting_book_value": None,
            "reporting_gain_loss": None,
            "reporting_gain_loss_pct": None,
            "valuation_status": "reporting_currency_required",
        })
        return

    converted_book = convert_amount(book_value, native_currency, reporting_currency, fx_lookup)
    converted_market = (
        convert_amount(market_value, native_currency, reporting_currency, fx_lookup)
        if market_value is not None
        else None
    )
    if converted_book is None:
        status = "missing_fx"
    elif converted_market is None:
        status = "missing_price"
    else:
        status = "current"
        # A price is only "current" while it is within the workspace's
        # stale-price window; an old NAV or close must not read as fresh.
        if stale_days is not None:
            age = (datetime.now(UTC).date() - price_row.price_date).days
            if age > stale_days:
                status = "stale"

    converted_gain_loss = (
        converted_market - converted_book
        if converted_market is not None and converted_book is not None
        else None
    )
    gain_loss_pct = (
        converted_gain_loss / converted_book * Decimal("100")
        if converted_gain_loss is not None and converted_book
        else (Decimal("0") if converted_gain_loss == 0 else None)
    )
    data.update({
        "reporting_current_value": _decimal_string(converted_market),
        "reporting_book_value": _decimal_string(converted_book),
        "reporting_gain_loss": _decimal_string(converted_gain_loss),
        "reporting_gain_loss_pct": _decimal_string(gain_loss_pct),
        "valuation_status": status,
    })


def _sort_holding_items(
    items: list[dict[str, Any]],
    sort_by: str,
    sort_direction: str,
    use_reporting_values: bool,
) -> list[dict[str, Any]]:
    reporting_fields = {
        "current_value": "reporting_current_value",
        "book_value": "reporting_book_value",
        "gain_loss": "reporting_gain_loss",
        "gain_loss_pct": "reporting_gain_loss_pct",
    }
    field = reporting_fields.get(sort_by, sort_by) if use_reporting_values else sort_by

    def value(item: dict[str, Any]) -> Decimal | str | None:
        raw = item.get(field)
        if raw is None:
            return None
        if sort_by in _HOLDINGS_VALUE_SORT_FIELDS:
            return Decimal(str(raw))
        return str(raw)

    present = [item for item in items if value(item) is not None]
    missing = [item for item in items if value(item) is None]
    present.sort(key=lambda item: str(item["public_id"]))
    present.sort(key=lambda item: value(item), reverse=sort_direction == "desc")  # type: ignore[arg-type]
    missing.sort(key=lambda item: str(item["public_id"]))
    return present + missing


def register(mcp: FastMCP) -> None:
    """Register the portfolio tools on ``mcp``."""

    @mcp.tool
    async def list_investment_holdings(
        workspace_id: int,
        limit: int = 25,
        offset: int = 0,
        quantity_state: str = "nonzero",
        symbol: str | None = None,
        account_id: str | None = None,
        currency: str | None = None,
        instrument_type: str | None = None,
        sort_by: str = "current_value",
        sort_direction: str = "desc",
        valuation_currency: str | None = None,
    ) -> dict[str, Any]:
        """List investment holdings with filters, sorting, and reporting valuation.

        Native valuation fields remain in each holding's own currency. Reporting
        values use ``valuation_currency`` when supplied, otherwise the workspace
        reporting currency, or the sole holding currency when unambiguous.
        """
        security.validate_page(limit, offset)
        if quantity_state not in _HOLDINGS_QUANTITY_STATES:
            return {
                "status": "error",
                "message": "quantity_state must be one of: all, nonzero, zero.",
            }
        if sort_by not in _HOLDINGS_SORT_FIELDS:
            return {
                "status": "error",
                "message": (
                    "sort_by must be one of: symbol, quantity, current_value, book_value, "
                    "gain_loss, gain_loss_pct, created_at, updated_at."
                ),
            }
        if sort_direction not in {"asc", "desc"}:
            return {"status": "error", "message": "sort_direction must be asc or desc."}
        if instrument_type and instrument_type not in _HOLDINGS_INSTRUMENT_TYPES:
            return {
                "status": "error",
                "message": "instrument_type must be one of: stock, etf, mutual_fund.",
            }
        if currency:
            currency = currency.strip().upper()
        if symbol:
            symbol = symbol.strip().upper()
        if valuation_currency:
            valuation_currency = valuation_currency.strip().upper()
            if len(valuation_currency) > 10 or not valuation_currency:
                return {"status": "error", "message": "Invalid valuation_currency."}
        try:
            account_public_id = uuid.UUID(account_id) if account_id else None
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid account_id."}

        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="list_investment_holdings",
            )

            account_filter_id = None
            if account_public_id is not None:
                account = await AccountRepository(session).get_by_public_id(
                    workspace_id, account_public_id
                )
                if account is None or account.id is None:
                    return {"status": "error", "message": "Account not found in this workspace."}
                account_filter_id = account.id

            service = services.holding_service(session)
            requires_value_sort = sort_by in _HOLDINGS_VALUE_SORT_FIELDS
            query_limit = None if requires_value_sort else limit
            query_offset = 0 if requires_value_sort else offset
            query_sort_by = "symbol" if requires_value_sort else sort_by
            query_sort_direction = "asc" if requires_value_sort else sort_direction
            raw_holdings, total = await HoldingRepository(session).get_all(
                workspace_id,
                limit=query_limit,
                offset=query_offset,
                quantity_state=quantity_state,
                symbol=symbol,
                account_id=account_filter_id,
                currency=currency,
                instrument_type=instrument_type,
                sort_by=query_sort_by,
                sort_direction=query_sort_direction,
            )
            detailed_items, _ = await service.list_holdings_with_details(
                workspace_id,
                limit=query_limit,
                offset=query_offset,
                quantity_state=quantity_state,
                symbol=symbol,
                account_id=account_filter_id,
                currency=currency,
                instrument_type=instrument_type,
                sort_by=query_sort_by,
                sort_direction=query_sort_direction,
            )
            instruments = await InstrumentRepository(session).get_by_ids([
                holding.instrument_id
                for holding in raw_holdings
                if holding.instrument_id is not None
            ])
            companies = await CompanyRepository(session).get_by_ids([
                instrument.company_id
                for instrument in instruments.values()
                if instrument.company_id is not None
            ])
            raw_by_public_id = {holding.public_id: holding for holding in raw_holdings}
            price_rows = await HoldingPriceRepository(session).latest_prices_on_or_before_bulk(
                workspace_id,
                [holding.id for holding in raw_holdings if holding.id is not None],
                datetime.now(UTC).date(),
            )

            finance_settings = await FinanceSettingRepository(session).get_by_workspace(
                workspace_id
            )
            reporting_currency = valuation_currency or (
                finance_settings.reporting_currency_code.upper()
                if finance_settings and finance_settings.reporting_currency_code
                else None
            )
            stale_days = (
                finance_settings.stale_price_days
                if finance_settings and finance_settings.stale_price_days is not None
                else 7
            )
            if valuation_currency:
                currency_row = await CurrencyRepository(session).get_by_code(valuation_currency)
                if currency_row is None or not currency_row.is_active:
                    return {
                        "status": "error",
                        "message": f"Unsupported valuation_currency '{valuation_currency}'.",
                    }

            used_currencies = sorted({
                holding.currency.upper() for holding in raw_holdings if holding.quantity != 0
            })
            if reporting_currency is None and len(used_currencies) == 1:
                reporting_currency = used_currencies[0]
            fx_lookup: dict[tuple[str, str], Any] = {}
            fx_as_of = None
            if reporting_currency and any(
                currency_code != reporting_currency for currency_code in used_currencies
            ):
                fx_as_of = effective_display_as_of()
                fx_lookup = await FxRateRepository(session).get_latest_rates_for_pairs(
                    list(build_required_pairs(used_currencies, reporting_currency)),
                    as_of=fx_as_of,
                )

            items = []
            for item in detailed_items:
                data = item.model_dump(mode="json")
                raw = raw_by_public_id.get(item.public_id)
                instrument = instruments.get(raw.instrument_id) if raw else None
                data.update({
                    "instrument_public_id": str(instrument.public_id) if instrument else None,
                    "instrument_name": instrument.name if instrument else None,
                    "isin": instrument.isin if instrument else None,
                    "exchange": instrument.exchange if instrument else None,
                })
                if instrument is not None and instrument.company_id is not None:
                    company = companies.get(instrument.company_id)
                    if company is not None:
                        data["company_sector"] = company.sector
                        data["company_industry"] = company.industry
                if raw is not None:
                    _add_holding_reporting_valuation(
                        data,
                        raw,
                        price_rows.get(raw.id) if raw.id is not None else None,
                        reporting_currency,
                        fx_lookup,
                        fx_as_of,
                        stale_days=stale_days,
                    )
                items.append(data)

            if requires_value_sort:
                items = _sort_holding_items(
                    items,
                    sort_by,
                    sort_direction,
                    use_reporting_values=reporting_currency is not None,
                )
            else:
                # The repository already applied this ordering. Keep the
                # explicit slice here only for the value-sort path's shared
                # response construction.
                items = items[:limit]
            page_total = total
            if requires_value_sort:
                items = items[offset : offset + limit]
            return {
                "status": "success",
                "workspace_id": workspace_id,
                "items": items,
                "total": page_total,
                "limit": limit,
                "offset": offset,
                "quantity_state": quantity_state,
                "sort_by": sort_by,
                "sort_direction": sort_direction,
                "valuation_currency": reporting_currency,
                "fx_as_of": fx_as_of.isoformat() if fx_as_of else None,
                "fx_rates_used": (
                    fx_rates_used(used_currencies, reporting_currency, fx_lookup)
                    if reporting_currency
                    else {}
                ),
            }

    @mcp.tool
    async def reconcile_holdings(
        workspace_id: int,
        external_list: list[dict[str, Any]],
        account_id: str | None = None,
        quantity_tolerance: str = "0.0001",
        cost_tolerance_pct: str = "0.5",
    ) -> dict[str, Any]:
        """Diff an external holdings list (INDmoney/Paasa/broker) against LifeStack.

        ``external_list`` entries (max 500): ``symbol`` and/or ``isin``,
        ``quantity`` (required), optional ``avg_cost``, ``currency``,
        ``account_id`` (LifeStack account UUID), ``source``, ``external_ref``.
        Symbols are normalised (``NSE:RELIANCE``, ``RELIANCE.NS`` and
        ``RELIANCE-EQ`` match ``RELIANCE``) and ISIN wins over symbol when both
        sides have one. Returns per-position status -- matched, quantity_drift,
        cost_basis_drift, missing_in_lifestack, missing_at_depository -- with an
        actionable suggestion for each non-match (record a missed order, review
        fees on mis-fee'd orders, remove a stale holding). Pass ``account_id``
        when the external list covers a single account. ``quantity_tolerance``
        is an absolute share count (float noise); ``cost_tolerance_pct`` is the
        allowed relative difference in average cost. Read-only.
        """
        try:
            external = parse_external_list(external_list)
            quantity_tol, cost_tol = parse_tolerances(quantity_tolerance, cost_tolerance_pct)
            account_public_id = uuid.UUID(account_id) if account_id else None
        except ReconciliationInputError as exc:
            return {"status": "error", "message": str(exc)}
        except (TypeError, ValueError):
            return {"status": "error", "message": "Invalid account_id."}
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="reconcile_holdings"
            )
            try:
                result = await services.reconciliation_service(session).reconcile(
                    workspace_id,
                    external,
                    account_public_id=account_public_id,
                    quantity_tolerance=quantity_tol,
                    cost_tolerance_pct=cost_tol,
                )
            except ReconciliationInputError as exc:
                return {"status": "error", "message": str(exc)}
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "account_id": account_id,
            "quantity_tolerance": str(quantity_tol),
            "cost_tolerance_pct": str(cost_tol),
            **result,
        }

    @mcp.tool
    async def get_performance(
        workspace_id: int,
        account_id: str | None = None,
        period: str = "all",
        reporting_currency: str = "INR",
    ) -> dict[str, Any]:
        """XIRR (money-weighted) and time-weighted return, converted to INR.

        XIRR is reported overall, per brokerage account and per holding, since
        inception, as a fraction (``0.1234`` = 12.34%) plus ``xirr_pct``; a
        position whose flows cannot yield one gets ``xirr: null`` and
        ``xirr_status: "not_computable"`` (never an error). Time-weighted return
        is chain-linked from portfolio snapshots over ``period`` (all, 1m, 3m,
        6m, 1y, 3y, 5y, ytd) with external contributions/withdrawals neutralised.
        Amounts are given natively and converted to ``reporting_currency``
        (default INR) at the latest FX; a currency with no rate yields null
        converted values and an explicit warning, never zero. Read-only.
        """
        period = period.strip().lower()
        if period not in VALID_PERIODS:
            return {
                "status": "error",
                "message": f"period must be one of: {', '.join(VALID_PERIODS)}.",
            }
        reporting_currency = reporting_currency.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", reporting_currency):
            return {
                "status": "error",
                "message": "reporting_currency must be a 3-letter currency code.",
            }
        try:
            account_public_id = uuid.UUID(account_id) if account_id else None
        except ValueError:
            return {"status": "error", "message": "Invalid account_id."}
        today = datetime.now(UTC).date()
        from_date = period_start(period, today)
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_performance"
            )
            if account_public_id is not None:
                account = await AccountRepository(session).get_by_public_id(
                    workspace_id, account_public_id
                )
                if account is None:
                    return {"status": "error", "message": "Account not found in this workspace."}
            metrics, positions = await services.return_metrics_service(
                session
            ).get_performance_metrics(workspace_id)
            twr = await services.performance_service(session).get_time_weighted_return(
                workspace_id, from_date, today
            )
            used = {r.currency.upper() for r in metrics.by_account}
            used |= {p["currency"].upper() for p in positions}
            if twr.get("currency"):
                used.add(twr["currency"].upper())
            used.discard(reporting_currency)
            fx_as_of = effective_display_as_of()
            fx_lookup = (
                await FxRateRepository(session).get_latest_rates_for_pairs(
                    list(build_required_pairs(sorted(used), reporting_currency)), as_of=fx_as_of
                )
                if used
                else {}
            )
        report = build_performance_report(
            metrics=metrics,
            positions=positions,
            twr=twr,
            rate_for=lambda ccy: conversion_rate(ccy, reporting_currency, fx_lookup),
            reporting_currency=reporting_currency,
            account_public_id=account_public_id,
        )
        rates_used = {
            ccy: str(rate)
            for ccy in sorted(used)
            if (rate := conversion_rate(ccy, reporting_currency, fx_lookup)) is not None
        }
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "account_id": account_id,
            "period": period,
            "from_date": from_date.isoformat() if from_date else None,
            "to_date": today.isoformat(),
            "reporting_currency": reporting_currency,
            "fx_as_of": fx_as_of.isoformat() if used else None,
            "fx_rates_used": rates_used,
            **report,
        }

    @mcp.tool
    async def get_investing_summary(workspace_id: int) -> dict[str, Any]:
        """Get a read-only investing summary for an authorized workspace."""
        return await common.run_capture_tool(workspace_id, "mcp:read", "get_investing_summary", {})

    @mcp.tool
    async def set_instrument_price(
        workspace_id: int,
        holding_public_id: str,
        price_date: str,
        unit_price: str,
        source: str = "manual",
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Manually override one holding's price for a date (feed down, stale NAV).

        ``unit_price`` is a decimal string in the holding's currency and
        ``price_date`` an ISO date (YYYY-MM-DD). ``source`` labels the
        provenance (1-16 characters, default ``manual``). The price is
        upserted on ``(holding, price_date)``; when ``price_date`` is today,
        that day's portfolio snapshot is invalidated so performance reads
        recompute against the new price. Without ``confirmed`` the call
        previews and writes nothing. Recorded in the audit trail. Requires
        the mcp:write scope.
        """
        try:
            holding_public = uuid.UUID(holding_public_id)
            price_day = date.fromisoformat(price_date.strip())
            price = Decimal(unit_price.strip())
            if not price.is_finite() or price < 0:
                raise InvalidOperation("price must be a non-negative finite decimal")
            src = (source or "manual").strip()
            if not src or len(src) > 16:
                raise ValueError("source must be 1-16 characters")
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError, InvalidOperation) as exc:
            return {"status": "error", "message": f"Invalid price input: {exc}"}

        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:write",
                tool="set_instrument_price",
            )
            holding = await HoldingRepository(session).get_by_public_id(
                workspace_id, holding_public
            )
            if holding is None or holding.id is None:
                return {"status": "error", "message": "Holding not found in this workspace."}
            price_repo = HoldingPriceRepository(session)
            existing = await price_repo.latest_price_on_or_before(
                workspace_id, holding.id, price_day
            )
            before = (
                str(existing.unit_price)
                if existing is not None and existing.price_date == price_day
                else None
            )

            if not confirmed:
                return {
                    "status": "needs_confirmation",
                    "needs_confirmation": True,
                    "message": (
                        f"Set the price of holding {holding_public_id} to "
                        f"{price} {holding.currency} as of {price_day} (source "
                        f"{src})? Confirm to continue."
                    ),
                    "workspace_id": workspace_id,
                    "holding_public_id": holding_public_id,
                    "price_date": price_day.isoformat(),
                    "unit_price": str(price),
                    "source": src,
                    "before": before,
                }

            await price_repo.upsert_price(
                workspace_id=workspace_id,
                holding_id=holding.id,
                price_date=price_day,
                unit_price=price,
                source=src,
            )
            if price_day == datetime.now(UTC).date():
                await PortfolioSnapshotRepository(session).delete_for_date(
                    workspace_id, price_day
                )
            await AuditLogger(session).log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="update",
                module="investing",
                entity_type="holding_price",
                entity_id=holding.id,
                details={
                    "entity_public_id": str(holding_public),
                    "holding_symbol": holding.symbol,
                    "price_date": price_day.isoformat(),
                    "before": before,
                    "after": str(price),
                    "source": src,
                },
            )
            await session.commit()
        return {
            "status": "success",
            "entity_type": "holding_price",
            "workspace_id": workspace_id,
            "holding_public_id": holding_public_id,
            "price_date": price_day.isoformat(),
            "unit_price": str(price),
            "source": src,
            "changed": before is not None and before != str(price),
            "summary": (
                f"Price for {holding_public_id} on {price_day} set to {price} "
                f"{holding.currency} (source {src})."
            ),
        }

    @mcp.tool
    async def create_portfolio_report(
        workspace_id: int,
        as_of: str,
        run_id: str,
        scalars_json: dict[str, Any] | None = None,
        foundation_json: dict[str, Any] | None = None,
        actions_md: str | None = None,
        theme_weights: dict[str, Any] | None = None,
        harvest_candidates: list[Any] | None = None,
        lrs_headroom_inr: str | None = None,
        tax_buckets: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one portfolio report run in the append-only history (cron use).

        Idempotent on ``(workspace_id, as_of, run_id)``: re-sending the same run
        returns the existing row's ``public_id`` and never overwrites it; a new
        ``run_id`` on the same day adds a new row. ``as_of`` is an ISO date;
        ``lrs_headroom_inr`` a decimal string. The denormalised columns
        (``theme_weights``, ``harvest_candidates``, ``lrs_headroom_inr``,
        ``tax_buckets``) are stored as given so dashboards need not parse
        ``foundation_json``.
        """
        try:
            as_of_date = date.fromisoformat(as_of)
        except ValueError:
            return {"status": "error", "message": "as_of must be an ISO date (YYYY-MM-DD)."}
        if not 1 <= len(run_id) <= 128:
            return {"status": "error", "message": "run_id must be 1-128 characters."}
        headroom: Decimal | None = None
        if lrs_headroom_inr is not None:
            try:
                headroom = Decimal(lrs_headroom_inr)
            except InvalidOperation:
                return {"status": "error", "message": "lrs_headroom_inr must be a decimal string."}
            if not headroom.is_finite() or abs(headroom) >= MAX_LRS_HEADROOM_INR:
                return {
                    "status": "error",
                    "message": "lrs_headroom_inr must be a finite amount below 10^16.",
                }
            headroom = headroom.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        payload_size = len(
            json.dumps(
                [
                    scalars_json,
                    foundation_json,
                    actions_md,
                    theme_weights,
                    harvest_candidates,
                    tax_buckets,
                ],
                default=str,
            )
        )
        if payload_size > MAX_PORTFOLIO_REPORT_CHARS:
            return {
                "status": "error",
                "message": f"report payload exceeds {MAX_PORTFOLIO_REPORT_CHARS} characters.",
            }
        async with postgres.async_session_maker() as session, session.begin():
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="create_portfolio_report"
            )
            recorded = await services.portfolio_report_service(session).record_portfolio_report(
                workspace_id=workspace_id,
                as_of=as_of_date,
                run_id=run_id,
                scalars_json=scalars_json,
                foundation_json=foundation_json,
                actions_md=actions_md,
                theme_weights=theme_weights,
                harvest_candidates=harvest_candidates,
                lrs_headroom_inr=headroom,
                tax_buckets=tax_buckets,
            )
            report = recorded.report
            result: dict[str, Any] = {
                "status": "success",
                "created": recorded.created,
                "content_matches": recorded.content_matches,
                "public_id": str(report.public_id),
                "as_of": report.as_of.isoformat(),
                "run_id": report.run_id,
                "created_at": report.created_at.isoformat() if report.created_at else None,
            }
            if not recorded.content_matches:
                result["warning"] = (
                    "A report for this run_id already exists with different content; nothing "
                    "was changed (reports are append-only). Send a corrected report under a "
                    "new run_id."
                )
            return result

    @mcp.tool
    async def list_portfolio_reports(
        workspace_id: int,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 20,
        cursor: int | None = None,
        include_payload: bool = False,
    ) -> dict[str, Any]:
        """List portfolio report history for a workspace, newest ``as_of`` first.

        Filters on ``as_of`` (inclusive). Cursor-paginated with a total count so
        callers can verify no page was cut off. Items carry the denormalised
        columns; set ``include_payload=true`` to also get ``scalars_json``,
        ``foundation_json`` and ``actions_md`` (large -- narrow the date range and
        lower ``limit`` first).
        """
        if limit < 1 or limit > 100:
            return {"status": "error", "message": "limit must be between 1 and 100."}
        if cursor is not None and cursor < 0:
            return {"status": "error", "message": "cursor must be a non-negative integer."}
        try:
            from_d = date.fromisoformat(from_date) if from_date else None
            to_d = date.fromisoformat(to_date) if to_date else None
        except ValueError:
            return {"status": "error", "message": "from_date/to_date must be ISO dates."}
        offset = cursor or 0
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_portfolio_reports"
            )
            items, total = await services.portfolio_report_service(
                session
            ).repo.list_by_workspace_range(
                workspace_id, from_date=from_d, to_date=to_d, limit=limit, offset=offset
            )
        next_cursor = offset + len(items) if offset + len(items) < total else None
        return {
            "status": "success",
            "total": total,
            "next_cursor": next_cursor,
            "items": [
                {
                    "public_id": str(item.public_id),
                    "as_of": item.as_of.isoformat() if item.as_of else None,
                    "run_id": item.run_id,
                    "theme_weights": item.theme_weights,
                    "harvest_candidates": item.harvest_candidates,
                    "lrs_headroom_inr": (
                        str(item.lrs_headroom_inr) if item.lrs_headroom_inr is not None else None
                    ),
                    "tax_buckets": item.tax_buckets,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                    **(
                        {
                            "scalars_json": item.scalars_json,
                            "foundation_json": item.foundation_json,
                            "actions_md": item.actions_md,
                        }
                        if include_payload
                        else {}
                    ),
                }
                for item in items
            ],
        }

    @mcp.tool
    async def update_investing_account_status(
        workspace_id: int,
        account_id: str,
        market_scope: str | None = None,
        exchange_name: str | None = None,
        symbol: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Set a brokerage account's market scope, or flag one holding's exchange.

        The capital-gains tax regime depends on where a security is listed.
        `market_scope` sets the account-level default ("india", "foreign" or
        "mixed") used when the reference security master has no entry for a
        ticker.

        `exchange_name` sets the exchange on a holding. With `symbol` it
        applies to that one holding and also sets its country override,
        which beats the account default. Without `symbol` it applies to
        every holding in the account whose exchange is still unset.

        Holdings whose exchange is already set are skipped, and an
        unrecognised exchange name leaves the country unset rather than
        guessing.
        """
        if market_scope is not None and market_scope not in {"india", "foreign", "mixed"}:
            return {
                "status": "error",
                "message": "market_scope must be 'india', 'foreign' or 'mixed'.",
            }
        if not market_scope and not exchange_name:
            return {
                "status": "error",
                "message": "Supply market_scope and/or exchange_name.",
            }
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    f"Update the market status for account {account_id}? Confirm to continue."
                ),
                "account_id": account_id,
                "market_scope": market_scope,
                "exchange_name": exchange_name,
                "symbol": symbol,
            }
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:write",
                tool="update_investing_account_status",
            )
            service = services.tax_service(session)
            try:
                account = await service.set_account_market_scope(
                    workspace_id, account_id, market_scope
                )
            except ValueError as exc:
                return {"status": "error", "message": str(exc)}

            updated: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            if exchange_name:
                mic, country = resolve_exchange(exchange_name)
                instruments = await service.list_account_instruments(workspace_id, account_id)
                if symbol:
                    instruments = [i for i in instruments if i.symbol.upper() == symbol.upper()]
                    if not instruments:
                        return {
                            "status": "error",
                            "message": f"No holding named '{symbol}' in this account.",
                        }
                for instrument in instruments:
                    if instrument.exchange and not symbol:
                        skipped.append({
                            "symbol": instrument.symbol,
                            "reason": f"exchange already set to {instrument.exchange}",
                        })
                        continue
                    instrument.exchange = exchange_name
                    if country:
                        instrument.exchange_country_override = country
                    instrument.updated_at = datetime.now(UTC)
                    session.add(instrument)
                    updated.append({
                        "symbol": instrument.symbol,
                        "exchange": instrument.exchange,
                        "exchange_country_override": instrument.exchange_country_override,
                    })
                await session.flush()

            # Error returns above leave the transaction uncommitted (the session closes
            # and rolls back); only a fully applied change is committed.
            await session.commit()
            return {
                "status": "success",
                "account": {
                    "public_id": str(account.public_id),
                    "name": account.name,
                    "market_scope": account.market_scope,
                },
                "instruments_updated": updated,
                "instruments_skipped": skipped,
                "notes": [
                    "The account market scope is a default; a holding-level "
                    "exchange country override takes precedence over it.",
                    "An unrecognised exchange name leaves the country unset rather than guessing.",
                ],
            }
