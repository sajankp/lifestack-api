from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import DEFAULT_LIMIT
from app.finance.models import Account, Currency, WorkspaceCurrency, WorkspaceFinanceSetting


class CurrencyRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_workspace_enabled(self, workspace_id: int) -> Sequence[Currency]:
        result = await self.session.execute(
            select(Currency)
            .join(WorkspaceCurrency, Currency.code == WorkspaceCurrency.currency_code)
            .where(WorkspaceCurrency.workspace_id == workspace_id)
            .where(Currency.is_active.is_(True))
            .order_by(Currency.code.asc())
        )
        return result.scalars().all()

    async def get_by_code(self, code: str) -> Currency | None:
        result = await self.session.execute(select(Currency).where(Currency.code == code.upper()))
        return result.scalar_one_or_none()


class AccountRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_workspace_accounts(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[Sequence[Account], int]:
        base = select(Account).where(Account.workspace_id == workspace_id)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(Account.created_at.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> Account | None:
        result = await self.session.execute(
            select(Account).where(
                Account.workspace_id == workspace_id,
                Account.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_name(self, workspace_id: int, name: str) -> Account | None:
        result = await self.session.execute(
            select(Account).where(
                Account.workspace_id == workspace_id,
                Account.name == name,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, account: Account) -> Account:
        self.session.add(account)
        await self.session.flush()
        await self.session.refresh(account)
        return account

    async def save(self, account: Account) -> Account:
        self.session.add(account)
        await self.session.flush()
        await self.session.refresh(account)
        return account


class FinanceSettingRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_workspace(self, workspace_id: int) -> WorkspaceFinanceSetting | None:
        result = await self.session.execute(
            select(WorkspaceFinanceSetting).where(
                WorkspaceFinanceSetting.workspace_id == workspace_id
            )
        )
        return result.scalar_one_or_none()
