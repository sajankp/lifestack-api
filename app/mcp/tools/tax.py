"""Capital-gains tax: summary, what-if sale, lot listing, harvesting and the tax profile."""

from typing import Any

from fastmcp import FastMCP

from app.auth.models import User
from app.core.database import postgres
from app.investing.tax_whatif_service import (
    WhatIfError,
    parse_decimal,
    parse_sale_date,
    resolve_zone,
)
from app.investing.tax_whatif_views import (
    harvest_response,
    lot_listing_response,
    simulation_response,
)
from app.mcp import security
from app.mcp.support import services


async def _user_timezone_name(session: Any, user_id: int) -> str | None:
    user = await session.get(User, user_id)
    return user.timezone if user is not None and user.timezone else None


def _what_if_error(exc: WhatIfError) -> dict[str, Any]:
    return {"status": "error", "code": exc.code, "message": exc.message, **exc.details}


def register(mcp: FastMCP) -> None:
    """Register the tax tools on ``mcp``."""

    @mcp.tool
    async def get_tax_summary(
        workspace_id: int,
        fy: str | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """Get the capital gains tax estimate for a workspace and financial year.

        Returns the LTCG and STCG buckets with their tax, the per-lot
        detail, the FX rates used (with their source), and the assumptions
        behind the number. This is an estimate, not a filing-ready
        liability.

        `fy` is an Indian financial year label such as "2026-27" and
        defaults to the current one.
        """
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_tax_summary"
            )
            estimate = await services.tax_service(session).compute(
                workspace_id, fy=fy, account_id=account_id
            )
        if estimate.status == "reconciliation_failed":
            return {
                "status": "reconciliation_failed",
                "message": estimate.message,
                "reconciliation_failures": estimate.reconciliation_failures,
            }
        if estimate.status == "missing_fx_rate":
            return {
                "status": "missing_fx_rate",
                "message": estimate.message,
                "missing_fx_rates": estimate.missing_fx_rates,
            }
        return {
            "status": "success",
            "fy_label": estimate.fy_label,
            "currency": estimate.currency,
            "buckets": {key: bucket.as_dict() for key, bucket in estimate.buckets.items()},
            "total_gain_inr": str(estimate.total_gain_inr),
            "total_tax_inr": str(estimate.total_tax_inr),
            "effective_rate_pct": str(estimate.effective_rate_pct),
            "slab": (
                {
                    "rate_pct": str(estimate.slab.rate_pct),
                    "source": estimate.slab.source,
                    "tax_regime": estimate.slab.tax_regime,
                    "surcharge_pct": str(estimate.slab.surcharge_pct),
                    "cess_pct": str(estimate.slab.cess_pct),
                    "rebate_amount": str(estimate.slab.rebate_amount),
                    "total_income_considered": str(estimate.slab.total_income_considered),
                }
                if estimate.slab is not None
                else None
            ),
            "fx_rates": [f.as_dict() for f in estimate.fx_rates],
            "positions": [
                {
                    "symbol": lot.symbol,
                    "account_name": lot.account_name,
                    "instrument_type": lot.instrument_type,
                    "currency": lot.currency,
                    "quantity": str(lot.quantity),
                    "cost_per_unit": str(lot.cost_per_unit),
                    "acquired_at": lot.acquired_at.isoformat(),
                    "sold_at": lot.sold_at.isoformat(),
                    "long_term_mark_date": lot.long_term_mark_date.isoformat(),
                    "holding_months": lot.holding_months,
                    "is_long_term": lot.is_long_term,
                    "regime": str(lot.regime),
                    "classified_by": lot.classified_by,
                    "classification_detail": lot.classification_detail,
                    "gain_native": str(lot.gain_native),
                    "gain_inr": str(lot.gain_inr),
                }
                for lot in estimate.lots
            ],
            "assumptions": estimate.assumptions,
            "disclaimers": estimate.disclaimers,
        }

    @mcp.tool
    async def simulate_sale(
        workspace_id: int,
        holding: str,
        qty: str,
        date: str,
        price: str,
        sell_fees: str = "0",
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """Read-only what-if: the capital-gains tax of selling `qty` units of a holding.

        Nothing is sold or changed. FIFO open lots are consumed oldest first;
        each lot is classified long-term/short-term on calendar days (more
        than 12 months for Indian-listed, 24 for foreign). The tax is
        INCREMENTAL: the estimated tax for the sale's financial year with the
        sale minus without it, on top of the gains already realised that FY
        (the same data get_tax_summary uses), so the Rs 1.25 lakh Sec 112A
        exemption is only what is left. Returns per-lot gains, the Rule 115
        FX rate and its source, a status (success, insufficient_quantity,
        missing_fx_rate, reconciliation_failed) and the assumptions used.

        `holding` is a holding public_id (from list_investment_holdings), or
        a symbol; a symbol held in several accounts is rejected with the
        candidates unless `account_id` (account public_id) narrows it.
        `date` is the sale day, YYYY-MM-DD in your timezone. `qty`, `price`
        and `sell_fees` are decimal strings (price and fees in the holding's
        currency).
        """
        try:
            qty_value = parse_decimal("qty", qty, allow_zero=False, max_places=8)
            price_value = parse_decimal("price", price, allow_zero=True, max_places=8)
            fees_value = parse_decimal("sell_fees", sell_fees, allow_zero=True, max_places=4)
        except WhatIfError as exc:
            return _what_if_error(exc)
        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="simulate_sale"
            )
            timezone_name = await _user_timezone_name(session, user_id)
            try:
                sale_dt = parse_sale_date(date, resolve_zone(timezone_name))
                outcome = await services.tax_whatif_service(session).simulate_sale(
                    workspace_id,
                    holding_ref=holding,
                    account_id=account_id,
                    qty=qty_value,
                    sale_date=sale_dt,
                    sale_price=price_value,
                    sell_fees=fees_value,
                    timezone_name=timezone_name,
                )
            except WhatIfError as exc:
                return _what_if_error(exc)
            return simulation_response(outcome)

    @mcp.tool
    async def list_tax_lots(
        workspace_id: int,
        holding: str,
        account_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Read-only: a holding's open FIFO tax lots with their long-term countdown.

        Per lot: acquired date, quantity, cost per unit (fee-inclusive),
        whether it is long-term today, and `days_to_long_term` (calendar
        days until the first day a sale is long-term; 0 = already). The
        threshold is 12 months for Indian-listed holdings and 24 for foreign
        ones. Unrealised P&L is included when a stored price exists.

        `holding` is a holding public_id or a symbol (use `account_id` to
        disambiguate a symbol held in several accounts). Lots are paged:
        `limit` 1-500, `offset` >= 0.
        """
        if not 1 <= limit <= 500 or offset < 0:
            return {
                "status": "error",
                "code": "invalid_page",
                "message": "limit must be 1-500 and offset non-negative.",
            }
        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_tax_lots"
            )
            try:
                listing = await services.tax_whatif_service(session).list_lots(
                    workspace_id,
                    holding_ref=holding,
                    account_id=account_id,
                    timezone_name=await _user_timezone_name(session, user_id),
                )
            except WhatIfError as exc:
                return _what_if_error(exc)
        response = lot_listing_response(listing)
        total = len(response["lots"])
        response["lot_count"] = total
        response["lots"] = response["lots"][offset : offset + limit]
        response["offset"] = offset
        response["has_more"] = offset + limit < total
        return response

    @mcp.tool
    async def harvest_candidates(
        workspace_id: int,
        stcg_rate_percent: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read-only: unrealised-loss lots worth realising for tax, best first.

        Scans every non-zero holding's open lots against its latest stored
        price, ranks loss lots by the tax the loss would save (converted to
        INR so mixed currencies compare), and reports each lot's price date
        and whether that price is stale. Holdings with no stored price or no
        open lots are listed under `skipped_holdings`, never guessed. A
        foreign short-term loss follows your slab, so it is ranked last
        unless you pass `stcg_rate_percent` (e.g. "30").

        `limit` 1-200 caps the returned candidates (candidate_count is the
        total).
        """
        if not 1 <= limit <= 200:
            return {
                "status": "error",
                "code": "invalid_page",
                "message": "limit must be between 1 and 200.",
            }
        stcg_value = None
        if stcg_rate_percent is not None:
            try:
                stcg_value = parse_decimal("stcg_rate_percent", stcg_rate_percent, allow_zero=True)
            except WhatIfError as exc:
                return _what_if_error(exc)
            if stcg_value > 100:
                return {
                    "status": "error",
                    "code": "invalid_number",
                    "message": "stcg_rate_percent must be between 0 and 100.",
                }
        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="harvest_candidates"
            )
            report = await services.tax_whatif_service(session).harvest(
                workspace_id,
                stcg_rate_percent=stcg_value,
                timezone_name=await _user_timezone_name(session, user_id),
            )
        return harvest_response(report, limit)

    @mcp.tool
    async def get_tax_profile(workspace_id: int) -> dict[str, Any]:
        """Get the workspace's capital gains tax configuration."""
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_tax_profile"
            )
            profile = await services.tax_service(session).get_profile(workspace_id)
        return {
            "public_id": str(profile.public_id),
            "resident_status": profile.resident_status,
            "tax_regime": profile.tax_regime,
            "other_income": str(profile.other_income),
            "slab_override_pct": str(profile.slab_override_pct)
            if profile.slab_override_pct is not None
            else None,
            "surcharge_override_pct": str(profile.surcharge_override_pct)
            if profile.surcharge_override_pct is not None
            else None,
            "cess_pct": str(profile.cess_pct),
            "explicit_regime": profile.explicit_regime,
            "apply_carry_forward": bool(profile.apply_carry_forward),
            "fx_overrides": dict(profile.fx_overrides or {}),
            "notes": profile.notes,
        }

    @mcp.tool
    async def update_tax_profile(
        workspace_id: int,
        resident_status: str | None = None,
        tax_regime: str | None = None,
        other_income: str | None = None,
        slab_override_pct: str | None = None,
        surcharge_override_pct: str | None = None,
        cess_pct: str | None = None,
        explicit_regime: str | None = None,
        apply_carry_forward: bool | None = None,
        fx_overrides: dict[str, str] | None = None,
        notes: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Update the workspace's capital gains tax configuration.

        All fields are optional — only provided fields are updated.
        `fx_overrides` accepts keys in "YYYY-MM" (legacy monthly) or "YYYY-MM-DD:CURRENCY" (daily) format.
        """
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": "Update tax profile? Confirm to continue.",
            }

        changes: dict[str, Any] = {}
        if resident_status is not None:
            changes["resident_status"] = resident_status
        if tax_regime is not None:
            changes["tax_regime"] = tax_regime
        if other_income is not None:
            changes["other_income"] = other_income
        if slab_override_pct is not None:
            changes["slab_override_pct"] = slab_override_pct
        if surcharge_override_pct is not None:
            changes["surcharge_override_pct"] = surcharge_override_pct
        if cess_pct is not None:
            changes["cess_pct"] = cess_pct
        if explicit_regime is not None:
            changes["explicit_regime"] = explicit_regime
        if apply_carry_forward is not None:
            changes["apply_carry_forward"] = apply_carry_forward
        if fx_overrides is not None:
            changes["fx_overrides"] = fx_overrides
        if notes is not None:
            changes["notes"] = notes

        if not changes:
            return {"status": "error", "message": "No fields to update."}

        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="update_tax_profile"
            )
            profile = await services.tax_service(session).update_profile(workspace_id, changes)
            await session.commit()
        return {
            "public_id": str(profile.public_id),
            "resident_status": profile.resident_status,
            "tax_regime": profile.tax_regime,
            "other_income": str(profile.other_income),
            "slab_override_pct": str(profile.slab_override_pct)
            if profile.slab_override_pct is not None
            else None,
            "surcharge_override_pct": str(profile.surcharge_override_pct)
            if profile.surcharge_override_pct is not None
            else None,
            "cess_pct": str(profile.cess_pct),
            "explicit_regime": profile.explicit_regime,
            "apply_carry_forward": bool(profile.apply_carry_forward),
            "fx_overrides": dict(profile.fx_overrides or {}),
            "notes": profile.notes,
        }
