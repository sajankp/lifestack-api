import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from app.core.audit import AuditLogger
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.pagination import DEFAULT_LIMIT
from app.finance.repository import (
    AccountRepository,
    CurrencyRepository,
    FinanceSettingRepository,
    FxRateRepository,
)
from app.investing.models import CashBalance, Holding
from app.investing.repository import CashBalanceRepository, HoldingRepository
from app.investing.schemas import (
    CashBalanceCreate,
    CashBalanceUpdate,
    HoldingCreate,
    HoldingUpdate,
    InvestingSummaryResponse,
)


def _snapshot_holding(holding: Holding) -> dict:
    return {
        "symbol": holding.symbol,
        "account_name": holding.account_name,
        "quantity": str(holding.quantity),
        "avg_cost": str(holding.avg_cost),
        "currency": holding.currency,
    }


def _snapshot_cash_balance(cash: CashBalance) -> dict:
    return {
        "account_name": cash.account_name,
        "balance": str(cash.balance),
        "currency": cash.currency,
        "as_of": cash.as_of.isoformat() if hasattr(cash.as_of, "isoformat") else str(cash.as_of),
    }


class HoldingService:
    def __init__(
        self,
        repository: HoldingRepository,
        account_repo: AccountRepository | None = None,
        currency_repo: CurrencyRepository | None = None,
    ):
        self.repository = repository
        self.account_repo = account_repo
        self.currency_repo = currency_repo

    async def _validate_refs(self, workspace_id: int, account_name: str, currency: str) -> None:
        if self.account_repo is not None:
            account = await self.account_repo.get_by_name(workspace_id, account_name)
            if not account or not account.is_active:
                raise ValidationError(
                    detail=f"Account '{account_name}' is not found in this workspace"
                )
        if self.currency_repo is not None:
            code = currency.upper()
            currency_row = await self.currency_repo.get_by_code(code)
            if not currency_row or not currency_row.is_active:
                raise ValidationError(detail=f"Unsupported currency code '{code}'")
            enabled = await self.currency_repo.is_enabled_for_workspace(workspace_id, code)
            if not enabled:
                raise ValidationError(detail=f"Currency '{code}' is not enabled for this workspace")

    async def list_holdings(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[Holding], int]:
        return await self.repository.get_all(workspace_id, limit, offset)

    async def get_holding(self, workspace_id: int, public_id: uuid.UUID) -> Holding:
        holding = await self.repository.get_by_public_id(workspace_id, public_id)
        if not holding:
            raise NotFoundError(detail=f"Holding with id {public_id} not found in this workspace")
        return holding

    async def create_holding(
        self,
        user_id: int,
        workspace_id: int,
        holding_in: HoldingCreate,
        audit_logger: AuditLogger | None = None,
    ) -> Holding:
        existing = await self.repository.get_by_unique_key(
            workspace_id, holding_in.symbol, holding_in.account_name
        )
        if existing:
            raise ConflictError(
                detail=("A holding already exists for this symbol/account in this workspace")
            )
        await self._validate_refs(workspace_id, holding_in.account_name, holding_in.currency)

        holding = Holding(
            workspace_id=workspace_id,
            user_id=user_id,
            symbol=holding_in.symbol,
            account_name=holding_in.account_name,
            quantity=holding_in.quantity,
            avg_cost=holding_in.avg_cost,
            currency=holding_in.currency,
        )
        holding = await self.repository.create(holding)

        if audit_logger:
            after_snap = _snapshot_holding(holding)
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="create",
                module="investing",
                entity_type="holding",
                entity_id=holding.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(holding.public_id),
                    "before": None,
                    "after": after_snap,
                    "changed_fields": list(after_snap.keys()),
                },
            )

        return holding

    async def update_holding(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        holding_in: HoldingUpdate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> Holding:
        holding = await self.get_holding(workspace_id, public_id)
        before_snap = _snapshot_holding(holding)

        update_data = holding_in.model_dump(exclude_unset=True)
        if not update_data:
            return holding

        next_account_name = holding.account_name
        next_currency = holding.currency
        if "currency" in update_data and update_data["currency"] is not None:
            next_currency = update_data["currency"]
        await self._validate_refs(workspace_id, next_account_name, next_currency)

        for key, value in update_data.items():
            setattr(holding, key, value)
        holding.updated_at = datetime.now(UTC)
        holding = await self.repository.save(holding)

        if audit_logger and actor_id is not None:
            after_snap = _snapshot_holding(holding)
            changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="update",
                module="investing",
                entity_type="holding",
                entity_id=holding.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(holding.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )
        return holding

    async def delete_holding(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        holding = await self.get_holding(workspace_id, public_id)
        before_snap = _snapshot_holding(holding)
        await self.repository.delete(holding)

        if audit_logger and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="delete",
                module="investing",
                entity_type="holding",
                entity_id=holding.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(holding.public_id),
                    "before": before_snap,
                    "after": None,
                    "changed_fields": [],
                },
            )


class CashBalanceService:
    def __init__(
        self,
        repository: CashBalanceRepository,
        account_repo: AccountRepository | None = None,
        currency_repo: CurrencyRepository | None = None,
    ):
        self.repository = repository
        self.account_repo = account_repo
        self.currency_repo = currency_repo

    async def _validate_refs(self, workspace_id: int, account_name: str, currency: str) -> None:
        if self.account_repo is not None:
            account = await self.account_repo.get_by_name(workspace_id, account_name)
            if not account or not account.is_active:
                raise ValidationError(
                    detail=f"Account '{account_name}' is not found in this workspace"
                )
        if self.currency_repo is not None:
            code = currency.upper()
            currency_row = await self.currency_repo.get_by_code(code)
            if not currency_row or not currency_row.is_active:
                raise ValidationError(detail=f"Unsupported currency code '{code}'")
            enabled = await self.currency_repo.is_enabled_for_workspace(workspace_id, code)
            if not enabled:
                raise ValidationError(detail=f"Currency '{code}' is not enabled for this workspace")

    async def list_cash_balances(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[CashBalance], int]:
        return await self.repository.get_all(workspace_id, limit, offset)

    async def get_cash_balance(self, workspace_id: int, public_id: uuid.UUID) -> CashBalance:
        cash = await self.repository.get_by_public_id(workspace_id, public_id)
        if not cash:
            raise NotFoundError(
                detail=f"Cash balance with id {public_id} not found in this workspace"
            )
        return cash

    async def create_cash_balance(
        self,
        user_id: int,
        workspace_id: int,
        cash_in: CashBalanceCreate,
        audit_logger: AuditLogger | None = None,
    ) -> CashBalance:
        await self._validate_refs(workspace_id, cash_in.account_name, cash_in.currency)
        cash = CashBalance(
            workspace_id=workspace_id,
            user_id=user_id,
            account_name=cash_in.account_name,
            balance=cash_in.balance,
            currency=cash_in.currency,
            as_of=cash_in.as_of,
        )
        cash = await self.repository.create(cash)

        if audit_logger:
            after_snap = _snapshot_cash_balance(cash)
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="create",
                module="investing",
                entity_type="cash_balance",
                entity_id=cash.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(cash.public_id),
                    "before": None,
                    "after": after_snap,
                    "changed_fields": list(after_snap.keys()),
                },
            )
        return cash

    async def update_cash_balance(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        cash_in: CashBalanceUpdate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> CashBalance:
        cash = await self.get_cash_balance(workspace_id, public_id)
        before_snap = _snapshot_cash_balance(cash)

        update_data = cash_in.model_dump(exclude_unset=True)
        if not update_data:
            return cash

        next_account_name = cash.account_name
        next_currency = cash.currency
        if "currency" in update_data and update_data["currency"] is not None:
            next_currency = update_data["currency"]
        await self._validate_refs(workspace_id, next_account_name, next_currency)

        for key, value in update_data.items():
            setattr(cash, key, value)
        cash.updated_at = datetime.now(UTC)
        cash = await self.repository.save(cash)

        if audit_logger and actor_id is not None:
            after_snap = _snapshot_cash_balance(cash)
            changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="update",
                module="investing",
                entity_type="cash_balance",
                entity_id=cash.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(cash.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )
        return cash

    async def delete_cash_balance(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        cash = await self.get_cash_balance(workspace_id, public_id)
        before_snap = _snapshot_cash_balance(cash)
        await self.repository.delete(cash)

        if audit_logger and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="delete",
                module="investing",
                entity_type="cash_balance",
                entity_id=cash.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(cash.public_id),
                    "before": before_snap,
                    "after": None,
                    "changed_fields": [],
                },
            )


class InvestingSummaryService:
    def __init__(
        self,
        holding_repo: HoldingRepository,
        cash_repo: CashBalanceRepository,
        finance_setting_repo: FinanceSettingRepository | None = None,
        fx_rate_repo: FxRateRepository | None = None,
    ):
        self.holding_repo = holding_repo
        self.cash_repo = cash_repo
        self.finance_setting_repo = finance_setting_repo
        self.fx_rate_repo = fx_rate_repo

    async def get_summary(self, workspace_id: int) -> InvestingSummaryResponse:
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
        cash_balances, _ = await self.cash_repo.get_all(workspace_id, limit=10000, offset=0)

        breakdown: dict[str, Decimal] = {}

        for holding in holdings:
            value = holding.quantity * holding.avg_cost
            curr = holding.currency.upper()
            breakdown[curr] = breakdown.get(curr, Decimal("0")) + value

        for cash in cash_balances:
            curr = cash.currency.upper()
            breakdown[curr] = breakdown.get(curr, Decimal("0")) + cash.balance

        used_currencies = sorted(breakdown.keys())
        reporting_currency: str | None = None
        if self.finance_setting_repo is not None:
            settings = await self.finance_setting_repo.get_by_workspace(workspace_id)
            if settings and settings.reporting_currency_code:
                reporting_currency = settings.reporting_currency_code.upper()

        # No data in workspace -> trivially valued as zero.
        if not used_currencies:
            return InvestingSummaryResponse(
                portfolio_value=Decimal("0"),
                holdings_count=0,
                cash_total=Decimal("0"),
                currency_breakdown={},
                daily_change=None,
                reporting_currency=reporting_currency,
                valuation_status="empty",
                fx_as_of=None,
            )

        if reporting_currency is None:
            # If there is only one currency, we can report deterministic native totals.
            if len(used_currencies) == 1:
                currency = used_currencies[0]
                portfolio_value = Decimal("0")
                cash_total = Decimal("0")
                for holding in holdings:
                    portfolio_value += holding.quantity * holding.avg_cost
                for cash in cash_balances:
                    cash_total += cash.balance
                return InvestingSummaryResponse(
                    portfolio_value=portfolio_value,
                    holdings_count=len(holdings),
                    cash_total=cash_total,
                    currency_breakdown=breakdown,
                    daily_change=None,
                    reporting_currency=currency,
                    valuation_status="single_currency_native",
                    fx_as_of=None,
                )

            # Multi-currency portfolio without configured reporting currency.
            return InvestingSummaryResponse(
                portfolio_value=None,
                holdings_count=len(holdings),
                cash_total=None,
                currency_breakdown=breakdown,
                daily_change=None,
                reporting_currency=None,
                valuation_status="multi_currency_unconverted",
                fx_as_of=None,
            )

        if any(curr != reporting_currency for curr in used_currencies):
            if self.fx_rate_repo is None:
                return InvestingSummaryResponse(
                    portfolio_value=None,
                    holdings_count=len(holdings),
                    cash_total=None,
                    currency_breakdown=breakdown,
                    daily_change=None,
                    reporting_currency=reporting_currency,
                    valuation_status="conversion_required",
                    fx_as_of=None,
                )

            converted_portfolio = Decimal("0")
            converted_cash = Decimal("0")
            valuation_as_of = datetime.now(UTC)

            for holding in holdings:
                native_value = holding.quantity * holding.avg_cost
                curr = holding.currency.upper()
                if curr == reporting_currency:
                    converted_portfolio += native_value
                    continue
                rate_row = await self.fx_rate_repo.get_latest_rate(
                    curr, reporting_currency, as_of=valuation_as_of
                )
                if rate_row is None and curr != "USD" and reporting_currency != "USD":
                    base_to_usd = await self.fx_rate_repo.get_latest_rate(
                        curr, "USD", as_of=valuation_as_of
                    )
                    usd_to_quote = await self.fx_rate_repo.get_latest_rate(
                        "USD", reporting_currency, as_of=valuation_as_of
                    )
                    if base_to_usd and usd_to_quote:
                        converted_portfolio += (
                            native_value
                            * Decimal(str(base_to_usd.rate))
                            * Decimal(str(usd_to_quote.rate))
                        )
                        continue
                if rate_row is None:
                    return InvestingSummaryResponse(
                        portfolio_value=None,
                        holdings_count=len(holdings),
                        cash_total=None,
                        currency_breakdown=breakdown,
                        daily_change=None,
                        reporting_currency=reporting_currency,
                        valuation_status="conversion_required",
                        fx_as_of=None,
                    )
                converted_portfolio += native_value * Decimal(str(rate_row.rate))

            for cash in cash_balances:
                curr = cash.currency.upper()
                if curr == reporting_currency:
                    converted_cash += cash.balance
                    continue
                rate_row = await self.fx_rate_repo.get_latest_rate(
                    curr, reporting_currency, as_of=valuation_as_of
                )
                if rate_row is None and curr != "USD" and reporting_currency != "USD":
                    base_to_usd = await self.fx_rate_repo.get_latest_rate(
                        curr, "USD", as_of=valuation_as_of
                    )
                    usd_to_quote = await self.fx_rate_repo.get_latest_rate(
                        "USD", reporting_currency, as_of=valuation_as_of
                    )
                    if base_to_usd and usd_to_quote:
                        converted_cash += (
                            cash.balance
                            * Decimal(str(base_to_usd.rate))
                            * Decimal(str(usd_to_quote.rate))
                        )
                        continue
                if rate_row is None:
                    return InvestingSummaryResponse(
                        portfolio_value=None,
                        holdings_count=len(holdings),
                        cash_total=None,
                        currency_breakdown=breakdown,
                        daily_change=None,
                        reporting_currency=reporting_currency,
                        valuation_status="conversion_required",
                        fx_as_of=None,
                    )
                converted_cash += cash.balance * Decimal(str(rate_row.rate))

            return InvestingSummaryResponse(
                portfolio_value=converted_portfolio,
                holdings_count=len(holdings),
                cash_total=converted_cash,
                currency_breakdown=breakdown,
                daily_change=None,
                reporting_currency=reporting_currency,
                valuation_status="converted_available",
                fx_as_of=valuation_as_of,
            )

        portfolio_value = Decimal("0")
        cash_total = Decimal("0")
        for holding in holdings:
            portfolio_value += holding.quantity * holding.avg_cost
        for cash in cash_balances:
            cash_total += cash.balance

        return InvestingSummaryResponse(
            portfolio_value=portfolio_value,
            holdings_count=len(holdings),
            cash_total=cash_total,
            currency_breakdown=breakdown,
            daily_change=None,
            reporting_currency=reporting_currency,
            valuation_status="converted_available",
            fx_as_of=None,
        )
