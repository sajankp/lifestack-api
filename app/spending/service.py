import calendar
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, func, select

from app.core.audit import AuditLogger
from app.core.exceptions import (
    CategoryInUseError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.core.pagination import DEFAULT_LIMIT
from app.finance.repository import AccountRepository
from app.spending.models import (
    RecurringTransaction,
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
    TransactionSourceType,
    TransactionType,
)
from app.spending.repository import (
    BudgetRepository,
    CategoryRepository,
    RecurringTransactionRepository,
    TransactionRepository,
)
from app.spending.schemas import (
    BudgetCreate,
    BudgetUpdate,
    CategoryCreate,
    CategoryUpdate,
    RecurringTransactionCreate,
    RecurringTransactionUpdate,
    SpendingTrendPoint,
    SpendingTrendResponse,
    TransactionCreate,
    TransactionUpdate,
    UpcomingPreviewResponse,
    UpcomingTransactionItem,
)

# Default system categories seeded during registration
DEFAULT_CATEGORIES: list[dict] = [
    {"name": "Food & Dining", "icon": "🍽️", "color": "#FF6B6B"},
    {"name": "Transport", "icon": "🚗", "color": "#4ECDC4"},
    {"name": "Housing", "icon": "🏠", "color": "#45B7D1"},
    {"name": "Health", "icon": "💊", "color": "#96CEB4"},
    {"name": "Entertainment", "icon": "🎬", "color": "#FFEAA7"},
    {"name": "Shopping", "icon": "🛍️", "color": "#DDA0DD"},
    {"name": "Income", "icon": "💰", "color": "#98FB98"},
    {"name": "Other", "icon": "📦", "color": "#D3D3D3"},
]


def _normalize(name: str) -> str:
    return name.strip().lower()


def _snapshot_category(category: SpendingCategory) -> dict:
    return {
        "name": category.name,
        "color": category.color,
        "icon": category.icon,
        "is_system": category.is_system,
    }


def _snapshot_transaction(transaction: SpendingTransaction) -> dict:
    return {
        "category_id": transaction.category_id,
        "account_id": transaction.account_id,
        "amount": str(transaction.amount) if transaction.amount is not None else None,
        "type": transaction.type,
        "occurred_at": transaction.occurred_at.isoformat() if transaction.occurred_at else None,
        "description": transaction.description,
        "wallet_name": transaction.wallet_name,
        "labels": transaction.labels,
        "source_type": transaction.source_type,
        "source_ref": transaction.source_ref,
    }


def _snapshot_budget(budget: SpendingBudget) -> dict:
    return {
        "category_id": budget.category_id,
        "amount": str(budget.amount) if budget.amount is not None else None,
        "month_start": budget.month_start.isoformat() if budget.month_start else None,
    }


class CategoryService:
    def __init__(self, repository: CategoryRepository):
        self.repository = repository

    async def list_categories(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[SpendingCategory], int]:
        return await self.repository.get_all(workspace_id, limit, offset)

    async def get_category(self, workspace_id: int, public_id: uuid.UUID) -> SpendingCategory:
        category = await self.repository.get_by_public_id(workspace_id, public_id)
        if not category:
            raise NotFoundError(detail=f"Category with id {public_id} not found in this workspace")
        return category

    async def create_category(
        self,
        workspace_id: int,
        category_in: CategoryCreate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> SpendingCategory:
        normalized = _normalize(category_in.name)
        existing = await self.repository.get_by_normalized_name(workspace_id, normalized)
        if existing:
            raise ConflictError(
                detail=f"A category named '{category_in.name}' already exists in this workspace"
            )
        category = SpendingCategory(
            workspace_id=workspace_id,
            name=category_in.name,
            normalized_name=normalized,
            color=category_in.color,
            icon=category_in.icon,
            is_system=False,
        )
        category = await self.repository.create(category)

        if audit_logger and actor_id is not None:
            after_snap = _snapshot_category(category)
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="create",
                module="spending",
                entity_type="spending_category",
                entity_id=category.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(category.public_id),
                    "before": None,
                    "after": after_snap,
                    "changed_fields": list(after_snap.keys()),
                },
            )
        return category

    async def update_category(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        category_in: CategoryUpdate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> SpendingCategory:
        category = await self.get_category(workspace_id, public_id)
        before_snap = _snapshot_category(category)

        update_data = category_in.model_dump(exclude_unset=True)
        if not update_data:
            return category

        if "name" in update_data:
            new_normalized = _normalize(update_data["name"])
            if new_normalized != category.normalized_name:
                existing = await self.repository.get_by_normalized_name(
                    workspace_id, new_normalized
                )
                if existing:
                    raise ConflictError(
                        detail=f"A category named '{update_data['name']}' already exists"
                    )
            update_data["normalized_name"] = new_normalized

        for key, value in update_data.items():
            setattr(category, key, value)
        category.updated_at = datetime.now(UTC)
        category = await self.repository.save(category)

        if audit_logger and actor_id is not None:
            after_snap = _snapshot_category(category)
            changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="update",
                module="spending",
                entity_type="spending_category",
                entity_id=category.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(category.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )
        return category

    async def delete_category(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        category = await self.get_category(workspace_id, public_id)
        if category.is_system:
            raise ForbiddenError(detail="System categories cannot be deleted")
        if await self.repository.has_usage(category.id):  # type: ignore[arg-type]
            raise CategoryInUseError(
                detail="Cannot delete a category that is in use by transactions, budgets, or recurring rules"
            )
        before_snap = _snapshot_category(category)
        await self.repository.delete(category)

        if audit_logger and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="delete",
                module="spending",
                entity_type="spending_category",
                entity_id=category.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(category.public_id),
                    "before": before_snap,
                    "after": None,
                    "changed_fields": [],
                },
            )

    async def provision_default_categories(self, workspace_id: int) -> None:
        """Insert system categories for a newly created workspace."""
        categories = [
            SpendingCategory(
                workspace_id=workspace_id,
                name=cat["name"],
                normalized_name=_normalize(cat["name"]),
                is_system=True,
                color=cat.get("color"),
                icon=cat.get("icon"),
            )
            for cat in DEFAULT_CATEGORIES
        ]
        await self.repository.create_many(categories)


class TransactionService:
    def __init__(
        self,
        transaction_repo: TransactionRepository,
        category_repo: CategoryRepository,
        account_repo: AccountRepository,
    ):
        self.transaction_repo = transaction_repo
        self.category_repo = category_repo
        self.account_repo = account_repo

    async def _resolve_category(
        self, workspace_id: int, category_public_id: uuid.UUID
    ) -> SpendingCategory:
        """Resolve a category by public_id, enforcing workspace boundary."""
        category = await self.category_repo.get_by_public_id(workspace_id, category_public_id)
        if not category:
            raise NotFoundError(
                detail=(
                    f"Category with id {category_public_id} not found in this workspace. "
                    "Cross-workspace category references are not permitted."
                )
            )
        return category

    async def _resolve_account_id(
        self, workspace_id: int, account_public_id: uuid.UUID | None
    ) -> int | None:
        if account_public_id is None:
            return None
        account = await self.account_repo.get_by_public_id(workspace_id, account_public_id)
        if not account:
            raise NotFoundError(
                detail=f"Account with id {account_public_id} not found in this workspace"
            )
        return account.id

    async def list_transactions(
        self,
        workspace_id: int,
        category_public_id: uuid.UUID | None = None,
        account_public_id: uuid.UUID | None = None,
        type_filter: TransactionType | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[Sequence[SpendingTransaction], int]:
        category_id: int | None = None
        if category_public_id is not None:
            cat = await self._resolve_category(workspace_id, category_public_id)
            category_id = cat.id  # type: ignore[assignment]
        account_id = await self._resolve_account_id(workspace_id, account_public_id)

        return await self.transaction_repo.get_all(
            workspace_id,
            category_id=category_id,
            account_id=account_id,
            type_filter=type_filter,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )

    async def get_sum_by_type(
        self,
        workspace_id: int,
        type_filter: str,
        from_date: datetime,
        to_date: datetime,
        category_public_id: uuid.UUID | None = None,
        account_public_id: uuid.UUID | None = None,
    ) -> Decimal:
        category_id: int | None = None
        if category_public_id is not None:
            cat = await self._resolve_category(workspace_id, category_public_id)
            category_id = cat.id  # type: ignore[assignment]
        account_id = await self._resolve_account_id(workspace_id, account_public_id)
        return await self.transaction_repo.get_sum_by_type(
            workspace_id,
            type_filter,
            from_date,
            to_date,
            category_id=category_id,
            account_id=account_id,
        )

    async def get_category_totals(
        self,
        workspace_id: int,
        from_date: datetime,
        to_date: datetime,
        type_filter: TransactionType | None = None,
        account_public_id: uuid.UUID | None = None,
    ) -> dict[int, Decimal]:
        account_id = await self._resolve_account_id(workspace_id, account_public_id)
        rows = await self.transaction_repo.get_category_totals(
            workspace_id,
            from_date,
            to_date,
            type_filter=type_filter,
            account_id=account_id,
        )
        return dict(rows)

    async def get_transaction(self, workspace_id: int, public_id: uuid.UUID) -> SpendingTransaction:
        transaction = await self.transaction_repo.get_by_public_id(workspace_id, public_id)
        if not transaction:
            raise NotFoundError(
                detail=f"Transaction with id {public_id} not found in this workspace"
            )
        return transaction

    async def create_transaction(
        self,
        user_id: int,
        workspace_id: int,
        tx_in: TransactionCreate,
        audit_logger: AuditLogger | None = None,
    ) -> SpendingTransaction:
        category = await self._resolve_category(workspace_id, tx_in.category_id)
        account_id: int | None = None
        if tx_in.account_id is not None:
            account = await self.account_repo.get_by_public_id(workspace_id, tx_in.account_id)
            if not account:
                raise NotFoundError(
                    detail=(
                        f"Account with id {tx_in.account_id} not found in this workspace. "
                        "Cross-workspace account references are not permitted."
                    )
                )
            account_id = account.id  # type: ignore[assignment]
        transaction = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=user_id,
            category_id=category.id,  # type: ignore[assignment]
            account_id=account_id,
            amount=tx_in.amount,
            type=tx_in.type,
            occurred_at=tx_in.occurred_at,
            description=tx_in.description,
            wallet_name=tx_in.wallet_name,
            labels=tx_in.labels,
            source_type=TransactionSourceType.manual,
        )
        transaction = await self.transaction_repo.create(transaction)

        if audit_logger:
            after_snap = _snapshot_transaction(transaction)
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="create",
                module="spending",
                entity_type="spending_transaction",
                entity_id=transaction.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(transaction.public_id),
                    "before": None,
                    "after": after_snap,
                    "changed_fields": list(after_snap.keys()),
                },
            )
        return transaction

    async def update_transaction(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        tx_in: TransactionUpdate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> SpendingTransaction:
        transaction = await self.get_transaction(workspace_id, public_id)
        before_snap = _snapshot_transaction(transaction)

        update_data = tx_in.model_dump(exclude_unset=True)
        if not update_data:
            return transaction

        if "category_id" in update_data:
            cat = await self._resolve_category(workspace_id, update_data.pop("category_id"))
            transaction.category_id = cat.id  # type: ignore[assignment]
        if "account_id" in update_data:
            account_public_id = update_data.pop("account_id")
            if account_public_id is None:
                transaction.account_id = None
            else:
                account = await self.account_repo.get_by_public_id(workspace_id, account_public_id)
                if not account:
                    raise NotFoundError(
                        detail=(
                            f"Account with id {account_public_id} not found in this workspace. "
                            "Cross-workspace account references are not permitted."
                        )
                    )
                transaction.account_id = account.id  # type: ignore[assignment]

        for key, value in update_data.items():
            setattr(transaction, key, value)
        transaction.updated_at = datetime.now(UTC)
        transaction = await self.transaction_repo.save(transaction)

        if audit_logger and actor_id is not None:
            after_snap = _snapshot_transaction(transaction)
            changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="update",
                module="spending",
                entity_type="spending_transaction",
                entity_id=transaction.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(transaction.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )
        return transaction

    async def delete_transaction(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        transaction = await self.get_transaction(workspace_id, public_id)
        before_snap = _snapshot_transaction(transaction)
        await self.transaction_repo.delete(transaction)

        if audit_logger and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="delete",
                module="spending",
                entity_type="spending_transaction",
                entity_id=transaction.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(transaction.public_id),
                    "before": before_snap,
                    "after": None,
                    "changed_fields": [],
                },
            )

    async def get_monthly_trends(
        self, workspace_id: int, from_month: date, to_month: date
    ) -> SpendingTrendResponse:
        if from_month > to_month:
            raise ValidationError(detail="from_month cannot be after to_month")
        start_dt = datetime(from_month.year, from_month.month, 1, tzinfo=UTC)
        if to_month.month == 12:
            end_dt = datetime(to_month.year + 1, 1, 1, tzinfo=UTC)
        else:
            end_dt = datetime(to_month.year, to_month.month + 1, 1, tzinfo=UTC)

        month_bucket = func.date_trunc("month", SpendingTransaction.occurred_at)
        rows = (
            await self.transaction_repo.session.execute(
                select(
                    month_bucket.label("month"),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    SpendingTransaction.type == TransactionType.income.value,
                                    SpendingTransaction.amount,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("income"),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    SpendingTransaction.type == TransactionType.expense.value,
                                    SpendingTransaction.amount,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("expense"),
                    func.count(SpendingTransaction.id).label("count"),
                )
                .where(
                    SpendingTransaction.workspace_id == workspace_id,
                    SpendingTransaction.occurred_at >= start_dt,
                    SpendingTransaction.occurred_at < end_dt,
                )
                .group_by(month_bucket)
                .order_by(month_bucket)
            )
        ).all()
        data_by_month: dict[str, SpendingTrendPoint] = {}
        for row in rows:
            month_str = row.month.strftime("%Y-%m")
            income = Decimal(row.income)
            expense = Decimal(row.expense)
            data_by_month[month_str] = SpendingTrendPoint(
                month=month_str,
                total_income=income,
                total_expense=expense,
                net=income - expense,
                transaction_count=int(row.count),
            )

        cursor = from_month.replace(day=1)
        end = to_month.replace(day=1)
        points: list[SpendingTrendPoint] = []
        while cursor <= end:
            month_str = cursor.strftime("%Y-%m")
            points.append(
                data_by_month.get(
                    month_str,
                    SpendingTrendPoint(
                        month=month_str,
                        total_income=Decimal("0"),
                        total_expense=Decimal("0"),
                        net=Decimal("0"),
                        transaction_count=0,
                    ),
                )
            )
            if cursor.month == 12:
                cursor = cursor.replace(year=cursor.year + 1, month=1)
            else:
                cursor = cursor.replace(month=cursor.month + 1)
        return SpendingTrendResponse(
            from_month=from_month.strftime("%Y-%m"),
            to_month=to_month.strftime("%Y-%m"),
            months=points,
        )


class BudgetService:
    def __init__(
        self,
        budget_repo: BudgetRepository,
        category_repo: CategoryRepository,
    ):
        self.budget_repo = budget_repo
        self.category_repo = category_repo

    async def _resolve_category(
        self, workspace_id: int, category_public_id: uuid.UUID
    ) -> SpendingCategory:
        category = await self.category_repo.get_by_public_id(workspace_id, category_public_id)
        if not category:
            raise NotFoundError(
                detail=(
                    f"Category with id {category_public_id} not found in this workspace. "
                    "Cross-workspace category references are not permitted."
                )
            )
        return category

    async def list_budgets(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        month_start: date | None = None,
    ) -> tuple[Sequence[SpendingBudget], int]:
        return await self.budget_repo.get_all(workspace_id, limit, offset, month_start=month_start)

    async def get_month_total_budget(self, workspace_id: int, month_start: date) -> Decimal:
        return await self.budget_repo.get_month_total(workspace_id, month_start)

    async def get_budget(self, workspace_id: int, public_id: uuid.UUID) -> SpendingBudget:
        budget = await self.budget_repo.get_by_public_id(workspace_id, public_id)
        if not budget:
            raise NotFoundError(detail=f"Budget with id {public_id} not found in this workspace")
        return budget

    async def create_budget(
        self,
        workspace_id: int,
        budget_in: BudgetCreate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> SpendingBudget:
        category = await self._resolve_category(workspace_id, budget_in.category_id)

        existing = await self.budget_repo.get_by_category_and_month(
            workspace_id,
            category.id,
            budget_in.month_start,  # type: ignore[arg-type]
        )
        if existing:
            raise ConflictError(
                detail=(
                    f"A budget for category '{category.name}' and month "
                    f"{budget_in.month_start} already exists. Use PATCH to update it."
                )
            )

        budget = SpendingBudget(
            workspace_id=workspace_id,
            category_id=category.id,  # type: ignore[assignment]
            amount=budget_in.amount,
            month_start=budget_in.month_start,
        )
        budget = await self.budget_repo.create(budget)

        if audit_logger and actor_id is not None:
            after_snap = _snapshot_budget(budget)
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="create",
                module="spending",
                entity_type="spending_budget",
                entity_id=budget.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(budget.public_id),
                    "before": None,
                    "after": after_snap,
                    "changed_fields": list(after_snap.keys()),
                },
            )
        return budget

    async def update_budget(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        budget_in: BudgetUpdate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> SpendingBudget:
        budget = await self.get_budget(workspace_id, public_id)
        before_snap = _snapshot_budget(budget)

        budget.amount = budget_in.amount
        budget.updated_at = datetime.now(UTC)
        budget = await self.budget_repo.save(budget)

        if audit_logger and actor_id is not None:
            after_snap = _snapshot_budget(budget)
            changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="update",
                module="spending",
                entity_type="spending_budget",
                entity_id=budget.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(budget.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )
        return budget


def _advance_due_date(current: date, frequency: str, interval: int) -> date:
    if frequency == "daily":
        return current + timedelta(days=interval)
    if frequency == "weekly":
        return current + timedelta(weeks=interval)
    if frequency == "yearly":
        try:
            return current.replace(year=current.year + interval)
        except ValueError:
            return date(current.year + interval, 2, 28)
    # monthly default
    month = current.month - 1 + interval
    year = current.year + month // 12
    month = month % 12 + 1
    day = min(current.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


class RecurringTransactionService:
    def __init__(
        self,
        recurring_repo: RecurringTransactionRepository,
        tx_repo: TransactionRepository,
        category_repo: CategoryRepository,
    ):
        self.recurring_repo = recurring_repo
        self.tx_repo = tx_repo
        self.category_repo = category_repo

    async def list_recurring(
        self, workspace_id: int, is_active: bool | None, limit: int, offset: int
    ) -> tuple[Sequence[RecurringTransaction], int]:
        return await self.recurring_repo.get_all(workspace_id, is_active, limit, offset)

    async def create_recurring(
        self, workspace_id: int, user_id: int, payload: RecurringTransactionCreate
    ) -> RecurringTransaction:
        category = await self.category_repo.get_by_public_id(workspace_id, payload.category_id)
        if not category:
            raise NotFoundError(
                detail=f"Category with id {payload.category_id} not found in this workspace"
            )
        if payload.end_date and payload.end_date < payload.anchor_date:
            raise ValidationError(detail="end_date cannot be before anchor_date")
        recurring = RecurringTransaction(
            workspace_id=workspace_id,
            user_id=user_id,
            category_id=category.id,  # type: ignore[arg-type]
            amount=payload.amount,
            type=payload.type,
            description=payload.description,
            frequency=payload.frequency,
            interval=payload.interval,
            anchor_date=payload.anchor_date,
            next_due_date=payload.anchor_date,
            end_date=payload.end_date,
        )
        return await self.recurring_repo.create(recurring)

    async def get_recurring(self, workspace_id: int, public_id: uuid.UUID) -> RecurringTransaction:
        recurring = await self.recurring_repo.get_by_public_id(workspace_id, public_id)
        if not recurring:
            raise NotFoundError(detail=f"Recurring transaction with id {public_id} not found")
        return recurring

    async def update_recurring(
        self, workspace_id: int, public_id: uuid.UUID, payload: RecurringTransactionUpdate
    ) -> RecurringTransaction:
        recurring = await self.get_recurring(workspace_id, public_id)
        update_data = payload.model_dump(exclude_unset=True)
        if "frequency" in update_data and update_data["frequency"] not in {
            "daily",
            "weekly",
            "monthly",
            "yearly",
        }:
            raise ValidationError(
                detail="Invalid frequency. Use daily, weekly, monthly, or yearly."
            )
        new_end = update_data.get("end_date", recurring.end_date)
        if new_end and new_end < recurring.anchor_date:
            raise ValidationError(detail="end_date cannot be before anchor_date")
        for key, value in update_data.items():
            setattr(recurring, key, value)
        recurring.updated_at = datetime.now(UTC)
        return await self.recurring_repo.save(recurring)

    async def deactivate_recurring(self, workspace_id: int, public_id: uuid.UUID) -> None:
        recurring = await self.get_recurring(workspace_id, public_id)
        recurring.is_active = False
        recurring.updated_at = datetime.now(UTC)
        await self.recurring_repo.save(recurring)

    async def upcoming_preview(
        self, workspace_id: int, days: int, category_repo: CategoryRepository
    ) -> UpcomingPreviewResponse:
        """Project upcoming transactions for the next N days without writing to the DB."""

        if days < 1 or days > 365:
            raise ValidationError(detail="days must be between 1 and 365")

        today = datetime.now(UTC).date()
        horizon = today + timedelta(days=days)

        # Fetch all active recurring rules for this workspace
        all_active, _ = await self.recurring_repo.get_all(
            workspace_id, is_active=True, limit=10000, offset=0
        )

        # Build category public_id lookup
        cats, _ = await category_repo.get_all(workspace_id, limit=10000, offset=0)
        cat_pub_id: dict[int, uuid.UUID] = {c.id: c.public_id for c in cats}  # type: ignore[union-attr]

        items: list[UpcomingTransactionItem] = []
        for recurrence in all_active:
            # Start projecting from next_due_date
            projected = recurrence.next_due_date
            iterations = 0
            while projected <= horizon and iterations < 500:
                iterations += 1
                if recurrence.end_date and projected > recurrence.end_date:
                    break
                if projected >= today:
                    cat_public_id = cat_pub_id.get(recurrence.category_id)
                    if cat_public_id is None:
                        break
                    items.append(
                        UpcomingTransactionItem(
                            recurring_public_id=recurrence.public_id,
                            category_id=cat_public_id,
                            amount=recurrence.amount,
                            type=recurrence.type,
                            description=recurrence.description,
                            projected_date=projected,
                            frequency=recurrence.frequency,
                            interval=recurrence.interval,
                        )
                    )
                projected = _advance_due_date(projected, recurrence.frequency, recurrence.interval)

        items.sort(key=lambda x: x.projected_date)
        return UpcomingPreviewResponse(
            days=days,
            from_date=today,
            to_date=horizon,
            items=items,
        )
