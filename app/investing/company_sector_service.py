"""Bulk company sector writes (spec-100, ``set_company_sector``).

Transaction semantics (decided and tested):

* Each row runs inside its own SAVEPOINT. A *domain* failure (company not
  found, ambiguous ticker, ...) raises :class:`SectorRowError`; that row's
  partial writes (e.g. a half-finished fork) are rolled back to the savepoint
  and the row is reported in ``errors`` while the other rows still apply.
  This matches spec-100 ("per-row before/after plus skipped rows with
  reasons").
* Any *unexpected* exception (DB error, bug) is not swallowed: it propagates,
  the caller's transaction is rolled back and **nothing** from the call is
  committed, so a half-applied bulk write can never be committed silently.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.investing.models import Company, Instrument
from app.investing.repository import (
    CompanyRepository,
    HoldingRepository,
    InstrumentRepository,
)
from app.investing.schemas import CompanySectorRow


class SectorRowError(Exception):
    """A row could not be applied for a reason the caller can act on."""


def _company_values(company: Company) -> dict[str, str | None]:
    return {
        "sector": company.sector,
        "industry": company.industry,
        "country_code": company.country_code,
    }


class CompanySectorService:
    def __init__(
        self,
        holding_repo: HoldingRepository,
        instrument_repo: InstrumentRepository,
        company_repo: CompanyRepository,
    ):
        self.holding_repo = holding_repo
        self.instrument_repo = instrument_repo
        self.company_repo = company_repo

    async def set_sectors(
        self, workspace_id: int, rows: Sequence[CompanySectorRow]
    ) -> dict[str, list[dict[str, Any]]]:
        """Apply ``rows``; return ``{"updated": [...], "errors": [...]}``."""
        updated: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            try:
                async with self.company_repo.session.begin_nested():
                    updated.append(await self._apply_row(workspace_id, index, row))
            except SectorRowError as exc:
                errors.append({"row_index": index, "message": str(exc)})
        return {"updated": updated, "errors": errors}

    async def _apply_row(
        self, workspace_id: int, index: int, row: CompanySectorRow
    ) -> dict[str, Any]:
        company, identified_via = await self._resolve_company(workspace_id, row)
        forked = company.workspace_id is None
        if company.workspace_id not in (None, workspace_id):
            raise SectorRowError("company is not visible in this workspace")
        try:
            company = await self.company_repo.fork_for_workspace(workspace_id, company)
        except ValueError as exc:
            raise SectorRowError(str(exc)) from exc

        before = _company_values(company)
        company.sector = row.sector
        if row.industry is not None:
            company.industry = row.industry
        if row.country_code is not None:
            company.country_code = row.country_code
        after = _company_values(company)
        changed = before != after
        if changed:
            company.updated_at = datetime.now(UTC)
            await self.company_repo.save(company)
        return {
            "row_index": index,
            "identified_via": identified_via,
            "company_public_id": str(company.public_id),
            "company_name": company.name,
            "company_ticker": company.ticker,
            "company_isin": company.isin,
            "forked_from_global": forked,
            "changed": changed,
            "before": before,
            "after": after,
        }

    async def _resolve_company(
        self, workspace_id: int, row: CompanySectorRow
    ) -> tuple[Company, str]:
        if row.holding_public_id is not None:
            return await self._company_for_holding(workspace_id, row.holding_public_id), "holding"
        if row.isin is not None:
            company = await self.company_repo.get_by_isin(workspace_id, row.isin)
            if company is None:
                raise SectorRowError(f"no company with ISIN '{row.isin}' visible in this workspace")
            return company, "isin"
        assert row.ticker is not None and row.exchange is not None
        return await self._company_for_ticker(workspace_id, row.ticker, row.exchange), (
            "ticker+exchange"
        )

    async def _company_for_holding(self, workspace_id: int, public_id: uuid.UUID) -> Company:
        holding = await self.holding_repo.get_by_public_id(workspace_id, public_id)
        if holding is None:
            raise SectorRowError(f"holding '{public_id}' not found in this workspace")
        instrument: Instrument | None = None
        if holding.instrument_id is not None:
            instruments = await self.instrument_repo.get_by_ids([holding.instrument_id])
            instrument = instruments.get(holding.instrument_id)
        if instrument is None:
            instrument = await self.instrument_repo.get_by_symbol(workspace_id, holding.symbol)
        if instrument is None:
            raise SectorRowError("holding has no linked instrument")
        if instrument.company_id is None:
            raise SectorRowError(
                "holding's instrument is not linked to a company (ETFs and mutual funds "
                "have no company sector; their look-through comes from constituent snapshots)"
            )
        company = await self.company_repo.get_by_id(instrument.company_id)
        if company is None:
            raise SectorRowError("company not found")
        return company

    async def _company_for_ticker(self, workspace_id: int, ticker: str, exchange: str) -> Company:
        instruments = list(
            await self.instrument_repo.get_instruments_by_symbol_exchange(
                workspace_id, ticker, exchange
            )
        )
        if not instruments:
            raise SectorRowError(
                f"no instrument '{ticker}' on '{exchange}' visible in this workspace"
            )
        # A workspace instrument shadows the global one with the same symbol.
        workspace_scoped = [i for i in instruments if i.workspace_id == workspace_id]
        instruments = workspace_scoped or instruments
        companies = await self.company_repo.get_by_ids([
            i.company_id for i in instruments if i.company_id is not None
        ])
        if not companies:
            raise SectorRowError(f"'{ticker}' on '{exchange}' is not linked to any company")
        if len(companies) > 1:
            raise SectorRowError(
                f"'{ticker}' on '{exchange}' is ambiguous: it resolves to {len(companies)} companies"
            )
        return next(iter(companies.values()))
