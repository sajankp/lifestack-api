from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import case, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import DEFAULT_LIMIT
from app.core.repository import BaseRepository
from app.finance.models import (
    Account,
    CapitalTransfer,
    Currency,
    CurrencyDisplayPreference,
    FxRate,
    NetWorthSnapshot,
    UserFinanceSetting,
    WorkspaceCurrency,
    WorkspaceFinanceSetting,
)
from app.investing.models import CashBalance, Dividend, Holding, InvestingOrder
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


class AccountRepository(BaseRepository[Account]):
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
                # net_amount_received: what the destination account actually received.
                # For cross-currency transfers this is in to_currency (correct for the
                # receiving account), whereas gross_amount is in from_currency (wrong).
                func.coalesce(func.sum(CapitalTransfer.net_amount_received), Decimal("0")).label(
                    "inflow"
                ),
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

    async def get_spending_balances_bulk(
        self, workspace_id: int, account_ids: list[int]
    ) -> dict[int, tuple[Decimal, int, int, "datetime | None", "datetime | None"]]:
        """Return spending balance data for multiple accounts in three queries instead of N.

        Returns a dict keyed by account_id with the same tuple as get_spending_balance.
        """
        if not account_ids:
            return {}

        tx_result = await self.session.execute(
            select(
                SpendingTransaction.account_id,
                func.coalesce(
                    func.sum(
                        case(
                            (SpendingTransaction.type == "income", SpendingTransaction.amount),
                            else_=Decimal("0"),
                        )
                    ),
                    Decimal("0"),
                ).label("income"),
                func.coalesce(
                    func.sum(
                        case(
                            (SpendingTransaction.type == "expense", SpendingTransaction.amount),
                            else_=Decimal("0"),
                        )
                    ),
                    Decimal("0"),
                ).label("expense"),
                func.count(SpendingTransaction.id).label("tx_count"),
                func.min(SpendingTransaction.occurred_at).label("first_at"),
                func.max(SpendingTransaction.occurred_at).label("last_at"),
            )
            .where(
                SpendingTransaction.workspace_id == workspace_id,
                SpendingTransaction.account_id.in_(account_ids),
            )
            .group_by(SpendingTransaction.account_id)
        )
        tx_by_account: dict[int, tuple] = {row[0]: row for row in tx_result.all()}

        inflow_result = await self.session.execute(
            select(
                CapitalTransfer.to_account_id,
                func.coalesce(func.sum(CapitalTransfer.net_amount_received), Decimal("0")).label(
                    "inflow"
                ),
                func.count(CapitalTransfer.id).label("inflow_count"),
            )
            .where(
                CapitalTransfer.workspace_id == workspace_id,
                CapitalTransfer.to_account_id.in_(account_ids),
            )
            .group_by(CapitalTransfer.to_account_id)
        )
        inflow_by_account: dict[int, tuple] = {row[0]: row for row in inflow_result.all()}

        outflow_result = await self.session.execute(
            select(
                CapitalTransfer.from_account_id,
                func.coalesce(func.sum(CapitalTransfer.gross_amount), Decimal("0")).label(
                    "outflow"
                ),
                func.count(CapitalTransfer.id).label("outflow_count"),
            )
            .where(
                CapitalTransfer.workspace_id == workspace_id,
                CapitalTransfer.from_account_id.in_(account_ids),
            )
            .group_by(CapitalTransfer.from_account_id)
        )
        outflow_by_account: dict[int, tuple] = {row[0]: row for row in outflow_result.all()}

        results: dict[int, tuple[Decimal, int, int, datetime | None, datetime | None]] = {}
        for account_id in account_ids:
            tx = tx_by_account.get(account_id)
            income = Decimal(str(tx[1] or "0")) if tx else Decimal("0")
            expense = Decimal(str(tx[2] or "0")) if tx else Decimal("0")
            tx_count = int(tx[3] or 0) if tx else 0
            first_at = tx[4] if tx else None
            last_at = tx[5] if tx else None

            inf = inflow_by_account.get(account_id)
            inflow = Decimal(str(inf[1] or "0")) if inf else Decimal("0")
            inflow_count = int(inf[2] or 0) if inf else 0

            out = outflow_by_account.get(account_id)
            outflow = Decimal(str(out[1] or "0")) if out else Decimal("0")
            outflow_count = int(out[2] or 0) if out else 0

            projected_balance = income - expense + inflow - outflow
            transfer_count = inflow_count + outflow_count
            results[account_id] = (projected_balance, tx_count, transfer_count, first_at, last_at)

        return results

    async def get_reconciliation_summary(
        self, workspace_id: int, account_id: int
    ) -> tuple[Decimal, int, int, int, int, Decimal | None, "datetime | None"]:
        """Return (projected_balance, tx_count, transfer_count, order_count,
        dividend_count, snapshot_balance, snapshot_as_of).

        projected_balance = (income - expense) + (transfer_in - transfer_out)
                          + (sell net - buy net)   ← investing order cash impact
                          + dividend net credits    ← spec-073 INV-2
        snapshot_balance is the most recent CashBalance.balance for this account,
        or None if no snapshot exists.
        """
        (
            ledger_balance,
            tx_count,
            transfer_count,
            _first,
            _last,
        ) = await self.get_spending_balance(workspace_id, account_id)

        # Investing order cash impact: a buy removes net_amount (gross + fees)
        # from the account's cash, a sell adds net_amount (gross - fees). Orders
        # already move the cash *snapshot* (see investing/service.py), so the
        # projected side must include them too — otherwise a brokerage account
        # shows a false discrepancy equal to net trade flow. Summing unconverted
        # is safe because a brokerage account holds a single currency (invariant;
        # see docs/domain/cash-model-ledger-snapshots-reconciliation.md).
        orders_row = await self.session.execute(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (InvestingOrder.order_type == "sell", InvestingOrder.net_amount),
                            else_=-InvestingOrder.net_amount,
                        )
                    ),
                    Decimal("0"),
                ),
                func.count(InvestingOrder.id),
            ).where(
                InvestingOrder.workspace_id == workspace_id,
                InvestingOrder.account_id == account_id,
            )
        )
        orders_result = orders_row.one()
        orders_net = Decimal(str(orders_result[0] or "0"))
        order_count = int(orders_result[1] or 0)

        # Dividend/income cash impact (spec-073 INV-2): a dividend credits cash
        # with no offsetting debit anywhere, so — like orders — it must appear
        # on the projected side too, or it manufactures a permanent discrepancy
        # equal to the dividend total.
        dividend_row = await self.session.execute(
            select(
                func.coalesce(func.sum(Dividend.net_amount), Decimal("0")),
                func.count(Dividend.id),
            ).where(
                Dividend.workspace_id == workspace_id,
                Dividend.account_id == account_id,
            )
        )
        dividend_result = dividend_row.one()
        dividend_net = Decimal(str(dividend_result[0] or "0"))
        dividend_count = int(dividend_result[1] or 0)

        projected_balance = ledger_balance + orders_net + dividend_net

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

        return (
            projected_balance,
            tx_count,
            transfer_count,
            order_count,
            dividend_count,
            snapshot_balance,
            snapshot_as_of,
        )


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
        default_spending_account_id: int | None = None,
    ) -> WorkspaceFinanceSetting:
        existing = await self.get_by_workspace(workspace_id)
        now = datetime.now(UTC)
        if existing:
            existing.reporting_currency_code = reporting_currency_code
            if currency_display_preference is not None:
                existing.currency_display_preference = currency_display_preference
            existing.lookthrough_min_weight_pct = lookthrough_min_weight_pct
            existing.default_spending_account_id = default_spending_account_id
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
            default_spending_account_id=default_spending_account_id,
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def clear_default_spending_account(self, workspace_id: int, account_id: int) -> None:
        """Called when an account is deactivated (spec-054) — a deactivated
        account can no longer serve as the fallback for new transactions."""
        existing = await self.get_by_workspace(workspace_id)
        if existing and existing.default_spending_account_id == account_id:
            existing.default_spending_account_id = None
            existing.updated_at = datetime.now(UTC)
            self.session.add(existing)
            await self.session.flush()

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
        # System rows only (workspace_id IS NULL) -- this is the live-fetch
        # ingestion path; it must never collide with or overwrite a
        # user-provided historical row (spec-072 INV-3).
        result = await self.session.execute(
            select(FxRate).where(
                FxRate.workspace_id.is_(None),
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
        # System rows only (workspace_id IS NULL): this feeds live/current
        # valuation, which must never see a user-provided historical rate
        # (spec-072 INV-3). Historical resolution with user fallback is
        # get_historical_rate_with_source below.
        query = select(FxRate).where(
            FxRate.workspace_id.is_(None),
            FxRate.base_currency_code == base_currency_code,
            FxRate.quote_currency_code == quote_currency_code,
        )
        if as_of is not None:
            query = query.where(FxRate.as_of <= as_of)
        result = await self.session.execute(query.order_by(FxRate.as_of.desc()).limit(1))
        return result.scalar_one_or_none()

    async def get_historical_rate_with_source(
        self,
        workspace_id: int,
        base_currency_code: str,
        quote_currency_code: str,
        as_of: datetime,
    ) -> tuple[Decimal, str] | None:
        """Past-dated resolution with precedence: system rate (as of or
        before the date) -> user rate for that workspace (as of or before
        the date) -> None. System always wins when both exist (spec-072
        INV-3). Returns (rate, 'system'|'user_provided') or None."""
        system = await self.get_latest_rate(base_currency_code, quote_currency_code, as_of=as_of)
        if system is not None:
            return Decimal(str(system.rate)), "system"

        result = await self.session.execute(
            select(FxRate)
            .where(
                FxRate.workspace_id == workspace_id,
                FxRate.base_currency_code == base_currency_code,
                FxRate.quote_currency_code == quote_currency_code,
                FxRate.as_of <= as_of,
            )
            .order_by(FxRate.as_of.desc())
            .limit(1)
        )
        user_row = result.scalar_one_or_none()
        if user_row is not None:
            return Decimal(str(user_row.rate)), "user_provided"
        return None

    async def get_user_rate_for_date(
        self,
        workspace_id: int,
        base_currency_code: str,
        quote_currency_code: str,
        as_of: datetime,
    ) -> FxRate | None:
        result = await self.session.execute(
            select(FxRate).where(
                FxRate.workspace_id == workspace_id,
                FxRate.base_currency_code == base_currency_code,
                FxRate.quote_currency_code == quote_currency_code,
                FxRate.as_of == as_of,
            )
        )
        return result.scalar_one_or_none()

    async def upsert_user_rate(
        self,
        workspace_id: int,
        base_currency_code: str,
        quote_currency_code: str,
        rate: Decimal,
        as_of: datetime,
        existing: FxRate | None = None,
    ) -> FxRate:
        """``existing`` is the row previously loaded via
        ``get_user_rate_for_date`` for the same key — passing it avoids
        re-running that exact SELECT per imported row."""
        if existing is not None:
            existing.rate = rate
            existing.updated_at = datetime.now(UTC)
            self.session.add(existing)
            await self.session.flush()
            await self.session.refresh(existing)
            return existing

        row = FxRate(
            workspace_id=workspace_id,
            base_currency_code=base_currency_code,
            quote_currency_code=quote_currency_code,
            rate=rate,
            as_of=as_of,
            fetched_at=datetime.now(UTC),
            source="user_provided",
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def list_user_rates(
        self, workspace_id: int, limit: int = 200, offset: int = 0
    ) -> tuple[Sequence[FxRate], int]:
        base = select(FxRate).where(FxRate.workspace_id == workspace_id)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(FxRate.as_of.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def get_user_rate_by_id(self, workspace_id: int, row_id: int) -> FxRate | None:
        result = await self.session.execute(
            select(FxRate).where(FxRate.id == row_id, FxRate.workspace_id == workspace_id)
        )
        return result.scalar_one_or_none()

    async def delete_user_rate(self, row: FxRate) -> None:
        await self.session.delete(row)
        await self.session.flush()

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
        # System rows only -- feeds live valuation (spec-072 INV-3).
        query = select(FxRate).where(
            FxRate.workspace_id.is_(None),
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


class CapitalTransferRepository(BaseRepository[CapitalTransfer]):
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


class NetWorthSnapshotRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(self, snapshot: NetWorthSnapshot) -> NetWorthSnapshot:
        stmt = select(NetWorthSnapshot).where(
            NetWorthSnapshot.workspace_id == snapshot.workspace_id,
            NetWorthSnapshot.snapshot_date == snapshot.snapshot_date,
        )
        res = await self.session.execute(stmt)
        existing = res.scalar_one_or_none()

        if existing:
            existing.reporting_currency = snapshot.reporting_currency
            existing.holdings_value = snapshot.holdings_value
            existing.investing_cash = snapshot.investing_cash
            existing.spending_cash = snapshot.spending_cash
            existing.total_net_worth = snapshot.total_net_worth
            existing.fx_rates_used = snapshot.fx_rates_used
            self.session.add(existing)
            return existing
        else:
            self.session.add(snapshot)
            return snapshot

    async def get_history(
        self, workspace_id: int, from_date: date, to_date: date
    ) -> Sequence[NetWorthSnapshot]:
        stmt = (
            select(NetWorthSnapshot)
            .where(
                NetWorthSnapshot.workspace_id == workspace_id,
                NetWorthSnapshot.snapshot_date >= from_date,
                NetWorthSnapshot.snapshot_date <= to_date,
            )
            .order_by(NetWorthSnapshot.snapshot_date.asc())
        )
        res = await self.session.execute(stmt)
        return res.scalars().all()

    async def get_for_date(self, workspace_id: int, snapshot_date: date) -> NetWorthSnapshot | None:
        stmt = select(NetWorthSnapshot).where(
            NetWorthSnapshot.workspace_id == workspace_id,
            NetWorthSnapshot.snapshot_date == snapshot_date,
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_earliest_live_date(self, workspace_id: int) -> date | None:
        """The backfill boundary (spec-072 INV-2): a user point is only ever
        accepted for dates strictly before this, or before today when no
        live row exists yet -- so the daily job can never collide with a
        user row on the (workspace, snapshot_date) unique constraint."""
        result = await self.session.execute(
            select(func.min(NetWorthSnapshot.snapshot_date)).where(
                NetWorthSnapshot.workspace_id == workspace_id,
                NetWorthSnapshot.source == "live",
            )
        )
        return result.scalar_one_or_none()

    async def create_user_point(
        self, snapshot: NetWorthSnapshot, existing: NetWorthSnapshot | None = None
    ) -> NetWorthSnapshot:
        """Upsert-keyed on (workspace, date) among user rows only -- INV-2
        (date-boundary check) is the caller's job before this is called.
        ``existing`` is the row the caller already loaded via ``get_for_date``
        for the same key — passing it avoids re-running that SELECT."""
        if existing is not None:
            existing.reporting_currency = snapshot.reporting_currency
            existing.holdings_value = snapshot.holdings_value
            existing.investing_cash = snapshot.investing_cash
            existing.spending_cash = snapshot.spending_cash
            existing.total_net_worth = snapshot.total_net_worth
            existing.source = "user_provided"
            self.session.add(existing)
            await self.session.flush()
            return existing
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def list_user_points(
        self, workspace_id: int, limit: int = 200, offset: int = 0
    ) -> tuple[Sequence[NetWorthSnapshot], int]:
        base = select(NetWorthSnapshot).where(
            NetWorthSnapshot.workspace_id == workspace_id,
            NetWorthSnapshot.source == "user_provided",
        )
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(NetWorthSnapshot.snapshot_date.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def get_user_point_by_id(self, workspace_id: int, row_id: int) -> NetWorthSnapshot | None:
        result = await self.session.execute(
            select(NetWorthSnapshot).where(
                NetWorthSnapshot.id == row_id,
                NetWorthSnapshot.workspace_id == workspace_id,
                NetWorthSnapshot.source == "user_provided",
            )
        )
        return result.scalar_one_or_none()

    async def delete_user_point(self, row: NetWorthSnapshot) -> None:
        await self.session.delete(row)
        await self.session.flush()
