from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import case, func, or_, select, true
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
from app.investing.models import CashBalance, Holding
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
        if transfer_usage_exists is not None:
            return True

        holding_usage_exists = (
            await self.session.execute(
                select(Holding.id)
                .where(
                    Holding.workspace_id == workspace_id,
                    Holding.account_id == account_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if holding_usage_exists is not None:
            return True

        cash_balance_usage_exists = (
            await self.session.execute(
                select(CashBalance.id)
                .where(
                    CashBalance.workspace_id == workspace_id,
                    CashBalance.account_id == account_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return cash_balance_usage_exists is not None

    async def delete(self, account: Account) -> None:
        await self.session.delete(account)
        await self.session.flush()

    async def get_spending_balance(
        self, workspace_id: int, account_id: int
    ) -> tuple[Decimal, int, int, "datetime | None", "datetime | None"]:
        """Return (projected_balance, tx_count, transfer_count, first_tx_at, last_tx_at).

        projected_balance = (income txns - expense txns)
                          + (transfer inflows - transfer outflows)

        This is the single source of truth for the account's calculated balance.
        """
        income_sum = func.coalesce(
            func.sum(
                case(
                    (SpendingTransaction.type == "income", SpendingTransaction.amount),
                    else_=Decimal("0"),
                )
            ),
            Decimal("0"),
        )
        expense_sum = func.coalesce(
            func.sum(
                case(
                    (SpendingTransaction.type == "expense", SpendingTransaction.amount),
                    else_=Decimal("0"),
                )
            ),
            Decimal("0"),
        )
        tx_count_col = func.count(SpendingTransaction.id)
        first_tx = func.min(SpendingTransaction.occurred_at)
        last_tx = func.max(SpendingTransaction.occurred_at)

        tx_sub = (
            select(
                income_sum.label("income"),
                expense_sum.label("expense"),
                tx_count_col.label("tx_count"),
                first_tx.label("first_at"),
                last_tx.label("last_at"),
            )
            .where(
                SpendingTransaction.workspace_id == workspace_id,
                SpendingTransaction.account_id == account_id,
            )
            .subquery()
        )

        inflow_sub = (
            select(
                func.coalesce(func.sum(CapitalTransfer.gross_amount), Decimal("0")).label("inflow"),
                func.count(CapitalTransfer.id).label("inflow_count"),
            )
            .where(
                CapitalTransfer.workspace_id == workspace_id,
                CapitalTransfer.to_account_id == account_id,
            )
            .subquery()
        )

        outflow_sub = (
            select(
                func.coalesce(func.sum(CapitalTransfer.gross_amount), Decimal("0")).label(
                    "outflow"
                ),
                func.count(CapitalTransfer.id).label("outflow_count"),
            )
            .where(
                CapitalTransfer.workspace_id == workspace_id,
                CapitalTransfer.from_account_id == account_id,
            )
            .subquery()
        )

        result = await self.session.execute(
            select(
                tx_sub.c.income,
                tx_sub.c.expense,
                tx_sub.c.tx_count,
                tx_sub.c.first_at,
                tx_sub.c.last_at,
                inflow_sub.c.inflow,
                inflow_sub.c.inflow_count,
                outflow_sub.c.outflow,
                outflow_sub.c.outflow_count,
            )
            .select_from(tx_sub)
            .join(inflow_sub, true())
            .join(outflow_sub, true())
        )
        row = result.one()
        income = Decimal(str(row[0] or "0"))
        expense = Decimal(str(row[1] or "0"))
        tx_count = int(row[2] or 0)
        first_at = row[3]
        last_at = row[4]
        inflow = Decimal(str(row[5] or "0"))
        inflow_count = int(row[6] or 0)
        outflow = Decimal(str(row[7] or "0"))
        outflow_count = int(row[8] or 0)

        projected_balance = income - expense + inflow - outflow
        transfer_count = inflow_count + outflow_count

        return projected_balance, tx_count, transfer_count, first_at, last_at

    async def get_reconciliation_summary(
        self, workspace_id: int, account_id: int
    ) -> tuple[Decimal, int, int, Decimal | None, "datetime | None"]:
        """Return (projected_balance, tx_count, transfer_count, snapshot_balance, snapshot_as_of).

        projected_balance is the transfer-inclusive ledger balance.
        snapshot_balance is the most recent CashBalance.balance for this account,
        or None if no snapshot exists.
        """
        (
            projected_balance,
            tx_count,
            transfer_count,
            _first,
            _last,
        ) = await self.get_spending_balance(workspace_id, account_id)

        snapshot_row = await self.session.execute(
            select(CashBalance.balance, CashBalance.as_of)
            .where(
                CashBalance.workspace_id == workspace_id,
                CashBalance.account_id == account_id,
            )
            .order_by(CashBalance.as_of.desc())
            .limit(1)
        )
        snapshot = snapshot_row.one_or_none()
        snapshot_balance: Decimal | None = None
        snapshot_as_of: datetime | None = None
        if snapshot is not None:
            snapshot_balance = Decimal(str(snapshot[0]))
            snapshot_as_of = snapshot[1]

        return projected_balance, tx_count, transfer_count, snapshot_balance, snapshot_as_of


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
        lookthrough_min_weight_pct: Decimal = Decimal("0.5"),
    ) -> WorkspaceFinanceSetting:
        existing = await self.get_by_workspace(workspace_id)
        now = datetime.now(UTC)
        if existing:
            existing.reporting_currency_code = reporting_currency_code
            if currency_display_preference is not None:
                existing.currency_display_preference = currency_display_preference
            existing.lookthrough_min_weight_pct = lookthrough_min_weight_pct
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
            lookthrough_min_weight_pct=lookthrough_min_weight_pct,
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

    async def save(self, transfer: CapitalTransfer) -> CapitalTransfer:
        self.session.add(transfer)
        await self.session.flush()
        await self.session.refresh(transfer)
        return transfer

    async def delete(self, transfer: CapitalTransfer) -> None:
        await self.session.delete(transfer)
        await self.session.flush()
