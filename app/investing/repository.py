from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import DEFAULT_LIMIT
from app.investing.models import CashBalance, Holding


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
