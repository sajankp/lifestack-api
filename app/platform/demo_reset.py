from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.audit import AuditLogger
from app.exports.models import ExportRecord
from app.finance.models import Account, CapitalTransfer, Currency, FxRate, WorkspaceCurrency
from app.imports.models import ImportBatch, ImportError, ImportPreviewRow
from app.investing.models import (
    CashBalance,
    Company,
    Holding,
    HoldingPrice,
    Instrument,
    InstrumentConstituent,
    PortfolioSnapshot,
)
from app.notifications.models import Notification
from app.platform.models import Workspace
from app.spending.models import (
    RecurringTransaction,
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
)
from app.todo.models import RecurringTodoRule, Todo

DEMO_RESET_FIXTURE_VERSION = "2026-06-10"


class DemoResetService:
    def __init__(self, session: AsyncSession, audit_logger: AuditLogger):
        self.session = session
        self.audit_logger = audit_logger

    async def log_denied(
        self,
        *,
        workspace: Workspace,
        actor_id: int,
        reason: str,
    ) -> None:
        if workspace.id is None:
            return
        await self.audit_logger.log(
            workspace_id=workspace.id,
            actor_id=actor_id,
            action="demo_reset_denied",
            module="platform",
            entity_type="workspace",
            entity_id=workspace.id,
            details={
                "entity_public_id": str(workspace.public_id),
                "before": None,
                "after": {
                    "status": "denied",
                    "reason": reason,
                    "fixture_version": DEMO_RESET_FIXTURE_VERSION,
                },
                "changed_fields": [],
            },
        )

    async def reset_workspace(self, *, workspace: Workspace, actor_id: int) -> None:
        w_id = workspace.id
        if w_id is None:
            raise ValueError("workspace_id cannot be None")

        await self._clear_workspace_data(w_id)
        await self._seed_workspace_data(w_id, actor_id)
        await self.audit_logger.log(
            workspace_id=w_id,
            actor_id=actor_id,
            action="demo_reset",
            module="platform",
            entity_type="workspace",
            entity_id=w_id,
            details={
                "entity_public_id": str(workspace.public_id),
                "before": None,
                "after": {
                    "status": "reset_success",
                    "fixture_version": DEMO_RESET_FIXTURE_VERSION,
                    "seeded": {
                        "accounts": ["brokerage", "wallet", "eur-wallet"],
                        "budgets": ["Rent", "Food", "Utilities"],
                        "holdings": ["AAPL", "MSFT"],
                        "cash_balances": ["USD", "EUR"],
                        "fx_rates": ["EUR/USD", "GBP/USD"],
                    },
                },
                "changed_fields": ["workspace_data"],
            },
        )

    async def _clear_workspace_data(self, workspace_id: int) -> None:
        if workspace_id is None:
            raise ValueError("workspace_id cannot be None for deletion")

        await self.session.execute(
            delete(RecurringTransaction).where(RecurringTransaction.workspace_id == workspace_id)
        )
        await self.session.execute(
            delete(ImportPreviewRow).where(
                ImportPreviewRow.import_batch_id.in_(
                    select(ImportBatch.id).where(ImportBatch.workspace_id == workspace_id)
                )
            )
        )
        await self.session.execute(
            delete(ImportError).where(
                ImportError.import_batch_id.in_(
                    select(ImportBatch.id).where(ImportBatch.workspace_id == workspace_id)
                )
            )
        )
        await self.session.execute(
            delete(ImportBatch).where(ImportBatch.workspace_id == workspace_id)
        )
        await self.session.execute(
            delete(ExportRecord).where(ExportRecord.workspace_id == workspace_id)
        )
        await self.session.execute(
            delete(Notification).where(Notification.workspace_id == workspace_id)
        )
        await self.session.execute(
            delete(RecurringTodoRule).where(RecurringTodoRule.workspace_id == workspace_id)
        )
        await self.session.execute(delete(Todo).where(Todo.workspace_id == workspace_id))
        await self.session.execute(
            delete(SpendingTransaction).where(SpendingTransaction.workspace_id == workspace_id)
        )
        await self.session.execute(
            delete(SpendingBudget).where(SpendingBudget.workspace_id == workspace_id)
        )
        await self.session.execute(
            delete(SpendingCategory).where(SpendingCategory.workspace_id == workspace_id)
        )
        await self.session.execute(
            delete(HoldingPrice).where(HoldingPrice.workspace_id == workspace_id)
        )
        await self.session.execute(
            delete(PortfolioSnapshot).where(PortfolioSnapshot.workspace_id == workspace_id)
        )
        await self.session.execute(delete(Holding).where(Holding.workspace_id == workspace_id))
        await self.session.execute(
            delete(CashBalance).where(CashBalance.workspace_id == workspace_id)
        )
        await self.session.execute(
            delete(InstrumentConstituent).where(
                InstrumentConstituent.instrument_id.in_(
                    select(Instrument.id).where(Instrument.workspace_id == workspace_id)
                )
            )
        )
        await self.session.execute(
            delete(Instrument).where(Instrument.workspace_id == workspace_id)
        )
        await self.session.execute(delete(Company).where(Company.workspace_id == workspace_id))
        await self.session.execute(
            delete(CapitalTransfer).where(CapitalTransfer.workspace_id == workspace_id)
        )
        await self.session.execute(delete(Account).where(Account.workspace_id == workspace_id))
        await self.session.flush()

    async def _seed_workspace_data(self, workspace_id: int, actor_id: int) -> None:
        categories_to_seed = [
            "Rent",
            "Food",
            "Utilities",
            "Entertainment",
            "Travel",
            "Salary",
            "Other",
        ]
        category_map = {}
        for cat_name in categories_to_seed:
            category = SpendingCategory(
                workspace_id=workspace_id,
                name=cat_name,
                normalized_name=cat_name.lower(),
                is_system=True,
            )
            self.session.add(category)
            await self.session.flush()
            category_map[cat_name.lower()] = category.id

        await self._ensure_demo_currencies(workspace_id)

        brokerage_acct = Account(
            workspace_id=workspace_id,
            name="brokerage",
            account_type="brokerage",
            default_currency_code="USD",
            is_active=True,
        )
        wallet_acct = Account(
            workspace_id=workspace_id,
            name="wallet",
            account_type="wallet",
            default_currency_code="USD",
            is_active=True,
        )
        eur_wallet_acct = Account(
            workspace_id=workspace_id,
            name="eur-wallet",
            account_type="wallet",
            default_currency_code="EUR",
            is_active=True,
        )
        self.session.add_all([brokerage_acct, wallet_acct, eur_wallet_acct])
        await self.session.flush()

        today_date = datetime.now(UTC).date()
        month_start_date = today_date.replace(day=1)
        for category_name, amount in [
            ("rent", Decimal("1500.00")),
            ("food", Decimal("400.00")),
            ("utilities", Decimal("200.00")),
        ]:
            self.session.add(
                SpendingBudget(
                    workspace_id=workspace_id,
                    category_id=category_map[category_name],
                    amount=amount,
                    month_start=month_start_date,
                )
            )

        for category_name, amount, transaction_type, days_ago, description in [
            ("rent", Decimal("1200.00"), "expense", 5, "Monthly Rent Payment"),
            ("food", Decimal("75.50"), "expense", 2, "Grocery Store Spend"),
            ("food", Decimal("4.75"), "expense", 1, "Coffee Shop"),
            ("salary", Decimal("3500.00"), "income", 10, "Salary Deposit"),
        ]:
            self.session.add(
                SpendingTransaction(
                    workspace_id=workspace_id,
                    user_id=actor_id,
                    category_id=category_map[category_name],
                    amount=amount,
                    type=transaction_type,
                    occurred_at=datetime.now(UTC) - timedelta(days=days_ago),
                    description=description,
                    wallet_name="wallet",
                    account_id=wallet_acct.id,
                )
            )

        apple_co = Company(
            workspace_id=workspace_id,
            name="Apple Inc.",
            ticker="AAPL",
            country_code="US",
        )
        msft_co = Company(
            workspace_id=workspace_id,
            name="Microsoft Corp.",
            ticker="MSFT",
            country_code="US",
        )
        self.session.add_all([apple_co, msft_co])
        await self.session.flush()

        aapl_inst = Instrument(
            workspace_id=workspace_id,
            symbol="AAPL",
            name="Apple Inc.",
            instrument_type="stock",
            company_id=apple_co.id,
        )
        msft_inst = Instrument(
            workspace_id=workspace_id,
            symbol="MSFT",
            name="Microsoft Corp.",
            instrument_type="stock",
            company_id=msft_co.id,
        )
        self.session.add_all([aapl_inst, msft_inst])
        await self.session.flush()

        self.session.add_all([
            Holding(
                workspace_id=workspace_id,
                user_id=actor_id,
                instrument_id=aapl_inst.id,
                symbol="AAPL",
                account_id=brokerage_acct.id,
                quantity=Decimal("10.00000000"),
                avg_cost=Decimal("150.00"),
                currency="USD",
            ),
            Holding(
                workspace_id=workspace_id,
                user_id=actor_id,
                instrument_id=msft_inst.id,
                symbol="MSFT",
                account_id=brokerage_acct.id,
                quantity=Decimal("5.00000000"),
                avg_cost=Decimal("300.00"),
                currency="USD",
            ),
            CashBalance(
                workspace_id=workspace_id,
                user_id=actor_id,
                account_id=brokerage_acct.id,
                balance=Decimal("5000.00"),
                currency="USD",
                as_of=datetime.now(UTC),
            ),
            CashBalance(
                workspace_id=workspace_id,
                user_id=actor_id,
                account_id=eur_wallet_acct.id,
                balance=Decimal("1200.00"),
                currency="EUR",
                as_of=datetime.now(UTC),
            ),
        ])

        await self._seed_fx_rate("EUR", "USD", Decimal("1.085"))
        await self._seed_fx_rate("GBP", "USD", Decimal("1.25"))

        self.session.add_all([
            Todo(
                workspace_id=workspace_id,
                user_id=actor_id,
                title="buy groceries tomorrow",
                completed=False,
                due_date=today_date + timedelta(days=1),
            ),
            Todo(
                workspace_id=workspace_id,
                user_id=actor_id,
                title="review investing performance",
                completed=False,
                due_date=today_date + timedelta(days=2),
            ),
            Notification(
                workspace_id=workspace_id,
                user_id=actor_id,
                category="general",
                severity="info",
                title="Demo Reset",
                body="Welcome to your Lifestack workspace!",
                is_read=False,
            ),
        ])

    async def _seed_fx_rate(self, base_currency: str, quote_currency: str, rate: Decimal) -> None:
        existing = await self.session.execute(
            select(FxRate).where(
                FxRate.base_currency_code == base_currency,
                FxRate.quote_currency_code == quote_currency,
                FxRate.source == "ECB",
            )
        )
        if existing.scalars().first():
            return
        self.session.add(
            FxRate(
                base_currency_code=base_currency,
                quote_currency_code=quote_currency,
                rate=rate,
                as_of=datetime.now(UTC),
                fetched_at=datetime.now(UTC),
                source="ECB",
            )
        )

    async def _ensure_demo_currencies(self, workspace_id: int) -> None:
        for code, name, symbol in [
            ("USD", "US Dollar", "$"),
            ("GBP", "Pound Sterling", "GBP"),
            ("EUR", "Euro", "EUR"),
        ]:
            existing_currency = (
                await self.session.execute(select(Currency).where(Currency.code == code))
            ).scalar_one_or_none()
            if existing_currency is None:
                self.session.add(Currency(code=code, name=name, symbol=symbol, minor_unit=2))

            existing_workspace_currency = (
                await self.session.execute(
                    select(WorkspaceCurrency).where(
                        WorkspaceCurrency.workspace_id == workspace_id,
                        WorkspaceCurrency.currency_code == code,
                    )
                )
            ).scalar_one_or_none()
            if existing_workspace_currency is None:
                self.session.add(WorkspaceCurrency(workspace_id=workspace_id, currency_code=code))
        await self.session.flush()
