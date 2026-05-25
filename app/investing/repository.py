from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import DEFAULT_LIMIT
from app.investing.models import (
    CashBalance,
    Company,
    Holding,
    HoldingPrice,
    Instrument,
    InstrumentConstituent,
    PortfolioSnapshot,
)


class HoldingRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_all(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[Holding], int]:
        base = select(Holding).where(Holding.workspace_id == workspace_id)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(Holding.created_at.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> Holding | None:
        result = await self.session.execute(
            select(Holding).where(
                Holding.workspace_id == workspace_id,
                Holding.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_unique_key(
        self, workspace_id: int, symbol: str, account_name: str
    ) -> Holding | None:
        result = await self.session.execute(
            select(Holding).where(
                Holding.workspace_id == workspace_id,
                Holding.symbol == symbol,
                Holding.account_name == account_name,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, holding: Holding) -> Holding:
        self.session.add(holding)
        await self.session.flush()
        await self.session.refresh(holding)
        return holding

    async def save(self, holding: Holding) -> Holding:
        self.session.add(holding)
        await self.session.flush()
        await self.session.refresh(holding)
        return holding

    async def delete(self, holding: Holding) -> None:
        await self.session.delete(holding)
        await self.session.flush()


class CashBalanceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_all(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[CashBalance], int]:
        base = select(CashBalance).where(CashBalance.workspace_id == workspace_id)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(CashBalance.as_of.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> CashBalance | None:
        result = await self.session.execute(
            select(CashBalance).where(
                CashBalance.workspace_id == workspace_id,
                CashBalance.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, cash_balance: CashBalance) -> CashBalance:
        self.session.add(cash_balance)
        await self.session.flush()
        await self.session.refresh(cash_balance)
        return cash_balance

    async def save(self, cash_balance: CashBalance) -> CashBalance:
        self.session.add(cash_balance)
        await self.session.flush()
        await self.session.refresh(cash_balance)
        return cash_balance

    async def delete(self, cash_balance: CashBalance) -> None:
        await self.session.delete(cash_balance)
        await self.session.flush()


class InstrumentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_workspace(self, workspace_id: int) -> Sequence[Instrument]:
        result = await self.session.execute(
            select(Instrument)
            .where(Instrument.workspace_id == workspace_id)
            .order_by(Instrument.created_at.desc())
        )
        return result.scalars().all()

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> Instrument | None:
        result = await self.session.execute(
            select(Instrument).where(
                Instrument.workspace_id == workspace_id,
                Instrument.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_symbol(self, workspace_id: int, symbol: str) -> Instrument | None:
        result = await self.session.execute(
            select(Instrument).where(
                Instrument.workspace_id == workspace_id,
                Instrument.symbol == symbol,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_symbols(
        self, workspace_id: int, symbols: Sequence[str]
    ) -> dict[str, Instrument]:
        normalized = {s for s in symbols if s}
        if not normalized:
            return {}
        result = await self.session.execute(
            select(Instrument).where(
                Instrument.workspace_id == workspace_id,
                Instrument.symbol.in_(normalized),
            )
        )
        rows = result.scalars().all()
        return {row.symbol: row for row in rows}

    async def get_by_ids(self, ids: Sequence[int]) -> dict[int, Instrument]:
        unique_ids = {i for i in ids if i is not None}
        if not unique_ids:
            return {}
        result = await self.session.execute(select(Instrument).where(Instrument.id.in_(unique_ids)))
        rows = result.scalars().all()
        return {row.id: row for row in rows if row.id is not None}

    async def create(self, instrument: Instrument) -> Instrument:
        self.session.add(instrument)
        await self.session.flush()
        await self.session.refresh(instrument)
        return instrument

    async def save(self, instrument: Instrument) -> Instrument:
        self.session.add(instrument)
        await self.session.flush()
        await self.session.refresh(instrument)
        return instrument


class CompanyRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> Company | None:
        result = await self.session.execute(
            select(Company).where(
                Company.workspace_id == workspace_id,
                Company.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_name(self, workspace_id: int, name: str) -> Company | None:
        result = await self.session.execute(
            select(Company).where(
                Company.workspace_id == workspace_id,
                Company.name == name,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, company_id: int) -> Company | None:
        result = await self.session.execute(select(Company).where(Company.id == company_id))
        return result.scalar_one_or_none()

    async def get_by_ids(self, ids: Sequence[int]) -> dict[int, Company]:
        unique_ids = {i for i in ids if i is not None}
        if not unique_ids:
            return {}
        result = await self.session.execute(select(Company).where(Company.id.in_(unique_ids)))
        rows = result.scalars().all()
        return {row.id: row for row in rows if row.id is not None}

    async def get_by_names(self, workspace_id: int, names: Sequence[str]) -> dict[str, Company]:
        unique_names = {n for n in names if n}
        if not unique_names:
            return {}
        result = await self.session.execute(
            select(Company).where(
                Company.workspace_id == workspace_id,
                Company.name.in_(unique_names),
            )
        )
        rows = result.scalars().all()
        return {row.name: row for row in rows}

    async def create(self, company: Company) -> Company:
        self.session.add(company)
        await self.session.flush()
        await self.session.refresh(company)
        return company


class InstrumentConstituentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def delete_snapshot(self, instrument_id: int, as_of_date: date, source: str) -> None:
        result = await self.session.execute(
            select(InstrumentConstituent).where(
                InstrumentConstituent.instrument_id == instrument_id,
                InstrumentConstituent.as_of_date == as_of_date,
                InstrumentConstituent.source == source,
            )
        )
        for row in result.scalars().all():
            await self.session.delete(row)
        await self.session.flush()

    async def create_many(self, rows: list[InstrumentConstituent]) -> list[InstrumentConstituent]:
        self.session.add_all(rows)
        await self.session.flush()
        return rows

    async def list_snapshot(
        self, instrument_id: int, as_of_date: date
    ) -> Sequence[InstrumentConstituent]:
        result = await self.session.execute(
            select(InstrumentConstituent).where(
                InstrumentConstituent.instrument_id == instrument_id,
                InstrumentConstituent.as_of_date == as_of_date,
            )
        )
        return result.scalars().all()

    async def get_latest_on_or_before(
        self, instrument_id: int, as_of_date: date
    ) -> Sequence[InstrumentConstituent]:
        latest_date = (
            await self.session.execute(
                select(func.max(InstrumentConstituent.as_of_date)).where(
                    InstrumentConstituent.instrument_id == instrument_id,
                    InstrumentConstituent.as_of_date <= as_of_date,
                )
            )
        ).scalar_one_or_none()
        if latest_date is None:
            return []
        return await self.list_snapshot(instrument_id, latest_date)

    async def get_latest_on_or_before_many(
        self, instrument_ids: Sequence[int], as_of_date: date
    ) -> dict[int, list[InstrumentConstituent]]:
        unique_ids = {i for i in instrument_ids if i is not None}
        if not unique_ids:
            return {}

        result = await self.session.execute(
            select(InstrumentConstituent)
            .where(InstrumentConstituent.instrument_id.in_(unique_ids))
            .where(InstrumentConstituent.as_of_date <= as_of_date)
            .order_by(
                InstrumentConstituent.instrument_id.asc(),
                InstrumentConstituent.as_of_date.desc(),
            )
        )
        rows = result.scalars().all()
        latest_date_by_instrument: dict[int, date] = {}
        grouped: dict[int, list[InstrumentConstituent]] = {}
        for row in rows:
            iid = row.instrument_id
            if iid is None:
                continue
            latest = latest_date_by_instrument.get(iid)
            if latest is None:
                latest_date_by_instrument[iid] = row.as_of_date
                grouped[iid] = [row]
                continue
            if row.as_of_date == latest:
                grouped[iid].append(row)
        return grouped


class HoldingPriceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert_price(
        self,
        workspace_id: int,
        holding_id: int,
        price_date: date,
        unit_price,
        source: str = "manual",
    ) -> HoldingPrice:
        existing = (
            await self.session.execute(
                select(HoldingPrice).where(
                    HoldingPrice.workspace_id == workspace_id,
                    HoldingPrice.holding_id == holding_id,
                    HoldingPrice.price_date == price_date,
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.unit_price = unit_price
            existing.source = source
            self.session.add(existing)
            await self.session.flush()
            return existing
        row = HoldingPrice(
            workspace_id=workspace_id,
            holding_id=holding_id,
            price_date=price_date,
            unit_price=unit_price,
            source=source,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_prices(
        self, workspace_id: int, holding_id: int, from_date: date, to_date: date
    ) -> list[HoldingPrice]:
        return list(
            (
                await self.session.execute(
                    select(HoldingPrice)
                    .where(
                        HoldingPrice.workspace_id == workspace_id,
                        HoldingPrice.holding_id == holding_id,
                        HoldingPrice.price_date >= from_date,
                        HoldingPrice.price_date <= to_date,
                    )
                    .order_by(HoldingPrice.price_date.asc())
                )
            )
            .scalars()
            .all()
        )


class PortfolioSnapshotRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(self, snapshot: PortfolioSnapshot) -> PortfolioSnapshot:
        existing = (
            await self.session.execute(
                select(PortfolioSnapshot).where(
                    PortfolioSnapshot.workspace_id == snapshot.workspace_id,
                    PortfolioSnapshot.snapshot_date == snapshot.snapshot_date,
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.total_value = snapshot.total_value
            existing.total_cost = snapshot.total_cost
            existing.holdings_value = snapshot.holdings_value
            existing.cash_value = snapshot.cash_value
            existing.currency_code = snapshot.currency_code
            existing.fx_rates_used = snapshot.fx_rates_used
            self.session.add(existing)
            await self.session.flush()
            return existing
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def latest(self, workspace_id: int) -> PortfolioSnapshot | None:
        return (
            await self.session.execute(
                select(PortfolioSnapshot)
                .where(PortfolioSnapshot.workspace_id == workspace_id)
                .order_by(PortfolioSnapshot.snapshot_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
