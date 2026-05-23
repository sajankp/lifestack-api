import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from app.core.audit import AuditLogger
from app.core.exceptions import ConflictError, NotFoundError
from app.core.pagination import DEFAULT_LIMIT
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


class HoldingService:
    def __init__(self, repository: HoldingRepository):
        self.repository = repository

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
    def __init__(self, repository: CashBalanceRepository):
        self.repository = repository

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
    ) -> CashBalance:
        cash = CashBalance(
            workspace_id=workspace_id,
            user_id=user_id,
            account_name=cash_in.account_name,
            balance=cash_in.balance,
            currency=cash_in.currency,
            as_of=cash_in.as_of,
        )
        return await self.repository.create(cash)

    async def update_cash_balance(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        cash_in: CashBalanceUpdate,
    ) -> CashBalance:
        cash = await self.get_cash_balance(workspace_id, public_id)

        update_data = cash_in.model_dump(exclude_unset=True)
        if not update_data:
            return cash

        for key, value in update_data.items():
            setattr(cash, key, value)
        cash.updated_at = datetime.now(UTC)
        return await self.repository.save(cash)

    async def delete_cash_balance(self, workspace_id: int, public_id: uuid.UUID) -> None:
        cash = await self.get_cash_balance(workspace_id, public_id)
        await self.repository.delete(cash)


class InvestingSummaryService:
    def __init__(self, holding_repo: HoldingRepository, cash_repo: CashBalanceRepository):
        self.holding_repo = holding_repo
        self.cash_repo = cash_repo

    async def get_summary(self, workspace_id: int) -> InvestingSummaryResponse:
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
        cash_balances, _ = await self.cash_repo.get_all(workspace_id, limit=10000, offset=0)

        portfolio_value = Decimal("0")
        breakdown: dict[str, Decimal] = {}

        for holding in holdings:
            value = holding.quantity * holding.avg_cost
            portfolio_value += value
            breakdown[holding.currency] = breakdown.get(holding.currency, Decimal("0")) + value

        cash_total = Decimal("0")
        for cash in cash_balances:
            cash_total += cash.balance
            breakdown[cash.currency] = breakdown.get(cash.currency, Decimal("0")) + cash.balance

        return InvestingSummaryResponse(
            portfolio_value=portfolio_value,
            holdings_count=len(holdings),
            cash_total=cash_total,
            currency_breakdown=breakdown,
            daily_change=None,
        )
