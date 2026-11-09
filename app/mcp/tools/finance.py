"""Net worth, accounts, capital transfers, LRS headroom and Rule 115 FX rates."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from fastmcp import FastMCP

from app.core.database import postgres
from app.core.exceptions import ValidationError
from app.core.exceptions import ValidationError as AppValidationError
from app.finance.lrs_service import LrsService
from app.finance.repository import (
    AccountRepository,
    CapitalTransferRepository,
    CurrencyRepository,
    FinanceSettingRepository,
    FxRateRepository,
    NetWorthSnapshotRepository,
)
from app.finance.service import AccountService, NetWorthService
from app.investing.performance_service import InvestingSummaryService
from app.investing.repository import (
    CashBalanceRepository,
    HoldingPriceRepository,
    HoldingRepository,
    PortfolioSnapshotRepository,
)
from app.investing.tax_service import normalize_currency_code, parse_fx_entries
from app.mcp import security
from app.mcp.support import common, services


def register(mcp: FastMCP) -> None:
    """Register the finance tools on ``mcp``."""

    @mcp.tool
    async def get_net_worth(workspace_id: int) -> dict[str, Any]:
        """Get the current net worth for an authorized workspace."""
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_net_worth"
            )
            account_repo = AccountRepository(session)
            setting_repo = FinanceSettingRepository(session)
            cash_repo = CashBalanceRepository(session)
            holding_repo = HoldingRepository(session)
            fx_rate_repo = FxRateRepository(session)
            summary = InvestingSummaryService(
                holding_repo,
                cash_repo,
                setting_repo,
                fx_rate_repo,
                HoldingPriceRepository(session),
                PortfolioSnapshotRepository(session),
                account_repo,
            )
            result = await NetWorthService(
                session,
                AccountService(account_repo, CurrencyRepository(session), setting_repo),
                summary,
                cash_repo,
                setting_repo,
                fx_rate_repo,
                NetWorthSnapshotRepository(session),
            ).get_net_worth(workspace_id)
            return result

    @mcp.tool
    async def list_transfers(
        workspace_id: int,
        day: str | None = None,
        from_day: str | None = None,
        to_day: str | None = None,
        account_name: str | None = None,
        amount: str | None = None,
        search: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """List capital transfers for a day or date range in an authorized workspace."""
        return await common.run_capture_tool(
            workspace_id,
            "mcp:read",
            "list_transfers",
            {
                "day": day,
                "from_day": from_day,
                "to_day": to_day,
                "account_name": account_name,
                "amount": amount,
                "search": search,
                "limit": limit,
            },
        )

    @mcp.tool
    async def find_transfers(
        workspace_id: int,
        from_day: str | None = None,
        to_day: str | None = None,
        account_name: str | None = None,
        amount: str | None = None,
        search: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Find bounded transfer candidates for a correction or deletion."""
        return await common.run_capture_tool(
            workspace_id,
            "mcp:read",
            "find_transfers",
            {
                "from_day": from_day,
                "to_day": to_day,
                "account_name": account_name,
                "amount": amount,
                "search": search,
                "limit": limit,
            },
        )

    @mcp.tool
    async def create_transfer(
        workspace_id: int,
        from_account_name: str,
        to_account_name: str,
        amount: str,
        fx_rate: str | None = None,
        fees: str | None = None,
        fx_fee_amount: str | None = None,
        platform_fee_amount: str | None = None,
        tax_amount: str | None = None,
        net_amount: str | None = None,
        occurred_at: str | None = None,
        notes: str | None = None,
        source_ref: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Create a capital transfer between accounts in an authorized workspace."""
        return await common.run_capture_tool(
            workspace_id,
            "mcp:write",
            "create_transfer",
            {
                "from_account_name": from_account_name,
                "to_account_name": to_account_name,
                "amount": amount,
                "fx_rate": fx_rate,
                "fees": fees,
                "fx_fee_amount": fx_fee_amount,
                "platform_fee_amount": platform_fee_amount,
                "tax_amount": tax_amount,
                "net_amount": net_amount,
                "occurred_at": occurred_at,
                "notes": notes,
                "source_ref": source_ref,
                "confirmed": confirmed,
            },
        )

    @mcp.tool
    async def update_transfer(
        workspace_id: int,
        public_id: str,
        from_account_name: str | None = None,
        to_account_name: str | None = None,
        amount: str | None = None,
        fx_rate: str | None = None,
        fees: str | None = None,
        fx_fee_amount: str | None = None,
        platform_fee_amount: str | None = None,
        tax_amount: str | None = None,
        net_amount: str | None = None,
        occurred_at: str | None = None,
        notes: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Update an existing capital transfer in an authorized workspace."""
        return await common.run_capture_tool(
            workspace_id,
            "mcp:write",
            "update_transfer",
            {
                "public_id": public_id,
                "from_account_name": from_account_name,
                "to_account_name": to_account_name,
                "amount": amount,
                "fx_rate": fx_rate,
                "fees": fees,
                "fx_fee_amount": fx_fee_amount,
                "platform_fee_amount": platform_fee_amount,
                "tax_amount": tax_amount,
                "net_amount": net_amount,
                "occurred_at": occurred_at,
                "notes": notes,
                "confirmed": confirmed,
            },
        )

    @mcp.tool
    async def delete_transfer(
        workspace_id: int, public_id: str, confirmed: bool = False
    ) -> dict[str, Any]:
        """Delete an existing capital transfer in an authorized workspace."""
        return await common.run_capture_tool(
            workspace_id,
            "mcp:write",
            "delete_transfer",
            {"public_id": public_id, "confirmed": confirmed},
        )

    @mcp.tool
    async def get_account_balances(workspace_id: int) -> dict[str, Any]:
        """Get read-only spending-account balances for an authorized workspace."""
        return await common.run_capture_tool(workspace_id, "mcp:read", "get_account_balances", {})

    @mcp.tool
    async def set_fx_rate(
        workspace_id: int,
        date_str: str,
        currency: str,
        rate: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Set a daily Rule 115 FX rate for a specific date and currency.

        `date_str`: "YYYY-MM-DD" — the Rule 115 specified date (last day of month before sale month).
        `currency`: e.g., "USD", "GBP".
        `rate`: SBI TT buying rate ₹ per 1 unit (decimal string).
        The key is stored as "YYYY-MM-DD:CURRENCY" for multi-currency workspaces.
        """
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": f"Set FX rate for {date_str} ({currency.upper()}) to {rate}? Confirm to continue.",
            }

        # Validate that the date is a real calendar date, not only the right shape.
        try:
            parsed_date = date.fromisoformat(date_str)
        except ValueError:
            return {"status": "error", "message": "date must be a valid YYYY-MM-DD date."}
        if parsed_date.isoformat() != date_str:
            return {"status": "error", "message": "date must be YYYY-MM-DD format."}

        currency = currency.upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            return {"status": "error", "message": "currency must be a 3-letter code (e.g., USD)."}

        try:
            rate_value = Decimal(rate)
        except (InvalidOperation, ValueError):
            return {"status": "error", "message": "rate must be a decimal string."}
        if not rate_value.is_finite() or rate_value <= 0 or rate_value > Decimal("1000"):
            return {"status": "error", "message": "rate must be finite and between 0 and 1000."}

        composite_key = f"{date_str}:{currency}"
        # Canonical text (no exponent / padding forms) so get_fx_rate returns what was meant.
        rate_text = format(rate_value, "f")

        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="set_fx_rate"
            )
            service = services.tax_service(session)
            # A deliberate correction: replace any existing value. The merge runs under a
            # row lock so a concurrent writer's entries are not lost.
            await service.set_fx_overrides(workspace_id, {composite_key: rate_text}, overwrite=True)
            profile = await service.get_profile(workspace_id)
            await session.commit()

        return {
            "status": "success",
            "message": f"FX rate set for {date_str} ({currency}): {rate_text}",
            "key": composite_key,
            "rate": rate_text,
            "fx_overrides": dict(profile.fx_overrides or {}),
        }

    @mcp.tool
    async def set_fx_rates(
        workspace_id: int,
        rates: list[dict[str, str]],
        overwrite_existing: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Backfill many daily Rule 115 FX rates in one call.

        ``rates`` is a list of ``{"date": "YYYY-MM-DD", "currency": "USD", "rate": "83.4"}``
        (up to 400 -- a financial year of daily rates). Use ``list_missing_fx_rates`` to find
        the gaps. Everything is validated first and nothing is written if any entry is bad;
        the error names the entry (``rates[3]: ...``). Dates may not be in the future.

        A rate already stored for the same date + currency is **kept** (reported under
        ``skipped_existing``) unless ``overwrite_existing=true``; an identical value is
        ``unchanged``. Without ``confirmed=true`` nothing is written and the result is a
        preview of the counts (``add`` / ``update`` / ``skip_existing`` / ``unchanged``).
        Concurrent calls merge safely.
        """
        try:
            entries = parse_fx_entries(rates)
        except AppValidationError as exc:
            return {"status": "error", "message": exc.detail}

        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="set_fx_rates"
            )
            service = services.tax_service(session)
            if not confirmed:
                preview = await service.set_fx_overrides(
                    workspace_id, entries, overwrite=overwrite_existing, apply=False
                )
                return {
                    "status": "needs_confirmation",
                    "needs_confirmation": True,
                    "message": (
                        f"Store {len(entries)} FX rate(s)? "
                        f"{len(preview.added)} new, {len(preview.updated)} replaced, "
                        f"{len(preview.skipped_existing)} kept as already set, "
                        f"{len(preview.unchanged)} identical. Confirm to continue."
                    ),
                    "counts": {
                        "add": len(preview.added),
                        "update": len(preview.updated),
                        "skip_existing": len(preview.skipped_existing),
                        "unchanged": len(preview.unchanged),
                    },
                }
            result = await service.set_fx_overrides(
                workspace_id, entries, overwrite=overwrite_existing
            )
            await session.commit()
        return {
            "status": "success",
            "counts": {
                "added": len(result.added),
                "updated": len(result.updated),
                "skipped_existing": len(result.skipped_existing),
                "unchanged": len(result.unchanged),
            },
            "skipped_existing": result.skipped_existing,
        }

    @mcp.tool
    async def get_lrs_summary(
        workspace_id: int,
        fy: str | None = None,
    ) -> dict[str, Any]:
        """FY outward remittance vs the USD 250,000 LRS cap, with TCS status.

        Built from INR-funded transfers into foreign-currency investing accounts
        (e.g. Paasa / a US brokerage) in the workspace; repatriations and INR
        brokerage funding are excluded. `fy` is an Indian financial year label
        such as "2026-27" (1 Apr - 31 Mar, IST) and defaults to the current one.
        Money values are decimal strings. See `assumptions` for limits.
        """
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_lrs_summary"
            )
            service = LrsService(
                CapitalTransferRepository(session), fx_rate_repository=FxRateRepository(session)
            )
            try:
                summary = await service.get_lrs_summary(workspace_id, fy)
            except ValidationError as exc:
                return {"status": "error", "message": exc.detail}
        return {
            "status": "success",
            "workspace_id": workspace_id,
            **summary,
            "fy_start": summary["fy_start"].isoformat(),
            "fy_end": summary["fy_end"].isoformat(),
        }

    @mcp.tool
    async def get_fx_rate(
        workspace_id: int,
        date_str: str,
        currency: str,
    ) -> dict[str, Any]:
        """Read the stored Rule 115 FX rate for a date + currency.

        Reads the user-provided rate written via `set_fx_rate` (key
        "YYYY-MM-DD:CURRENCY"). Returns status "not_found" (the REST 404) when no
        rate is stored for that exact date and currency.
        """
        try:
            parsed = date.fromisoformat(date_str)
        except ValueError:
            return {"status": "error", "message": "date_str must be a valid YYYY-MM-DD date."}
        if parsed.isoformat() != date_str:
            return {"status": "error", "message": "date_str must be YYYY-MM-DD format."}
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_fx_rate"
            )
            try:
                code = normalize_currency_code(currency)
                rate = await services.tax_service(session).get_fx_rate(workspace_id, parsed, code)
            except ValidationError as exc:
                return {"status": "error", "message": exc.detail}
        if rate is None:
            return {
                "status": "not_found",
                "message": f"No FX rate stored for {date_str} ({code}).",
            }
        return {
            "status": "success",
            "date": date_str,
            "currency": code,
            "rate": rate,
            "source": "user_provided",
        }

    @mcp.tool
    async def list_missing_fx_rates(
        workspace_id: int,
        from_date: str,
        to_date: str,
        currency: str,
    ) -> dict[str, Any]:
        """Dates in [from_date, to_date] (YYYY-MM-DD, max 366 days) with no stored Rule 115 rate."""
        try:
            from_d = date.fromisoformat(from_date)
            to_d = date.fromisoformat(to_date)
        except ValueError:
            return {"status": "error", "message": "dates must be valid YYYY-MM-DD dates."}
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_missing_fx_rates"
            )
            try:
                code = normalize_currency_code(currency)
                missing = await services.tax_service(session).list_missing_fx_rates(
                    workspace_id, from_d, to_d, code
                )
            except ValidationError as exc:
                return {"status": "error", "message": exc.detail}
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "from_date": from_d.isoformat(),
            "to_date": to_d.isoformat(),
            "currency": code,
            "missing": [d.isoformat() for d in missing],
            "missing_count": len(missing),
        }
