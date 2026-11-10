"""Constituents, sectors, halal compliance, themes and look-through exposure."""

import decimal
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import structlog
from fastmcp import FastMCP
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from app.core.audit import AuditLogger
from app.core.database import postgres
from app.core.exceptions import APIError
from app.core.exceptions import ValidationError as AppValidationError
from app.investing.company_merge_service import (
    CompanyMergeService,
    MergeConflictError,
)
from app.investing.company_sector_service import SectorRowError
from app.investing.models import Instrument, InstrumentConstituent
from app.investing.repository import (
    CompanyRepository,
    InstrumentConstituentRepository,
    InstrumentRepository,
)
from app.investing.schemas import (
    CompanySectorRow,
    HoldingComplianceUpsert,
    InstrumentConstituentUpsert,
)
from app.investing.theme_schemas import GroupBy, HoldingThemeTagsInput, ThemeTagInput
from app.mcp import security
from app.mcp.support import services

logger = structlog.get_logger(__name__)


def register(mcp: FastMCP) -> None:
    """Register the research tools on ``mcp``."""

    @mcp.tool
    async def get_investment_constituents(
        workspace_id: int,
        instrument_public_id: str,
        as_of_date: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Read the latest sourced ETF or mutual-fund constituent snapshot.

        Returns the snapshot on or before ``as_of_date`` (latest snapshot by
        ``(as_of_date, fetched_at)`` counts when several exist), optionally
        filtered by ``source``. Each row carries the constituent company's
        ``sector`` and ``industry`` where known.
        """
        try:
            instrument_id = uuid.UUID(instrument_public_id)
            as_of = date.fromisoformat(as_of_date) if as_of_date else datetime.now(UTC).date()
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid instrument_public_id or as_of_date."}

        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="get_investment_constituents",
            )
            items = await services.constituent_service(session).get_constituents(
                workspace_id, instrument_id, as_of, source=source
            )
            return {
                "status": "success",
                "workspace_id": workspace_id,
                "instrument_public_id": instrument_public_id,
                "as_of_date": as_of.isoformat(),
                "source": source,
                "items": [item.model_dump(mode="json") for item in items],
                "total": len(items),
            }

    @mcp.tool
    async def write_investment_constituent_snapshot(
        workspace_id: int,
        instrument_public_id: str,
        as_of_date: str,
        source: str,
        fetched_at: str,
        constituents: list[dict[str, Any]],
        renormalise: bool = False,
        overwrite_existing: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Replace one sourced ETF or mutual-fund constituent snapshot.

        Requires the dedicated mcp:research grant and explicit confirmation.
        Each constituent must include a ticker or ISIN and a decimal weight,
        and may optionally carry ``sector`` and ``industry`` (free text, up to
        100 characters each; GICS sector names are recommended). A provided
        sector/industry fills an empty value on the constituent company; an
        already-set value is overwritten only when ``overwrite_existing`` is
        true.
        """
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    "This will replace the complete constituent snapshot for "
                    f"{instrument_public_id} on {as_of_date} from {source}. Confirm to continue."
                ),
                "instrument_public_id": instrument_public_id,
                "as_of_date": as_of_date,
                "source": source,
                "constituent_count": len(constituents),
            }
        if not 1 <= len(constituents) <= 500:
            return {
                "status": "error",
                "message": "constituents must contain between 1 and 500 rows.",
            }

        try:
            instrument_id = uuid.UUID(instrument_public_id)
            payload = InstrumentConstituentUpsert.model_validate({
                "as_of_date": as_of_date,
                "source": source,
                "fetched_at": fetched_at,
                "constituents": constituents,
                "renormalise": renormalise,
                "overwrite_existing": overwrite_existing,
            })
        except (TypeError, ValueError, PydanticValidationError) as exc:
            return {"status": "error", "message": f"Invalid constituent snapshot: {exc}"}

        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:research",
                tool="write_investment_constituent_snapshot",
            )
            try:
                service = services.constituent_service(session)
                await service.upsert_constituents(workspace_id, instrument_id, payload)
                items = await service.get_constituents(
                    workspace_id,
                    instrument_id,
                    payload.as_of_date,
                    source=payload.source,
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                "entity_type": "investment_constituent_snapshot",
                "instrument_public_id": instrument_public_id,
                "as_of_date": payload.as_of_date.isoformat(),
                "source": payload.source,
                "fetched_at": payload.fetched_at.isoformat(),
                "items": [item.model_dump(mode="json") for item in items],
                "total": len(items),
                "summary": f"Saved {len(items)} constituent rows from {payload.source}.",
            }

    @mcp.tool
    async def delete_investment_constituent_snapshot(
        workspace_id: int,
        instrument_public_id: str,
        as_of_date: str,
        source: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Delete one complete sourced constituent snapshot after confirmation."""
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    "This will delete the complete constituent snapshot for "
                    f"{instrument_public_id} on {as_of_date} from {source}. Confirm to continue."
                ),
            }
        try:
            instrument_id = uuid.UUID(instrument_public_id)
            snapshot_date = date.fromisoformat(as_of_date)
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid instrument_public_id or as_of_date."}

        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:research",
                tool="delete_investment_constituent_snapshot",
            )
            try:
                deleted = await services.constituent_service(session).delete_snapshot(
                    workspace_id, instrument_id, snapshot_date, source
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                "entity_type": "investment_constituent_snapshot",
                "instrument_public_id": instrument_public_id,
                "as_of_date": as_of_date,
                "source": source,
                "deleted_rows": deleted,
                "summary": f"Deleted the {source} constituent snapshot.",
            }

    @mcp.tool
    async def set_company_sector(
        workspace_id: int,
        rows: list[dict[str, Any]],
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Set the sector (and optional industry and country) on companies, in bulk.

        ``rows`` holds 1-200 entries. Each row identifies its company by
        exactly one of:

        - ``holding_public_id`` - a holding in this workspace; the company
          behind that holding's instrument is updated.
        - ``isin`` - a company matched by ISIN, workspace-scoped or global.
        - ``ticker`` plus ``exchange`` - a company via an instrument listed
          on that exchange, workspace-scoped or global.

        ``sector`` is required on every row (free text, up to 100 characters).
        Use GICS names: Information Technology, Health Care, Financials,
        Industrials, Energy, Consumer Staples, Consumer Discretionary,
        Materials, Utilities, Communication Services, Real Estate. Case and
        extra whitespace are normalised (``health  care`` -> ``Health Care``)
        so variants never split an allocation bucket. ``industry`` (up to 100
        characters) and ``country_code`` are optional and only written when
        provided. ETFs and mutual funds have no company; their sectors come
        from constituent snapshots.

        A global company is never edited in place: it is forked into a
        workspace-scoped copy and this workspace's instruments and holdings
        are re-pointed to it (idempotent on re-run). Without ``confirmed`` the
        call returns ``needs_confirmation`` and writes nothing. With it, each
        row is applied atomically; a row that cannot be applied is reported in
        ``errors`` while the other rows still apply, and an unexpected failure
        rolls back the whole call. Returns per-row ``before``/``after`` values.
        Requires the mcp:research scope.
        """
        if not 1 <= len(rows) <= 200:
            return {
                "status": "error",
                "message": "rows must contain between 1 and 200 entries.",
            }
        parsed: list[CompanySectorRow] = []
        for idx, raw in enumerate(rows):
            try:
                parsed.append(CompanySectorRow.model_validate(raw))
            except PydanticValidationError as exc:
                detail = "; ".join(
                    str(err["msg"]).removeprefix("Value error, ") for err in exc.errors()
                )
                return {"status": "error", "message": f"row {idx}: {detail}"}

        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:research",
                tool="set_company_sector",
            )
            if not confirmed:
                return {
                    "status": "needs_confirmation",
                    "needs_confirmation": True,
                    "message": (
                        f"This will set the sector on {len(parsed)} company(ies) in "
                        f"workspace {workspace_id}. Confirm to continue."
                    ),
                    "row_count": len(parsed),
                    "rows": [
                        {
                            "row_index": idx,
                            "requested_sector": row.sector,
                            "requested_industry": row.industry,
                            "requested_country_code": row.country_code,
                        }
                        for idx, row in enumerate(parsed)
                    ],
                }
            try:
                outcome = await services.company_sector_service(session).set_sectors(
                    workspace_id, parsed
                )
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("set_company_sector failed; no changes were saved")
                return {
                    "status": "error",
                    "message": "set_company_sector failed unexpectedly; no changes were saved.",
                }
        updated, errors = outcome["updated"], outcome["errors"]
        return {
            "status": "success",
            "entity_type": "company_sector",
            "workspace_id": workspace_id,
            "updated": updated,
            "errors": errors,
            "summary": (
                f"Set sector on {len(updated)} company(ies)"
                + (f"; {len(errors)} row(s) failed" if errors else "")
                + "."
            ),
        }

    @mcp.tool
    async def set_company_details(
        workspace_id: int,
        company_public_id: str,
        name: str | None = None,
        isin: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Rename a company and/or set its ISIN, workspace-scoped.

        Edits one company visible in this workspace (global or a workspace
        copy). A global company is forked into a workspace copy first, so the
        global record is never mutated in place. At least one of ``name`` or
        ``isin`` must be provided. An ISIN already used by another company in
        this workspace is rejected; a name may shadow a global record's name
        (the workspace copy takes precedence for this workspace).

        Without ``confirmed`` the call returns ``needs_confirmation`` and
        writes nothing. With it, the change is applied in a transaction and
        recorded in the audit trail (before/after plus changed fields).
        Requires the mcp:write scope.
        """
        if name is None and isin is None:
            return {
                "status": "error",
                "message": "provide at least one of name or isin",
            }
        try:
            public_id = uuid.UUID(company_public_id)
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid company_public_id."}

        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:write",
                tool="set_company_details",
            )
            if not confirmed:
                return {
                    "status": "needs_confirmation",
                    "needs_confirmation": True,
                    "message": (
                        f"This will rename and/or set the ISIN of company "
                        f"{company_public_id} in workspace {workspace_id}. "
                        "Confirm to continue."
                    ),
                    "company_public_id": company_public_id,
                    "requested_name": name,
                    "requested_isin": isin,
                }
            try:
                outcome = await services.company_sector_service(session).set_company_details(
                    workspace_id,
                    public_id,
                    name=name,
                    isin=isin,
                )
                await AuditLogger(session).log(
                    workspace_id=workspace_id,
                    actor_id=user_id,
                    action="update",
                    module="investing",
                    entity_type="company",
                    entity_id=outcome["company_id"],
                    details={
                        "entity_public_id": str(public_id),
                        "before": outcome["before"],
                        "after": outcome["after"],
                        "changed_fields": [
                            k
                            for k in ("name", "isin")
                            if outcome["before"][k] != outcome["after"][k]
                        ],
                    },
                )
                await session.commit()
            except (APIError, ValueError, SectorRowError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
        return {
            "status": "success",
            "entity_type": "company",
            "entity_public_id": company_public_id,
            "workspace_id": workspace_id,
            "forked_from_global": outcome["forked_from_global"],
            "changed": outcome["changed"],
            "before": outcome["before"],
            "after": outcome["after"],
            "summary": (
                f"Updated company {company_public_id}"
                + (", forked from the global record" if outcome["forked_from_global"] else "")
                + (
                    "; changed: "
                    + ", ".join(
                        k for k in ("name", "isin") if outcome["before"][k] != outcome["after"][k]
                    )
                    if outcome["changed"]
                    else ""
                )
                + "."
            ),
        }

    @mcp.tool
    async def merge_companies(
        workspace_id: int,
        keep_company_public_id: str,
        drop_company_public_id: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Merge one duplicate company into another inside this workspace.

        Re-points this workspace's instruments and constituent snapshot rows
        from the drop company to the keep company (summing weights when both
        companies already carry a row for one instrument snapshot), copies
        ISIN/ticker/sector onto the keep company when empty, then retires the
        drop company. Returns before/after snapshots. Both companies must be
        visible in the workspace; global companies are never merged or retired
        by this tool.

        Without ``confirmed`` the call returns ``needs_confirmation`` and
        writes nothing. The change is recorded in the audit trail with
        before/after values. Requires the mcp:write scope.
        """
        try:
            keep_id = uuid.UUID(keep_company_public_id)
            drop_id = uuid.UUID(drop_company_public_id)
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid company_public_id."}

        if keep_id == drop_id:
            return {
                "status": "error",
                "message": "keep and drop must be different companies.",
            }

        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:write",
                tool="merge_companies",
            )

            service = CompanyMergeService(
                session,
                CompanyRepository(session),
                InstrumentRepository(session),
                InstrumentConstituentRepository(session),
            )
            keep = await service.company_repo.get_by_public_id(workspace_id, keep_id)
            drop = await service.company_repo.get_by_public_id(workspace_id, drop_id)
            preview: dict[str, Any] = {
                "keep": {
                    "company_public_id": keep_company_public_id,
                    "name": keep.name if keep else None,
                    "ticker": keep.ticker if keep else None,
                    "isin": keep.isin if keep else None,
                    "sector": keep.sector if keep else None,
                },
                "drop": {
                    "company_public_id": drop_company_public_id,
                    "name": drop.name if drop else None,
                    "ticker": drop.ticker if drop else None,
                    "isin": drop.isin if drop else None,
                    "sector": drop.sector if drop else None,
                },
            }
            if keep is not None and drop is not None and keep.workspace_id == workspace_id:
                instruments = await service.instrument_repo.get_by_company(workspace_id, drop.id)
                preview["instruments_repointed"] = len(instruments)
                result = await session.execute(
                    select(InstrumentConstituent)
                    .join(Instrument, Instrument.id == InstrumentConstituent.instrument_id)
                    .where(
                        Instrument.workspace_id == workspace_id,
                        InstrumentConstituent.constituent_company_id == drop.id,
                    )
                )
                preview["constituent_rows_repointed"] = len(list(result.scalars().all()))
            else:
                preview["error"] = (
                    "both companies must be visible in this workspace"
                    if keep is not None and drop is not None
                    else "company not found in this workspace"
                )

            if not confirmed:
                return {
                    "status": "needs_confirmation",
                    "needs_confirmation": True,
                    "message": (
                        f"Merge company {drop_company_public_id} into "
                        f"{keep_company_public_id} in workspace {workspace_id}? "
                        "Confirm to continue."
                    ),
                    "workspace_id": workspace_id,
                    "keep_company_public_id": keep_company_public_id,
                    "drop_company_public_id": drop_company_public_id,
                    **preview,
                }

            if "error" in preview:
                return {"status": "error", "message": preview["error"]}

            try:
                outcome = await service.merge_companies(
                    workspace_id, keep_id, drop_id, actor_id=user_id
                )
                await AuditLogger(session).log(
                    workspace_id=workspace_id,
                    actor_id=user_id,
                    action="merge",
                    module="investing",
                    entity_type="company",
                    entity_id=outcome["company_id"],
                    details={
                        "entity_public_id": str(keep_id),
                        "before": outcome["before"],
                        "after": outcome["after"],
                        "changed_fields": outcome["changed_fields"],
                        "summary": outcome["summary"],
                    },
                )
                await session.commit()
            except (APIError, ValueError, MergeConflictError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
        return {
            "status": "success",
            "entity_type": "company",
            "workspace_id": workspace_id,
            "keep_company_public_id": keep_company_public_id,
            "drop_company_public_id": drop_company_public_id,
            "changed": outcome["changed"],
            "before": outcome["before"],
            "after": outcome["after"],
            "summary": outcome["summary"],
            "summary_text": (
                f"Merged {drop_company_public_id} into {keep_company_public_id}: "
                f"{outcome['summary']['instruments_repointed']} instrument(s) re-pointed, "
                f"{outcome['summary']['constituent_rows_repointed']} constituent row(s) re-pointed, "
                f"{outcome['summary']['constituent_weights_summed']} weight(s) summed, "
                "drop retired."
            ),
        }

    @mcp.tool
    async def get_investment_sector_allocation(
        workspace_id: int,
        as_of_date: str | None = None,
        lookthrough: bool = True,
    ) -> dict[str, Any]:
        """Sector allocation of the portfolio, with optional ETF/MF look-through.

        With look-through (default) each ETF/MF holding is split across the
        sectors of the companies in its latest constituent snapshot (exactly
        one snapshot per instrument, newest by ``(as_of_date, fetched_at)``);
        direct stocks use their own company's sector; ``OTHER-<FUND>``
        placeholder holdings and unmapped companies go to ``Other / not
        itemised`` and are never rescaled. Returns value and % per sector
        (summing to 100% including that bucket) plus coverage per fund.
        """
        try:
            as_of = date.fromisoformat(as_of_date) if as_of_date else datetime.now(UTC).date()
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid as_of_date. Use YYYY-MM-DD."}

        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="get_investment_sector_allocation",
            )
            result = await services.analytics_service(session).sector_allocation(
                workspace_id, as_of, lookthrough=lookthrough
            )
            return {
                "status": "success",
                "workspace_id": workspace_id,
                "as_of_date": result["as_of_date"],
                "currency": result["currency"],
                "lookthrough": result["lookthrough"],
                "total_portfolio_value": result["total_portfolio_value"],
                "holdings_value": result["holdings_value"],
                "sectors": result["sectors"],
                "fund_coverage": result["fund_coverage"],
                "warnings": result["warnings"],
                "fx_as_of": result["fx_as_of"],
                "fx_rates_used": result["fx_rates_used"],
            }

    @mcp.tool
    async def set_holding_compliance(
        workspace_id: int,
        holding_id: str,
        verdict: str,
        sources: list[dict[str, Any]],
        as_of_date: str,
        impure_income_pct: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Record (create or replace) the halal compliance verdict for a holding.

        Requires the dedicated mcp:research grant and explicit confirmation.
        Idempotent: the latest call wins and the previous value stays in the
        audit log.

        `verdict` is one of "halal", "non_halal", "permissible" (not fully
        halal but acceptable once income is purified) or "unknown".
        `sources` is 1-10 citations, each {"screener_name", "url" (http/https),
        "rule_reference"} plus optional "verdict" and "as_of_date" (YYYY-MM-DD)
        so screeners that disagree stay visible side by side. `as_of_date` is
        the date of the verdict (not in the future). `impure_income_pct` is the
        screener-stated share (0-100, decimal string) of non-permissible income;
        it drives get_purification_estimate and is never inferred.
        """
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    f"Record compliance verdict '{verdict}' (as of {as_of_date}, "
                    f"{len(sources)} source(s)) for holding {holding_id}, replacing any "
                    "existing record? Confirm to continue."
                ),
                "holding_id": holding_id,
                "verdict": verdict,
                "as_of_date": as_of_date,
                "impure_income_pct": impure_income_pct,
                "source_count": len(sources),
            }
        try:
            holding_public_id = uuid.UUID(holding_id)
            payload = HoldingComplianceUpsert.model_validate({
                "verdict": verdict,
                "sources": sources,
                "as_of_date": as_of_date,
                "impure_income_pct": impure_income_pct,
            })
        except (TypeError, ValueError, PydanticValidationError) as exc:
            return {"status": "error", "message": f"Invalid compliance record: {exc}"}

        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:research",
                tool="set_holding_compliance",
            )
            try:
                compliance = await services.compliance_service(session).set_compliance(
                    workspace_id,
                    holding_public_id,
                    payload,
                    actor_id=user_id,
                    audit_logger=AuditLogger(session),
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                "entity_type": "holding_compliance",
                "compliance": compliance.model_dump(mode="json"),
                "summary": f"Saved '{compliance.verdict}' verdict with {len(payload.sources)} source(s).",
            }

    @mcp.tool
    async def get_holding_compliance(workspace_id: int, holding_id: str) -> dict[str, Any]:
        """Get the stored halal compliance verdict for a holding.

        Returns the verdict, its date, the non-permissible income percentage
        and every cited source (screener name, URL, rule reference, and each
        screener's own verdict/date). `sources_disagree` is true when the
        cited screeners gave different verdicts. When the holding has no
        record yet, `compliance` is null.
        """
        try:
            holding_public_id = uuid.UUID(holding_id)
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid holding_id."}
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="get_holding_compliance",
            )
            try:
                compliance = await services.compliance_service(session).get_compliance(
                    workspace_id, holding_public_id
                )
            except (APIError, ValueError) as exc:
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                "workspace_id": workspace_id,
                "holding_id": holding_id,
                "compliance": compliance.model_dump(mode="json") if compliance else None,
                "message": (None if compliance else "No compliance record for this holding yet."),
            }

    @mcp.tool
    async def get_purification_estimate(
        workspace_id: int,
        holding_id: str | None = None,
        dividend_id: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Estimate the halal purification (charity) amount for income.

        Pass exactly one of `holding_id` (all income recorded against that
        holding, optionally limited by `from_date`/`to_date` as YYYY-MM-DD)
        or `dividend_id` (one income event). Amount = gross income x the
        holding's recorded non-permissible-income %, as a decimal string in
        the holding's currency. If no percentage is recorded the amount is
        "0.00" and `basis` says why; nothing is guessed. Estimate only, not
        a fatwa: see the `disclaimer` field.
        """
        try:
            holding_public_id = uuid.UUID(holding_id) if holding_id else None
            dividend_public_id = uuid.UUID(dividend_id) if dividend_id else None
            start = date.fromisoformat(from_date) if from_date else None
            end = date.fromisoformat(to_date) if to_date else None
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {
                "status": "error",
                "message": "Invalid holding_id, dividend_id, from_date or to_date.",
            }
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="get_purification_estimate",
            )
            try:
                estimate = await services.compliance_service(session).get_purification_estimate(
                    workspace_id,
                    holding_public_id=holding_public_id,
                    dividend_public_id=dividend_public_id,
                    from_date=start,
                    to_date=end,
                )
            except (APIError, ValueError) as exc:
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                "workspace_id": workspace_id,
                "estimate": estimate.model_dump(mode="json"),
            }

    @mcp.tool
    async def set_holding_theme_tags(
        workspace_id: int,
        holdings: list[dict[str, Any]],
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Set theme tags on holdings.

        Requires mcp:research scope and explicit confirmation.
        Each holding entry: {"holding_id": str, "tags": [{"theme": str, "target_weight_pct": float?}]}
        """
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": f"Set theme tags on {len(holdings)} holdings? Confirm to continue.",
                "holdings": holdings,
            }
        if not 1 <= len(holdings) <= 200:
            return {
                "status": "error",
                "message": "holdings must contain between 1 and 200 entries.",
            }

        validated = []
        for i, h in enumerate(holdings):
            try:
                holding_public_id = uuid.UUID(h["holding_id"])
                tag_list = h.get("tags", [])
                if not isinstance(tag_list, list) or len(tag_list) > 20:
                    return {
                        "status": "error",
                        "message": f"holding {i}: tags must be a list of 0-20 items.",
                    }
                tags = []
                for t in tag_list:
                    tag = str(t["theme"]).strip()
                    if not tag or len(tag) > 64:
                        return {
                            "status": "error",
                            "message": f"holding {i}: theme must be 1-64 chars.",
                        }
                    tw = t.get("target_weight_pct")
                    if tw is not None:
                        tw = float(tw)
                        if tw < 0 or tw > 100:
                            return {
                                "status": "error",
                                "message": f"holding {i}: target_weight_pct must be 0-100.",
                            }
                    tags.append(ThemeTagInput(theme=tag, target_weight_pct=tw))
                validated.append(HoldingThemeTagsInput(holding_id=holding_public_id, tags=tags))
            except (KeyError, TypeError, ValueError) as exc:
                return {"status": "error", "message": f"holding {i}: invalid entry: {exc}"}

        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:research", tool="set_holding_theme_tags"
            )
            try:
                result = await services.theme_tag_service(session).set_tags(workspace_id, validated)
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                **result.model_dump(mode="json"),
                "summary": f"Updated theme tags on {len(validated)} holding(s).",
            }

    @mcp.tool
    async def get_lookthrough_exposure(
        workspace_id: int,
        as_of_date: str | None = None,
        group_by: GroupBy | None = None,
    ) -> dict[str, Any]:
        """Get look-through exposure by theme/company/sector/country.

        Omit ``group_by`` for every grouping, or pass one of company, sector,
        country, theme to limit the response to it.

        Uses exactly one latest constituent snapshot per fund (newest by as_of_date, fetched_at).
        OTHER-* placeholder rows and unmapped companies go to "not itemised" (never rescaled).
        """
        try:
            as_of = date.fromisoformat(as_of_date) if as_of_date else datetime.now(UTC).date()
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid as_of_date. Use YYYY-MM-DD."}

        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_lookthrough_exposure"
            )
            try:
                result = await services.theme_exposure_service(session).lookthrough_exposure(
                    workspace_id, as_of, group_by=group_by
                )
            except PydanticValidationError as exc:
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                "workspace_id": workspace_id,
                **result.model_dump(mode="json"),
            }

    @mcp.tool
    async def get_allocation_vs_target(
        workspace_id: int,
        new_cash: str = "0",
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        """Allocation vs target with optional new-cash deployment.

        ``new_cash`` defaults to 0. If positive, computes buy-only deployment to
        underweight themes.
        Never suggests sells. Largest-remainder rounding ensures exact sums.
        """
        try:
            as_of = date.fromisoformat(as_of_date) if as_of_date else datetime.now(UTC).date()
            cash = Decimal(new_cash)
        except (
            TypeError,
            ValueError,
            decimal.InvalidOperation,
            PydanticValidationError,
            AppValidationError,
        ):
            return {"status": "error", "message": "Invalid as_of_date or new_cash."}

        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_allocation_vs_target"
            )
            try:
                result = await services.theme_exposure_service(session).allocation_vs_target(
                    workspace_id, as_of, new_cash=cash
                )
                return {
                    "status": "success",
                    "workspace_id": workspace_id,
                    **result.model_dump(mode="json"),
                }
            except AppValidationError as exc:
                return {"status": "error", "message": exc.detail}
