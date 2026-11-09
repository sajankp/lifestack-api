"""Shape the ``get_performance`` MCP payload (spec-103).

Pure: takes already-loaded return metrics + TWR + a rate lookup and builds the
response. Money is ``Decimal`` internally and serialised as strings; a missing
FX rate yields ``None`` converted values plus an explicit warning -- never a
silent zero.
"""

from __future__ import annotations

import calendar
import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

from app.investing.schemas import ReturnMetricsResponse

MONEY = Decimal("0.01")
VALID_PERIODS = ("all", "1m", "3m", "6m", "1y", "3y", "5y", "ytd")
_PERIOD_MONTHS = {"1m": 1, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60}

XIRR_NOT_COMPUTABLE = (
    "XIRR needs at least one outflow and one inflow spanning at least a day; the flows were "
    "one-sided, too short, or the solver did not converge."
)


def period_start(period: str, today: date) -> date | None:
    """Window start for ``period`` (``None`` for ``all``). Calendar months,
    clamped to month length (31 Mar - 1m -> 28/29 Feb)."""
    if period == "all":
        return None
    if period == "ytd":
        return date(today.year, 1, 1)
    months = _PERIOD_MONTHS[period]
    index = today.year * 12 + (today.month - 1) - months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(MONEY))


def _opt(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _xirr_fields(xirr: Decimal | None) -> dict[str, Any]:
    return {
        "xirr": _opt(xirr),
        "xirr_pct": _money(xirr * Decimal("100")) if xirr is not None else None,
        "xirr_status": "ok" if xirr is not None else "not_computable",
    }


class _Converter:
    def __init__(self, rate_for: Callable[[str], Decimal | None], reporting_currency: str):
        self.rate_for = rate_for
        self.reporting_currency = reporting_currency
        self.missing: set[str] = set()

    def convert(self, value: Decimal | None, currency: str | None) -> Decimal | None:
        if value is None or not currency:
            return None
        rate = self.rate_for(currency.upper())
        if rate is None:
            self.missing.add(currency.upper())
            return None
        return value * rate


def build_performance_report(
    *,
    metrics: ReturnMetricsResponse,
    positions: list[dict],
    twr: dict[str, Any],
    rate_for: Callable[[str], Decimal | None],
    reporting_currency: str,
    account_public_id: uuid.UUID | None,
) -> dict[str, Any]:
    conv = _Converter(rate_for, reporting_currency)
    warnings: list[str] = []

    def amounts(
        native_currency: str | None,
        invested: Decimal,
        market_value: Decimal,
        realized: Decimal,
        unrealized: Decimal,
    ) -> dict[str, Any]:
        return {
            "invested": _money(invested),
            "market_value": _money(market_value),
            "realized": _money(realized),
            "unrealized": _money(unrealized),
            "converted": {
                "currency": reporting_currency,
                "invested": _money(conv.convert(invested, native_currency)),
                "market_value": _money(conv.convert(market_value, native_currency)),
                "realized": _money(conv.convert(realized, native_currency)),
                "unrealized": _money(conv.convert(unrealized, native_currency)),
            },
        }

    account_rows = [
        r
        for r in metrics.by_account
        if account_public_id is None or r.account_id == account_public_id
    ]
    holding_rows = [
        p for p in positions if account_public_id is None or p["account_id"] == account_public_id
    ]

    by_account: list[dict[str, Any]] = []
    totals = {
        "invested": Decimal("0"),
        "market_value": Decimal("0"),
        "realized": Decimal("0"),
        "unrealized": Decimal("0"),
    }
    overall_convertible = True
    for r in sorted(account_rows, key=lambda r: (r.account_name, str(r.account_id))):
        invested = (r.open.invested or Decimal("0")) + (r.closed.invested or Decimal("0"))
        market_value = r.open.market_value or Decimal("0")
        row_amounts = amounts(r.currency, invested, market_value, r.realized, r.unrealized)
        by_account.append({
            "account_id": str(r.account_id),
            "account_name": r.account_name,
            "currency": r.currency,
            **_xirr_fields(r.xirr),
            "annualized_return_pct": _opt(r.annualized_return_pct),
            "total_return_pct": _opt(r.total_return_pct),
            **row_amounts,
        })
        for key, value in (
            ("invested", invested),
            ("market_value", market_value),
            ("realized", r.realized),
            ("unrealized", r.unrealized),
        ):
            converted = conv.convert(value, r.currency)
            if converted is None:
                overall_convertible = False
            else:
                totals[key] += converted

    by_holding = [
        {
            "account_id": str(p["account_id"]),
            "account_name": p["account_name"],
            "symbol": p["symbol"],
            "currency": p["currency"],
            "is_open": p["is_open"],
            **_xirr_fields(p["xirr"]),
            "annualized_return_pct": _opt(p["annualized_return_pct"]),
            "total_return_pct": _opt(p["total_return_pct"]),
            **amounts(
                p["currency"],
                p["invested"] or Decimal("0"),
                p["market_value"] or Decimal("0"),
                p["realized"] or Decimal("0"),
                p["unrealized"] or Decimal("0"),
            ),
        }
        for p in holding_rows
    ]

    # Overall: workspace-wide (or the one requested account). Amounts are the
    # sum of per-account amounts each converted at the latest FX rate; XIRR is
    # a rate (currency-neutral) from the service.
    # An account with no investing activity has no row: report "not computable",
    # never the workspace-wide figures under an account label.
    if account_public_id is None:
        source = metrics.overall
    else:
        source = account_rows[0] if account_rows else None
        if source is None:
            warnings.append(
                "The requested account has no investing activity, so no return can be computed."
            )
    overall: dict[str, Any] = {
        "scope": "account" if account_public_id is not None else "workspace",
        **_xirr_fields(source.xirr if source is not None else None),
        "annualized_return_pct": _opt(source.annualized_return_pct if source else None),
        "total_return_pct": _opt(source.total_return_pct if source else None),
        "converted": {
            "currency": reporting_currency,
            **{
                key: (_money(value) if overall_convertible else None)
                for key, value in totals.items()
            },
        },
    }
    if account_public_id is None and metrics.valuation_status == "conversion_required":
        overall["xirr_status"] = "conversion_required"
        warnings.append(
            "Overall XIRR needs historical FX rates for every currency and a workspace "
            "reporting currency; some are missing, so it is null. Per-account and "
            "per-holding XIRR are unaffected."
        )
    # TWR amounts converted at the latest FX too.
    twr_out = dict(twr)
    twr_currency = twr.get("currency")
    start_value = Decimal(twr["start_value"]) if twr.get("start_value") is not None else None
    end_value = Decimal(twr["end_value"]) if twr.get("end_value") is not None else None
    flows = (
        Decimal(twr["net_external_flows"]) if twr.get("net_external_flows") is not None else None
    )
    twr_out["converted"] = {
        "currency": reporting_currency,
        "start_value": _money(conv.convert(start_value, twr_currency)),
        "end_value": _money(conv.convert(end_value, twr_currency)),
        "net_external_flows": _money(conv.convert(flows, twr_currency)),
    }
    twr_out["warnings"] = list(twr.get("warnings") or [])
    if account_public_id is not None:
        twr_out["warnings"].append(
            "Time-weighted return is computed for the whole workspace; portfolio snapshots "
            "are not stored per account."
        )

    if conv.missing:
        warnings.append(
            f"No FX rate to {reporting_currency} for {sorted(conv.missing)}: converted values "
            "involving those currencies are null (never zero). Native-currency values and "
            "XIRR are unaffected."
        )
    warnings.extend(twr_out["warnings"])
    if any(row["xirr_status"] == "not_computable" for row in (overall, *by_account, *by_holding)):
        warnings.append(XIRR_NOT_COMPUTABLE)

    return {
        "valuation_status": "conversion_required" if conv.missing else "current",
        "overall": overall,
        "by_account": by_account,
        "by_holding": by_holding,
        "time_weighted": twr_out,
        "warnings": warnings,
        "xirr_basis": "since_inception",
    }
