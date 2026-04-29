import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

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
        self, workspace_id: int, category_in: CategoryCreate
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
        return await self.repository.create(category)

    async def update_category(
        self, workspace_id: int, public_id: uuid.UUID, category_in: CategoryUpdate
    ) -> SpendingCategory:
        category = await self.get_category(workspace_id, public_id)
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
        return await self.repository.save(category)

    async def delete_category(self, workspace_id: int, public_id: uuid.UUID) -> None:
        category = await self.get_category(workspace_id, public_id)
        if category.is_system:
            raise ForbiddenError(detail="System categories cannot be deleted")
        if await self.repository.has_transactions(category.id):  # type: ignore[arg-type]
            raise CategoryInUseError(
                detail="Cannot delete a category that has transactions referencing it"
            )
        await self.repository.delete(category)

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

    async def get_transaction(self, workspace_id: int, public_id: uuid.UUID) -> SpendingTransaction:
        transaction = await self.transaction_repo.get_by_public_id(workspace_id, public_id)
        if not transaction:
            raise NotFoundError(
                detail=f"Transaction with id {public_id} not found in this workspace"
            )
        return transaction

    async def create_transaction(
        self, user_id: int, workspace_id: int, tx_in: TransactionCreate
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
        return await self.transaction_repo.create(transaction)

    async def update_transaction(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        tx_in: TransactionUpdate,
    ) -> SpendingTransaction:
        transaction = await self.get_transaction(workspace_id, public_id)
        update_data = tx_in.model_dump(exclude_unset=True)
        if not update_data:
            return transaction

        if "category_id" in update_data:
            cat = await self._resolve_category(workspace_id, update_data.pop("category_id"))
            transaction.category_id = cat.id  # type: ignore[assignment]

        for key, value in update_data.items():
            setattr(transaction, key, value)
        transaction.updated_at = datetime.now(UTC)
        return await self.transaction_repo.save(transaction)

    async def delete_transaction(self, workspace_id: int, public_id: uuid.UUID) -> None:
        transaction = await self.get_transaction(workspace_id, public_id)
        await self.transaction_repo.delete(transaction)


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

    async def create_budget(self, workspace_id: int, budget_in: BudgetCreate) -> SpendingBudget:
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
        return await self.budget_repo.create(budget)

    async def update_budget(
        self, workspace_id: int, public_id: uuid.UUID, budget_in: BudgetUpdate
    ) -> SpendingBudget:
        budget = await self.get_budget(workspace_id, public_id)
        budget.amount = budget_in.amount
        budget.updated_at = datetime.now(UTC)
        return await self.budget_repo.save(budget)
