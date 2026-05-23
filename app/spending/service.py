import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from app.core.audit import AuditLogger
from app.core.exceptions import (
    CategoryInUseError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from app.core.pagination import DEFAULT_LIMIT
from app.spending.models import (
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
    TransactionType,
)
from app.spending.repository import BudgetRepository, CategoryRepository, TransactionRepository
from app.spending.schemas import (
    BudgetCreate,
    BudgetUpdate,
    CategoryCreate,
    CategoryUpdate,
    TransactionCreate,
    TransactionUpdate,
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
        "amount": str(transaction.amount) if transaction.amount is not None else None,
        "type": transaction.type,
        "occurred_at": transaction.occurred_at.isoformat() if transaction.occurred_at else None,
        "description": transaction.description,
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
        if await self.repository.has_transactions(category.id):  # type: ignore[arg-type]
            raise CategoryInUseError(
                detail="Cannot delete a category that has transactions referencing it"
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
    ):
        self.transaction_repo = transaction_repo
        self.category_repo = category_repo

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

    async def list_transactions(
        self,
        workspace_id: int,
        category_public_id: uuid.UUID | None = None,
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

        return await self.transaction_repo.get_all(
            workspace_id,
            category_id=category_id,
            type_filter=type_filter,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )

    async def get_sum_by_type(
        self, workspace_id: int, type_filter: str, from_date: datetime, to_date: datetime
    ) -> Decimal:
        return await self.transaction_repo.get_sum_by_type(
            workspace_id, type_filter, from_date, to_date
        )

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
        transaction = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=user_id,
            category_id=category.id,  # type: ignore[assignment]
            amount=tx_in.amount,
            type=tx_in.type,
            occurred_at=tx_in.occurred_at,
            description=tx_in.description,
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
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[SpendingBudget], int]:
        return await self.budget_repo.get_all(workspace_id, limit, offset)

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
