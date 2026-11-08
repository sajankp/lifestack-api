"""Persistence for holding theme tags and single-snapshot constituent lookups.

Every query here is workspace-scoped and batch-oriented (no per-row queries).
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.repository import BaseRepository
from app.investing.models import Holding, HoldingThemeTag, InstrumentConstituent


class HoldingThemeTagRepository(BaseRepository[HoldingThemeTag]):
    """Theme tags on holdings, always filtered by workspace."""

    async def holdings_by_public_ids(
        self, workspace_id: int, public_ids: Sequence[UUID]
    ) -> dict[UUID, Holding]:
        """Holdings in *workspace_id* matching *public_ids* (others are omitted)."""
        unique_ids = set(public_ids)
        if not unique_ids:
            return {}
        result = await self.session.execute(
            select(Holding).where(
                Holding.workspace_id == workspace_id, Holding.public_id.in_(unique_ids)
            )
        )
        return {holding.public_id: holding for holding in result.scalars().all()}

    async def list_for_workspace(self, workspace_id: int) -> list[tuple[HoldingThemeTag, Holding]]:
        """Every tag in the workspace with its holding (one query)."""
        result = await self.session.execute(
            select(HoldingThemeTag, Holding)
            .join(Holding, HoldingThemeTag.holding_id == Holding.id)
            .where(
                HoldingThemeTag.workspace_id == workspace_id,
                Holding.workspace_id == workspace_id,
            )
            .order_by(HoldingThemeTag.theme.asc(), HoldingThemeTag.id.asc())
        )
        return [(tag, holding) for tag, holding in result.all()]

    async def list_for_holdings(
        self, workspace_id: int, holding_ids: Sequence[int]
    ) -> list[HoldingThemeTag]:
        """Tags on the given internal holding ids, scoped to the workspace."""
        ids = set(holding_ids)
        if not ids:
            return []
        result = await self.session.execute(
            select(HoldingThemeTag)
            .where(
                HoldingThemeTag.workspace_id == workspace_id,
                HoldingThemeTag.holding_id.in_(ids),
            )
            .order_by(HoldingThemeTag.theme.asc())
        )
        return list(result.scalars().all())

    async def replace_for_holding(
        self,
        workspace_id: int,
        holding_id: int,
        tags: Sequence[tuple[str, Decimal | None]],
    ) -> list[HoldingThemeTag]:
        """Make the holding carry exactly *tags* (theme, target) - idempotent.

        Existing rows for a kept theme are updated in place (stable public id),
        removed themes are deleted and new themes inserted. Callers must have
        verified that the holding belongs to *workspace_id* and that themes are
        unique within *tags*.
        """
        existing_rows = await self.list_for_holdings(workspace_id, [holding_id])
        existing = {row.theme: row for row in existing_rows}
        wanted = dict(tags)
        now = datetime.now(UTC)

        for theme, row in existing.items():
            if theme not in wanted:
                await self.session.delete(row)
        # Flush deletions first so re-adding a theme never trips the unique key.
        await self.session.flush()

        result: list[HoldingThemeTag] = []
        for theme, target in wanted.items():
            row = existing.get(theme)
            if row is None:
                row = HoldingThemeTag(
                    workspace_id=workspace_id,
                    holding_id=holding_id,
                    theme=theme,
                    target_weight_pct=target,
                )
            elif row.target_weight_pct != target:
                row.target_weight_pct = target
                row.updated_at = now
            self.session.add(row)
            result.append(row)
        await self.session.flush()
        return result


class LatestConstituentSnapshotRepository:
    """Exactly-one latest constituent snapshot per instrument.

    ``InstrumentConstituentRepository.get_latest_on_or_before_many`` returns
    every row on the newest ``as_of_date`` - which double counts when two
    sources wrote snapshots for the same date. Look-through exposure needs a
    single snapshot, so this picks the newest by ``(as_of_date, fetched_at)``
    (ties broken by source name) and returns that one snapshot's rows.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_single_latest_many(
        self, instrument_ids: Sequence[int], as_of_date: date
    ) -> dict[int, list[InstrumentConstituent]]:
        """``{instrument_id: rows of its one latest snapshot on or before as_of_date}``."""
        unique_ids = {i for i in instrument_ids if i is not None}
        if not unique_ids:
            return {}

        ranked = (
            select(
                InstrumentConstituent.instrument_id.label("instrument_id"),
                InstrumentConstituent.as_of_date.label("as_of_date"),
                InstrumentConstituent.source.label("source"),
                func
                .row_number()
                .over(
                    partition_by=InstrumentConstituent.instrument_id,
                    order_by=(
                        InstrumentConstituent.as_of_date.desc(),
                        func.max(InstrumentConstituent.fetched_at).desc(),
                        InstrumentConstituent.source.asc(),
                    ),
                )
                .label("rn"),
            )
            .where(
                InstrumentConstituent.instrument_id.in_(unique_ids),
                InstrumentConstituent.as_of_date <= as_of_date,
            )
            .group_by(
                InstrumentConstituent.instrument_id,
                InstrumentConstituent.as_of_date,
                InstrumentConstituent.source,
            )
            .subquery()
        )
        result = await self.session.execute(
            select(InstrumentConstituent)
            .join(
                ranked,
                (InstrumentConstituent.instrument_id == ranked.c.instrument_id)
                & (InstrumentConstituent.as_of_date == ranked.c.as_of_date)
                & (InstrumentConstituent.source == ranked.c.source),
            )
            .where(ranked.c.rn == 1)
            .order_by(InstrumentConstituent.instrument_id.asc(), InstrumentConstituent.id.asc())
        )
        grouped: dict[int, list[InstrumentConstituent]] = {}
        for row in result.scalars().all():
            grouped.setdefault(row.instrument_id, []).append(row)
        return grouped
