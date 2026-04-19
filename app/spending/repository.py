from collections.abc import Sequence
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.spending.models import SpendingBudget, SpendingCategory, SpendingTransaction


class CategoryRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_all(self, workspace_id: int) -> Sequence[SpendingCategory]:
        result = await self.session.execute(
            select(SpendingCategory).where(SpendingCategory.workspace_id == workspace_id)
        )
        return result.scalars().all()

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> SpendingCategory | None:
        result = await self.session.execute(
            select(SpendingCategory).where(
                SpendingCategory.workspace_id == workspace_id,
                SpendingCategory.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_normalized_name(
        self, workspace_id: int, normalized_name: str
    ) -> SpendingCategory | None:
        result = await self.session.execute(
            select(SpendingCategory).where(
                SpendingCategory.workspace_id == workspace_id,
                SpendingCategory.normalized_name == normalized_name,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, category: SpendingCategory) -> SpendingCategory:
        self.session.add(category)
        await self.session.flush()
        await self.session.refresh(category)
        return category

    async def save(self, category: SpendingCategory) -> SpendingCategory:
        self.session.add(category)
        await self.session.flush()
        await self.session.refresh(category)
        return category

    async def delete(self, category: SpendingCategory) -> None:
        await self.session.delete(category)
        await self.session.flush()

    async def has_transactions(self, category_id: int) -> bool:
        result = await self.session.execute(
            select(SpendingTransaction).where(SpendingTransaction.category_id == category_id)
        )
        return result.scalar_one_or_none() is not None

    async def create_many(self, categories: list[SpendingCategory]) -> None:
        """Bulk insert categories (used during workspace provisioning)."""
        for category in categories:
            self.session.add(category)
        await self.session.flush()


class TransactionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_all(
        self,
        workspace_id: int,
        category_id: int | None = None,
        type_filter: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> Sequence[SpendingTransaction]:
        query = select(SpendingTransaction).where(SpendingTransaction.workspace_id == workspace_id)
        if category_id is not None:
            query = query.where(SpendingTransaction.category_id == category_id)
        if type_filter is not None:
            query = query.where(SpendingTransaction.type == type_filter)
        if from_date is not None:
            query = query.where(SpendingTransaction.occurred_at >= from_date)
        if to_date is not None:
            query = query.where(SpendingTransaction.occurred_at <= to_date)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_by_public_id(
        self, workspace_id: int, public_id: UUID
    ) -> SpendingTransaction | None:
        result = await self.session.execute(
            select(SpendingTransaction).where(
                SpendingTransaction.workspace_id == workspace_id,
                SpendingTransaction.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, transaction: SpendingTransaction) -> SpendingTransaction:
        self.session.add(transaction)
        await self.session.flush()
        await self.session.refresh(transaction)
        return transaction

    async def save(self, transaction: SpendingTransaction) -> SpendingTransaction:
        self.session.add(transaction)
        await self.session.flush()
        await self.session.refresh(transaction)
        return transaction

    async def delete(self, transaction: SpendingTransaction) -> None:
        await self.session.delete(transaction)
        await self.session.flush()


class BudgetRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_all(self, workspace_id: int) -> Sequence[SpendingBudget]:
        result = await self.session.execute(
            select(SpendingBudget).where(SpendingBudget.workspace_id == workspace_id)
        )
        return result.scalars().all()

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> SpendingBudget | None:
        result = await self.session.execute(
            select(SpendingBudget).where(
                SpendingBudget.workspace_id == workspace_id,
                SpendingBudget.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_category_and_month(
        self, workspace_id: int, category_id: int, month_start: date
    ) -> SpendingBudget | None:
        result = await self.session.execute(
            select(SpendingBudget).where(
                SpendingBudget.workspace_id == workspace_id,
                SpendingBudget.category_id == category_id,
                SpendingBudget.month_start == month_start,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, budget: SpendingBudget) -> SpendingBudget:
        self.session.add(budget)
        await self.session.flush()
        await self.session.refresh(budget)
        return budget

    async def save(self, budget: SpendingBudget) -> SpendingBudget:
        self.session.add(budget)
        await self.session.flush()
        await self.session.refresh(budget)
        return budget
