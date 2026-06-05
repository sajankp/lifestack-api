from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import DEFAULT_LIMIT
from app.finance.models import (
    Account,
    CapitalTransfer,
    Currency,
    CurrencyDisplayPreference,
    FxRate,
    UserFinanceSetting,
    WorkspaceCurrency,
    WorkspaceFinanceSetting,
)
from app.spending.models import SpendingTransaction


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

    async def ensure_workspace_defaults(self, workspace_id: int) -> None:
        existing_count = (
            await self.session.execute(
                select(func.count()).select_from(
                    select(WorkspaceCurrency)
                    .where(WorkspaceCurrency.workspace_id == workspace_id)
                    .subquery()
                )
            )
        ).scalar_one()
        if existing_count > 0:
            return
        active = await self.list_active()
        if active:
            await self.add_workspace_currencies(
                workspace_id, [currency.code for currency in active]
            )

    async def get_by_code(self, code: str) -> Currency | None:
        result = await self.session.execute(select(Currency).where(Currency.code == code.upper()))
        return result.scalar_one_or_none()

    async def is_enabled_for_workspace(self, workspace_id: int, code: str) -> bool:
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

    async def list_by_ids(self, workspace_id: int, account_ids: Sequence[int]) -> Sequence[Account]:
        if not account_ids:
            return []
        unique_ids = list({account_id for account_id in account_ids if account_id is not None})
        if not unique_ids:
            return []
        result = await self.session.execute(
            select(Account).where(
                Account.workspace_id == workspace_id,
                Account.id.in_(unique_ids),
            )
        )
        return result.scalars().all()

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

    async def has_usage(self, workspace_id: int, account_id: int) -> bool:
        spending_usage_exists = (
            await self.session.execute(
                select(SpendingTransaction.id)
                .where(
                    SpendingTransaction.workspace_id == workspace_id,
                    SpendingTransaction.account_id == account_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if spending_usage_exists is not None:
            return True

        transfer_usage_exists = (
            await self.session.execute(
                select(CapitalTransfer.id)
                .where(
                    CapitalTransfer.workspace_id == workspace_id,
                    or_(
                        CapitalTransfer.from_account_id == account_id,
                        CapitalTransfer.to_account_id == account_id,
                    ),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return transfer_usage_exists is not None

    async def delete(self, account: Account) -> None:
        await self.session.delete(account)
        await self.session.flush()


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

    async def upsert_workspace_settings(
        self,
        workspace_id: int,
        *,
        reporting_currency_code: str | None,
        currency_display_preference: CurrencyDisplayPreference | None,
    ) -> WorkspaceFinanceSetting:
        existing = await self.get_by_workspace(workspace_id)
        now = datetime.now(UTC)
        if existing:
            existing.reporting_currency_code = reporting_currency_code
            if currency_display_preference is not None:
                existing.currency_display_preference = currency_display_preference
            existing.updated_at = now
            self.session.add(existing)
            await self.session.flush()
            await self.session.refresh(existing)
            return existing

        row = WorkspaceFinanceSetting(
            workspace_id=workspace_id,
            reporting_currency_code=reporting_currency_code,
            currency_display_preference=(
                currency_display_preference or CurrencyDisplayPreference.symbol
            ),
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def get_user_setting(self, workspace_id: int, user_id: int) -> UserFinanceSetting | None:
        result = await self.session.execute(
            select(UserFinanceSetting).where(
                UserFinanceSetting.workspace_id == workspace_id,
                UserFinanceSetting.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def upsert_user_settings(
        self,
        workspace_id: int,
        user_id: int,
        *,
        reporting_currency_override_code: str | None,
        currency_display_preference_override: CurrencyDisplayPreference | None,
    ) -> UserFinanceSetting:
        existing = await self.get_user_setting(workspace_id, user_id)
        now = datetime.now(UTC)
        if existing:
            existing.reporting_currency_override_code = reporting_currency_override_code
            existing.currency_display_preference_override = currency_display_preference_override
            existing.updated_at = now
            self.session.add(existing)
            await self.session.flush()
            await self.session.refresh(existing)
            return existing

        row = UserFinanceSetting(
            workspace_id=workspace_id,
            user_id=user_id,
            reporting_currency_override_code=reporting_currency_override_code,
            currency_display_preference_override=currency_display_preference_override,
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
        rate: Decimal,
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

    async def get_latest_rates_for_pairs(
        self,
        pairs: Sequence[tuple[str, str]],
        as_of: datetime | None = None,
    ) -> dict[tuple[str, str], FxRate]:
        unique_pairs = {(b.upper(), q.upper()) for b, q in pairs if b and q}
        if not unique_pairs:
            return {}
        base_codes = {b for b, _ in unique_pairs}
        quote_codes = {q for _, q in unique_pairs}
        query = select(FxRate).where(
            FxRate.base_currency_code.in_(base_codes),
            FxRate.quote_currency_code.in_(quote_codes),
        )
        if as_of is not None:
            query = query.where(FxRate.as_of <= as_of)
        result = await self.session.execute(
            query.order_by(
                FxRate.base_currency_code.asc(),
                FxRate.quote_currency_code.asc(),
                FxRate.as_of.desc(),
            )
        )
        rows = result.scalars().all()

        latest: dict[tuple[str, str], FxRate] = {}
        for row in rows:
            key = (row.base_currency_code.upper(), row.quote_currency_code.upper())
            if key not in unique_pairs:
                continue
            if key not in latest:
                latest[key] = row
        return latest


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
