from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import DEFAULT_LIMIT
from app.finance.models import (
    Account,
    CapitalTransfer,
    Currency,
    FxRate,
    WorkspaceCurrency,
    WorkspaceFinanceSetting,
)


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

    async def list_active(self) -> Sequence[Currency]:
        result = await self.session.execute(
            select(Currency).where(Currency.is_active.is_(True)).order_by(Currency.code.asc())
        )
        return result.scalars().all()

    async def add_workspace_currencies(self, workspace_id: int, codes: Sequence[str]) -> None:
        for code in codes:
            self.session.add(
                WorkspaceCurrency(
                    workspace_id=workspace_id,
                    currency_code=code.upper(),
                )
            )
        await self.session.flush()

    async def get_by_code(self, code: str) -> Currency | None:
        result = await self.session.execute(select(Currency).where(Currency.code == code.upper()))
        return result.scalar_one_or_none()

    async def is_enabled_for_workspace(self, workspace_id: int, code: str) -> bool:
        # Lazy bootstrap: if workspace has no explicit mappings yet, enable all active currencies.
        existing_count = (
            await self.session.execute(
                select(func.count()).select_from(
                    select(WorkspaceCurrency)
                    .where(WorkspaceCurrency.workspace_id == workspace_id)
                    .subquery()
                )
            )
        ).scalar_one()
        if existing_count == 0:
            active = await self.list_active()
            if active:
                await self.add_workspace_currencies(
                    workspace_id, [currency.code for currency in active]
                )

        result = await self.session.execute(
            select(WorkspaceCurrency).where(
                WorkspaceCurrency.workspace_id == workspace_id,
                WorkspaceCurrency.currency_code == code.upper(),
            )
        )
        return result.scalar_one_or_none() is not None


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

    async def get_by_id(self, workspace_id: int, account_id: int) -> Account | None:
        result = await self.session.execute(
            select(Account).where(
                Account.workspace_id == workspace_id,
                Account.id == account_id,
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

    async def upsert_reporting_currency(
        self, workspace_id: int, reporting_currency_code: str | None
    ) -> WorkspaceFinanceSetting:
        existing = await self.get_by_workspace(workspace_id)
        now = datetime.now(UTC)
        if existing:
            existing.reporting_currency_code = reporting_currency_code
            existing.updated_at = now
            self.session.add(existing)
            await self.session.flush()
            await self.session.refresh(existing)
            return existing

        row = WorkspaceFinanceSetting(
            workspace_id=workspace_id,
            reporting_currency_code=reporting_currency_code,
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row


class FxRateRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert_rate(
        self,
        base_currency_code: str,
        quote_currency_code: str,
        rate: float,
        as_of: datetime,
        fetched_at: datetime,
        source: str,
    ) -> FxRate:
        result = await self.session.execute(
            select(FxRate).where(
                FxRate.base_currency_code == base_currency_code,
                FxRate.quote_currency_code == quote_currency_code,
                FxRate.as_of == as_of,
                FxRate.source == source,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.rate = rate
            existing.fetched_at = fetched_at
            existing.updated_at = datetime.now(UTC)
            self.session.add(existing)
            await self.session.flush()
            await self.session.refresh(existing)
            return existing

        row = FxRate(
            base_currency_code=base_currency_code,
            quote_currency_code=quote_currency_code,
            rate=rate,
            as_of=as_of,
            fetched_at=fetched_at,
            source=source,
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def get_latest_rate(
        self,
        base_currency_code: str,
        quote_currency_code: str,
        as_of: datetime | None = None,
    ) -> FxRate | None:
        query = select(FxRate).where(
            FxRate.base_currency_code == base_currency_code,
            FxRate.quote_currency_code == quote_currency_code,
        )
        if as_of is not None:
            query = query.where(FxRate.as_of <= as_of)
        result = await self.session.execute(query.order_by(FxRate.as_of.desc()).limit(1))
        return result.scalar_one_or_none()


class CapitalTransferRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_workspace_transfers(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[CapitalTransfer], int]:
        base = select(CapitalTransfer).where(CapitalTransfer.workspace_id == workspace_id)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(CapitalTransfer.occurred_at.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> CapitalTransfer | None:
        result = await self.session.execute(
            select(CapitalTransfer).where(
                CapitalTransfer.workspace_id == workspace_id,
                CapitalTransfer.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, transfer: CapitalTransfer) -> CapitalTransfer:
        self.session.add(transfer)
        await self.session.flush()
        await self.session.refresh(transfer)
        return transfer
