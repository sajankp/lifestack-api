"""Workspace-scoped, user-driven merge of two duplicate ``Company`` rows
(spec-083 §9).

``merge_companies`` is the deliberate, confirmed, audit-logged counterpart of
the CLI backfill ``merge_company_identities``
(app/application/company_identity_merge.py): that job groups duplicates
automatically by identity key and never runs automatically; this tool merges
one operator-chosen pair at a time within a single workspace, re-points every
row that references the dropped company at the kept company, and retires the
drop.

Everything it touches is workspace-scoped: workspace instruments, this
workspace's constituent snapshot rows, and workspace companies. Global rows are
never mutated or retired by this tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.investing.models import Company, Instrument, InstrumentConstituent
from app.investing.repository import (
    CompanyRepository,
    InstrumentConstituentRepository,
    InstrumentRepository,
)

_WEIGHT_QUANT = Decimal("0.00000001")


class MergeConflictError(ValueError):
    """The merge cannot proceed for a reason the caller can act on."""


def _company_snapshot(company: Company) -> dict[str, Any]:
    return {
        "company_public_id": str(company.public_id),
        "name": company.name,
        "ticker": company.ticker,
        "isin": company.isin,
        "sector": company.sector,
        "industry": company.industry,
        "country_code": company.country_code,
    }


@dataclass
class CompanyMergeSummary:
    workspace_id: int
    keep_company_public_id: UUID
    drop_company_public_id: UUID
    keep_before: dict[str, Any]
    drop_before: dict[str, Any]
    keep_after: dict[str, Any]
    instruments_repointed: int = 0
    constituent_rows_repointed: int = 0
    constituent_weights_summed: int = 0
    drop_retired: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "keep_company_public_id": str(self.keep_company_public_id),
            "drop_company_public_id": str(self.drop_company_public_id),
            "instruments_repointed": self.instruments_repointed,
            "constituent_rows_repointed": self.constituent_rows_repointed,
            "constituent_weights_summed": self.constituent_weights_summed,
            "drop_retired": self.drop_retired,
        }


class CompanyMergeService:
    def __init__(
        self,
        session: Any,
        company_repo: CompanyRepository,
        instrument_repo: InstrumentRepository,
        constituent_repo: InstrumentConstituentRepository,
    ) -> None:
        self.session = session
        self.company_repo = company_repo
        self.instrument_repo = instrument_repo
        self.constituent_repo = constituent_repo

    async def _load_workspace_company(self, workspace_id: int, public_id: UUID) -> Company:
        company = await self.company_repo.get_by_public_id(workspace_id, public_id)
        if company is None:
            raise MergeConflictError(f"company '{public_id}' not found in this workspace")
        if company.workspace_id != workspace_id:
            raise MergeConflictError(
                "merge_companies is workspace-scoped: global companies cannot be merged or retired here"
            )
        assert company.id is not None
        return company

    async def merge_companies(
        self,
        workspace_id: int,
        keep_public_id: UUID,
        drop_public_id: UUID,
        *,
        actor_id: int,
    ) -> dict[str, Any]:
        """Merge ``drop_public_id`` into ``keep_public_id`` within one workspace.

        Re-points workspace instruments and this workspace's constituent
        snapshot rows from the drop company to the keep company (summing
        weights when both companies already carry a row for one instrument
        snapshot), copies ISIN/ticker/sector onto the keep company when empty,
        then retires (deletes) the drop company. Returns before/after snapshots
        plus counts. Nothing is committed here; the caller audits and commits.
        """
        keep = await self._load_workspace_company(workspace_id, keep_public_id)
        drop = await self._load_workspace_company(workspace_id, drop_public_id)
        if keep.id == drop.id:
            raise MergeConflictError("keep and drop must be different companies")

        before = {
            "keep": _company_snapshot(keep),
            "drop": _company_snapshot(drop),
        }

        changed_fields: list[str] = []
        if not keep.isin and drop.isin:
            # Any company other than keep/drop already carrying the drop's ISIN
            # blocks the merge (the ISIN moves with the drop into keep).
            result = await self.session.execute(
                select(Company).where(
                    Company.isin == drop.isin, Company.workspace_id == workspace_id
                )
            )
            clashers = [c for c in result.scalars().all() if c.id not in (keep.id, drop.id)]
            if clashers:
                raise MergeConflictError(
                    f"ISIN '{drop.isin}' is already used by '{clashers[0].name}'"
                )
            keep.isin = drop.isin
            changed_fields.append("isin")
        if not keep.ticker and drop.ticker:
            keep.ticker = drop.ticker
            changed_fields.append("ticker")
        if not keep.sector and drop.sector:
            keep.sector = drop.sector
            changed_fields.append("sector")
        if changed_fields:
            keep.updated_at = datetime.now(UTC)
            self.session.add(keep)

        # Re-point workspace instruments linked to the drop company.
        instruments = await self.instrument_repo.get_by_company(workspace_id, drop.id)
        for instrument in instruments:
            instrument.company_id = keep.id
            instrument.updated_at = datetime.now(UTC)
            self.session.add(instrument)
        instruments_repointed = len(instruments)

        # Re-point this workspace's constituent snapshot rows from drop to keep.
        # An instrument may already point at the keep company while a
        # constituent row of it still references the drop company (the exact
        # production state this tool repairs), so we key off the instrument's
        # workspace, not the instrument's current company.
        drop_result = await self.session.execute(
            select(InstrumentConstituent)
            .join(Instrument, Instrument.id == InstrumentConstituent.instrument_id)
            .where(
                Instrument.workspace_id == workspace_id,
                InstrumentConstituent.constituent_company_id == drop.id,
            )
        )
        drop_rows = list(drop_result.scalars().all())

        keep_result = await self.session.execute(
            select(InstrumentConstituent)
            .join(Instrument, Instrument.id == InstrumentConstituent.instrument_id)
            .where(
                Instrument.workspace_id == workspace_id,
                InstrumentConstituent.constituent_company_id == keep.id,
            )
        )
        keep_by_key: dict[tuple[int, str, str], InstrumentConstituent] = {}
        for row in keep_result.scalars().all():
            keep_by_key[(row.instrument_id, row.as_of_date.isoformat(), row.source)] = row

        constituent_rows_repointed = 0
        constituent_weights_summed = 0
        # Collisions first: drop rows whose snapshot key already exists on the
        # keep side are summed into the keep row and deleted, so the
        # uq_investing_constituent_snapshot constraint is never violated.
        for row in drop_rows:
            key = (row.instrument_id, row.as_of_date.isoformat(), row.source)
            keep_row = keep_by_key.get(key)
            assert row.instrument_id is not None
            assert row.constituent_company_id is not None
            if keep_row is not None:
                assert keep_row.weight is not None and row.weight is not None
                keep_row.weight = (keep_row.weight + row.weight).quantize(_WEIGHT_QUANT)
                self.session.add(keep_row)
                await self.session.delete(row)
                constituent_weights_summed += 1
            else:
                row.constituent_company_id = keep.id
                self.session.add(row)
                constituent_rows_repointed += 1

        # Flush repoints/deletes before the company DELETE so no FK constraint
        # on Instrument.company_id / InstrumentConstituent.constituent_company_id
        # is violated by the retiring row.
        await self.session.flush()

        await self.session.delete(drop)
        await self.session.flush()

        after = {
            "keep": _company_snapshot(keep),
            "drop": {**_company_snapshot(drop), "retired": True},
        }
        return {
            "company_id": keep.id,
            "company_public_id": str(keep.public_id),
            "changed": bool(changed_fields),
            "before": before,
            "after": after,
            "changed_fields": changed_fields,
            "summary": CompanyMergeSummary(
                workspace_id=workspace_id,
                keep_company_public_id=keep_public_id,
                drop_company_public_id=drop_public_id,
                keep_before=before["keep"],
                drop_before=before["drop"],
                keep_after=after["keep"],
                instruments_repointed=instruments_repointed,
                constituent_rows_repointed=constituent_rows_repointed,
                constituent_weights_summed=constituent_weights_summed,
                drop_retired=True,
            ).as_dict(),
        }
