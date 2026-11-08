"""JSON shapes of the read-only tax what-if tools (spec-106).

Money, quantities and rates are Decimal *strings*. Dates are ISO calendar
days in the user's timezone; internal database ids are never exposed.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.investing.tax.regime import REGIMES
from app.investing.tax_whatif_service import (
    FxInfo,
    HarvestReport,
    HoldingContext,
    LotListing,
    PriceInfo,
    SimulationOutcome,
)

DISCLAIMER = (
    "This is an estimate, not a filing-ready liability. It ignores prior-year "
    "losses, advance tax and your full income tax computation; verify with your CA."
)


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _day(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    return (value.date() if isinstance(value, datetime) else value).isoformat()


def holding_block(context: HoldingContext) -> dict[str, Any]:
    snap = context.snapshot
    spec = REGIMES[snap.regime]
    return {
        "holding_id": str(context.holding.public_id),
        "symbol": snap.symbol,
        "account_id": snap.account_id,
        "account_name": snap.account_name,
        "currency": snap.currency,
        "holding_quantity": str(context.holding.quantity),
        "open_lots_quantity": str(context.lots_quantity),
        "regime": {
            "type": str(snap.regime),
            "label": spec.label,
            "classified_by": snap.regime_source,
            "detail": context.regime_detail,
            "long_term_after_months": spec.ltcg_holding_months,
        },
    }


def price_block(price: PriceInfo | None) -> dict[str, Any] | None:
    if price is None:
        return None
    return {
        "unit_price": str(price.unit_price),
        "price_date": price.price_date.isoformat(),
        "source": price.source,
        "age_days": price.age_days,
        "is_stale": price.is_stale,
    }


def fx_block(fx: FxInfo | None) -> dict[str, Any] | None:
    if fx is None:
        return None
    return {
        "currency": fx.currency,
        "rate_to_inr": str(fx.rate),
        "as_of": fx.as_of.isoformat() if fx.as_of else None,
        "source": fx.source,
    }


def lot_listing_response(listing: LotListing) -> dict[str, Any]:
    """``list_tax_lots``: open lots with their holding-period countdown."""
    lots = []
    for position, view in enumerate(listing.lots, start=1):
        lots.append({
            "fifo_position": position,
            "acquired_date": _day(view.acquired_at),
            "quantity": str(view.quantity),
            "cost_per_unit": str(view.cost_per_unit),
            "cost_total": str((view.quantity * view.cost_per_unit).quantize(Decimal("0.01"))),
            "holding_months": view.holding_months,
            "is_long_term": view.is_long_term,
            "days_to_long_term": view.days_to_long_term,
            "long_term_from": view.long_term_from.isoformat(),
            "unrealised_pnl_native": _s(view.unrealised_pnl_native),
            "unrealised_pnl_inr": _s(view.unrealised_pnl_inr),
        })
    return {
        "status": "success",
        "as_of": listing.as_of.isoformat(),
        "timezone": listing.timezone,
        "holding": holding_block(listing.context),
        "lot_count": len(lots),
        "lots": lots,
        "current_price": price_block(listing.price),
        "fx": fx_block(listing.fx),
        "warnings": listing.warnings,
        "notes": [
            "Lots are FIFO (oldest first), as recorded from buy orders; cost per unit "
            "includes buy-side fees (Sec 48).",
            "Long-term needs a holding period of MORE than the threshold, so "
            "days_to_long_term counts to the day after the anniversary.",
        ],
    }


def simulation_response(outcome: SimulationOutcome) -> dict[str, Any]:
    """``simulate_sale``: lots consumed, gains, FX and incremental tax."""
    sim = outcome.simulation
    tz_name = outcome.timezone
    lots = []
    for position, lot in enumerate(sim.consumed_lots, start=1):
        lots.append({
            "fifo_position": position,
            "acquired_date": _day(lot.acquired_at),
            "quantity": str(lot.quantity),
            "cost_per_unit": str(lot.cost_per_unit),
            "sale_price_per_unit": str(lot.sale_price_per_unit),
            "gain_native": str(lot.gain_native),
            "sell_fees_native": str(lot.fees_native),
            "gain_inr": _s(lot.gain_inr),
            "is_long_term": lot.is_long_term,
            "holding_months": lot.holding_months,
            "days_to_long_term": lot.days_to_long_term,
            "long_term_from": _day(lot.long_term_from),
        })
    fx = (
        {
            "month": f"{sim.sale_date.year:04d}-{sim.sale_date.month:02d}",
            "specified_date": outcome.specified_fx_date.isoformat(),
            "currency": sim.currency,
            "rate_to_inr": str(sim.fx_rate),
            "source": sim.fx_source,
        }
        if sim.fx_rate is not None
        else None
    )
    tax = None
    if sim.tax_inr is not None:
        tax = {
            "incremental_tax_inr": str(sim.tax_inr),
            "fy_tax_before_inr": _s(sim.tax_before_inr),
            "fy_tax_after_inr": _s(sim.tax_after_inr),
            "realised_lots_in_fy": sim.fy_realised_lots,
            "realised_gain_in_fy_inr": _s(sim.fy_realised_gain_inr),
            "realised_ltcg_in_fy_inr": _s(sim.fy_realised_ltcg_inr),
            "ltcg_exemption_remaining_inr": _s(sim.ltcg_exemption_remaining_inr),
            "ltcg_exemption_used_by_sale_inr": _s(sim.ltcg_exemption_used_inr),
        }
    response: dict[str, Any] = {
        "status": sim.status,
        "message": sim.message,
        "timezone": tz_name,
        "holding": holding_block(outcome.context),
        "sale": {
            "date": sim.sale_date.date().isoformat(),
            "fy": sim.fy_label,
            "quantity_requested": str(sim.quantity_sold),
            "quantity_consumed": str(sim.quantity_consumed),
            "price_per_unit": str(sim.sale_price),
            "sell_fees": str(outcome.sell_fees),
            "currency": sim.currency,
        },
        "lots": lots,
        "gains": {
            "gain_native": str(sim.gain_native),
            "ltcg_native": str(sim.ltcg_gain_native),
            "stcg_native": str(sim.stcg_gain_native),
            "gain_inr": _s(sim.gain_inr),
        },
        "fx": fx,
        "tax": tax,
        "assumptions": sim.assumptions,
        "warnings": outcome.warnings,
        "disclaimer": DISCLAIMER,
    }
    if sim.status == "missing_fx_rate":
        response["missing_fx_rates"] = [
            {
                "month": f"{sim.sale_date.year:04d}-{sim.sale_date.month:02d}",
                "currency": sim.currency,
                "specified_date": outcome.specified_fx_date.isoformat(),
            }
        ]
    return response


def harvest_response(report: HarvestReport, limit: int) -> dict[str, Any]:
    """``harvest_candidates``: unrealised-loss lots ranked by tax saved."""
    shown = report.rows[:limit]
    candidates = []
    for rank, row in enumerate(shown, start=1):
        c = row.candidate
        snap = row.context.snapshot
        candidates.append({
            "rank": rank,
            "holding_id": str(row.context.holding.public_id),
            "symbol": c.symbol,
            "account_name": snap.account_name,
            "currency": snap.currency,
            "regime": str(c.regime),
            "fifo_position": row.fifo_position,
            "acquired_date": row.acquired_on.isoformat(),
            "quantity": str(c.quantity),
            "cost_per_unit": str(c.cost_per_unit),
            "current_price": str(c.current_price),
            "price_date": row.price.price_date.isoformat(),
            "price_source": row.price.source,
            "price_is_stale": row.price.is_stale,
            "unrealised_loss_native": str(c.unrealised_loss_native),
            "is_long_term": c.is_long_term,
            "rate_percent": (_s(c.rate_pct * Decimal("100")) if c.rate_pct is not None else None),
            "tax_saved_native": _s(c.tax_saved_native),
            "tax_saved_inr": _s(c.tax_saved_inr),
            "reason": c.reason,
        })
    return {
        "status": "success",
        "as_of": report.as_of.isoformat(),
        "timezone": report.timezone,
        "holdings_considered": report.holdings_considered,
        "candidate_count": len(report.rows),
        "returned": len(candidates),
        "candidates": candidates,
        "skipped_holdings": report.skipped,
        "fx_rates": [fx_block(f) for f in report.fx_rates],
        "missing_fx_currencies": report.missing_fx_currencies,
        "warnings": report.warnings,
        "assumptions": [
            "tax_saved is loss x the statutory base rate (12.5% long-term; 20% Sec 111A "
            "short-term, or the stcg_rate_percent you pass; foreign short-term follows your "
            "slab so it is unranked unless you pass a rate). Cess, surcharge and the "
            "₹1.25 lakh Sec 112A exemption are not modelled.",
            "A loss only saves tax if you have gains to set it against in the same FY "
            "(long-term loss only against long-term gain).",
            "Unrealised losses use the latest stored price (price_date shown); INR values use "
            "the latest stored market rate, not the Rule 115 rate.",
        ],
        "disclaimer": DISCLAIMER,
    }
