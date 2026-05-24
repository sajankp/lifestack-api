from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import DEFAULT_LIMIT
from app.investing.models import CashBalance, Company, Holding, Instrument, InstrumentConstituent


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
