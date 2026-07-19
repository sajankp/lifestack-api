import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import case, desc, func, select

from app.imports.repository import ImportRepository
from app.spending.response_helpers import (
    budget_response,
    recurring_response,
    source_metadata_response,
    transaction_response,
)

if TYPE_CHECKING:
    from app.imports.models import ImportBatch

from app.core.audit import AuditLogger, snapshot_columns
from app.core.exceptions import (
    CategoryInUseError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.pagination import DEFAULT_LIMIT
from app.core.recurrence import advance_due_date, first_due_date, validate_recurrence_fields
from app.finance.models import Account
from app.finance.repository import AccountRepository, FinanceSettingRepository
from app.finance.statement_service import StatementService
from app.spending.models import (
    CategoryGroup,
    FinancialKpi,
    KpiMetricType,
    KpiWindow,
    RecurringTransaction,
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
    TransactionSort,
    TransactionSourceType,
    TransactionType,
)
from app.spending.repository import (
    BudgetRepository,
    CategoryGroupRepository,
    CategoryRepository,
    KpiRepository,
    LedgerRow,
    RecurringTransactionRepository,
    TransactionRepository,
)
from app.spending.schemas import (
    BudgetCreate,
    BudgetPerformanceItem,
    BudgetPerformanceResponse,
    BudgetPerformanceTotals,
    BudgetResponse,
    BudgetUpdate,
    CategoryBreakdownItem,
    CategoryBreakdownOther,
    CategoryBreakdownResponse,
    CategoryCreate,
    CategoryGroupCreate,
    CategoryGroupUpdate,
    CategoryUpdate,
    KpiCreate,
    KpiResponse,
    KpiUpdate,
    LedgerEntry,
    LedgerResponse,
    RecurringTransactionCreate,
    RecurringTransactionResponse,
    RecurringTransactionUpdate,
    SavingsRatePoint,
    SavingsRateResponse,
    SavingsRateTotals,
    SpendingTrendPoint,
    SpendingTrendResponse,
    TransactionCreate,
    TransactionResponse,
    TransactionUpdate,
    UpcomingPreviewResponse,
    UpcomingTransactionItem,
)


def add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    return date(year, month, 1)


def merge_budget_intervals(
    target_budgets: list[SpendingBudget],
    source_budgets: list[SpendingBudget],
) -> list[dict]:
    boundaries = set()
    for b in target_budgets + source_budgets:
        boundaries.add(b.start_month)
        if b.end_month is not None:
            boundaries.add(add_months(b.end_month, 1))

    sorted_boundaries = sorted(boundaries)
    if not sorted_boundaries:
        return []

    intervals = []
    for i in range(len(sorted_boundaries)):
        start = sorted_boundaries[i]
        end_month = None
        if i + 1 < len(sorted_boundaries):
            end_month = add_months(sorted_boundaries[i + 1], -1)

        # Find target amount
        target_amount = Decimal("0")
        for tb in target_budgets:
            if tb.start_month <= start and (tb.end_month is None or tb.end_month >= start):
                target_amount = tb.amount
                break

        # Find source amount
        source_amount = Decimal("0")
        for sb in source_budgets:
            if sb.start_month <= start and (sb.end_month is None or sb.end_month >= start):
                source_amount += sb.amount

        combined_amount = target_amount + source_amount
        if combined_amount > 0:
            intervals.append({
                "start_month": start,
                "end_month": end_month,
                "amount": combined_amount,
            })

    # Merge consecutive intervals with the same amount
    merged_intervals = []
    for interval in intervals:
        if not merged_intervals:
            merged_intervals.append(interval)
        else:
            last = merged_intervals[-1]
            is_consecutive = (
                last["end_month"] is not None
                and add_months(last["end_month"], 1) == interval["start_month"]
            )

            if is_consecutive and last["amount"] == interval["amount"]:
                last["end_month"] = interval["end_month"]
            else:
                merged_intervals.append(interval)

    return merged_intervals


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


_CATEGORY_AUDIT_FIELDS = (
    "name",
    "color",
    "icon",
    "is_system",
)

_TRANSACTION_AUDIT_FIELDS = (
    "category_id",
    "account_id",
    "amount",
    "type",
    "occurred_at",
    "description",
    "wallet_name",
    "labels",
    "source_type",
    "source_ref",
)

_BUDGET_AUDIT_FIELDS = (
    "category_id",
    "category_group_id",
    "amount",
    "start_month",
    "end_month",
)


def _snapshot_category(category: SpendingCategory) -> dict:
    return snapshot_columns(category, _CATEGORY_AUDIT_FIELDS)


def _snapshot_transaction(transaction: SpendingTransaction) -> dict:
    data = snapshot_columns(transaction, _TRANSACTION_AUDIT_FIELDS)
    # Convert Decimal and datetime fields for JSON serialization
    if data.get("amount") is not None:
        data["amount"] = str(data["amount"])
    if data.get("occurred_at") is not None:
        data["occurred_at"] = data["occurred_at"].isoformat()
    return data


def _snapshot_budget(budget: SpendingBudget) -> dict:
    data = snapshot_columns(budget, _BUDGET_AUDIT_FIELDS)
    # Convert Decimal and date fields for JSON serialization
    if data.get("amount") is not None:
        data["amount"] = str(data["amount"])
    if data.get("start_month") is not None:
        data["start_month"] = data["start_month"].isoformat()
    if data.get("end_month") is not None:
        data["end_month"] = data["end_month"].isoformat()
    return data


class CategoryGroupService:
    def __init__(
        self,
        repository: CategoryGroupRepository,
        budget_repo: BudgetRepository,
        category_repo: CategoryRepository,
    ):
        self.repository = repository
        self.budget_repo = budget_repo
        self.category_repo = category_repo

    async def list_groups(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[CategoryGroup], int]:
        return await self.repository.get_all(workspace_id, limit, offset)

    async def get_group(self, workspace_id: int, public_id: uuid.UUID) -> CategoryGroup:
        group = await self.repository.get_by_public_id(workspace_id, public_id)
        if not group:
            raise NotFoundError(
                detail=f"Category group with id {public_id} not found in this workspace"
            )
        return group

    async def create_group(
        self,
        workspace_id: int,
        group_in: CategoryGroupCreate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> CategoryGroup:
        normalized = _normalize(group_in.name)
        existing = await self.repository.get_by_normalized_name(workspace_id, normalized)
        if existing:
            raise ConflictError(detail="A category group with this name already exists")

        group = CategoryGroup(
            workspace_id=workspace_id,
            name=group_in.name,
            normalized_name=normalized,
            color=group_in.color,
            icon=group_in.icon,
        )
        group = await self.repository.create(group)

        if audit_logger and actor_id is not None:
            after_snap = {
                "name": group.name,
                "color": group.color,
                "icon": group.icon,
            }
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="create",
                module="spending",
                entity_type="category_group",
                entity_id=group.id,
                details={
                    "entity_public_id": str(group.public_id),
                    "before": None,
                    "after": after_snap,
                    "changed_fields": list(after_snap.keys()),
                },
            )
        return group

    async def update_group(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        group_in: CategoryGroupUpdate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> CategoryGroup:
        group = await self.get_group(workspace_id, public_id)
        before_snap = {
            "name": group.name,
            "color": group.color,
            "icon": group.icon,
        }

        changed_fields = []
        if group_in.name is not None and group_in.name != group.name:
            normalized = _normalize(group_in.name)
            existing = await self.repository.get_by_normalized_name(workspace_id, normalized)
            if existing and existing.id != group.id:
                raise ConflictError(detail="A category group with this name already exists")
            group.name = group_in.name
            group.normalized_name = normalized
            changed_fields.append("name")

        if group_in.color is not None and group_in.color != group.color:
            group.color = group_in.color
            changed_fields.append("color")

        if group_in.icon is not None and group_in.icon != group.icon:
            group.icon = group_in.icon
            changed_fields.append("icon")

        if changed_fields:
            group.updated_at = datetime.now(UTC)
            await self.repository.save(group)

            if audit_logger and actor_id is not None:
                await audit_logger.log(
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    action="update",
                    module="spending",
                    entity_type="category_group",
                    entity_id=group.id,
                    details={
                        "entity_public_id": str(group.public_id),
                        "before": before_snap,
                        "after": {
                            "name": group.name,
                            "color": group.color,
                            "icon": group.icon,
                        },
                        "changed_fields": changed_fields,
                    },
                )
        return group

    async def delete_group(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        group = await self.get_group(workspace_id, public_id)
        if await self.budget_repo.has_current_or_future_budget(workspace_id, group.id):
            raise ConflictError(
                detail="Cannot delete a category group that has a budget for the current or a future month"
            )
        before_snap = {
            "name": group.name,
            "color": group.color,
            "icon": group.icon,
        }
        await self.category_repo.ungroup_categories(workspace_id, group.id)
        await self.budget_repo.delete_by_group_id(workspace_id, group.id)
        await self.repository.delete(group)

        if audit_logger and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="delete",
                module="spending",
                entity_type="category_group",
                entity_id=group.id,
                details={
                    "entity_public_id": str(group.public_id),
                    "before": before_snap,
                    "after": None,
                    "changed_fields": [],
                },
            )


class CategoryService:
    def __init__(
        self,
        repository: CategoryRepository,
        budget_repo: BudgetRepository | None = None,
        group_repo: CategoryGroupRepository | None = None,
        session=None,
    ):
        self.repository = repository
        self.budget_repo = budget_repo
        self.group_repo = group_repo
        self.session = session

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
            raise ConflictError(detail=f"A category named '{category_in.name}' already exists")

        category = SpendingCategory(
            workspace_id=workspace_id,
            name=category_in.name,
            normalized_name=normalized,
            color=category_in.color,
            icon=category_in.icon,
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

        if "category_group_id" in update_data:
            uuid_val = update_data["category_group_id"]
            if uuid_val is not None:
                if self.group_repo is None:
                    raise ValidationError(
                        detail="Group repository is required to update group association"
                    )
                group = await self.group_repo.get_by_public_id(workspace_id, uuid_val)
                if not group:
                    raise NotFoundError(detail="Category group not found")
                category.category_group_id = group.id
            else:
                category.category_group_id = None
            del update_data["category_group_id"]

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

    async def merge_categories(
        self,
        workspace_id: int,
        target_public_id: uuid.UUID,
        source_public_ids: list[uuid.UUID],
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        if not source_public_ids:
            raise ValidationError(detail="source_public_ids cannot be empty")

        source_public_ids = list(set(source_public_ids))
        if target_public_id in source_public_ids:
            raise ValidationError(detail="target_public_id cannot be in source_public_ids")

        target_category = await self.get_category(workspace_id, target_public_id)
        source_categories = []
        for spid in source_public_ids:
            cat = await self.get_category(workspace_id, spid)
            source_categories.append(cat)

        target_id = target_category.id
        source_ids = [c.id for c in source_categories]

        if self.budget_repo is None or self.session is None:
            raise ValidationError(
                detail="Budget repository and session are required to merge categories"
            )

        # 1. Reassign transactions & recurring rules
        tx_count = await self.repository.reassign_transactions(workspace_id, source_ids, target_id)
        recurring_count = await self.repository.reassign_recurring_rules(
            workspace_id, source_ids, target_id
        )

        # 2. Merge range-based budgets
        all_budgets = await self.budget_repo.get_by_category_ids(
            workspace_id, [target_id] + source_ids
        )
        target_budgets = [b for b in all_budgets if b.category_id == target_id]
        source_budgets = [b for b in all_budgets if b.category_id in source_ids]

        merged_intervals = merge_budget_intervals(target_budgets, source_budgets)

        # Delete existing budgets
        await self.budget_repo.delete_by_category_ids(workspace_id, [target_id] + source_ids)

        # Create new merged budgets
        budgets_summed = 0
        budgets_repointed = 0
        for interval in merged_intervals:
            has_target = any(
                tb.start_month <= interval["start_month"]
                and (tb.end_month is None or tb.end_month >= interval["start_month"])
                for tb in target_budgets
            )
            has_source = any(
                sb.start_month <= interval["start_month"]
                and (sb.end_month is None or sb.end_month >= interval["start_month"])
                for sb in source_budgets
            )
            if has_target and has_source:
                budgets_summed += 1
            else:
                if has_source:
                    budgets_repointed += 1

            new_budget = SpendingBudget(
                workspace_id=workspace_id,
                category_id=target_id,
                category_group_id=None,
                amount=interval["amount"],
                start_month=interval["start_month"],
                end_month=interval["end_month"],
                source_type="manual",
            )
            self.session.add(new_budget)

        await self.session.flush()

        # Delete source categories
        for cat in source_categories:
            await self.repository.delete(cat)

        if audit_logger and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="merge",
                module="spending",
                entity_type="spending_category",
                entity_id=target_id,
                details={
                    "entity_public_id": str(target_public_id),
                    "before": None,
                    "after": None,
                    "changed_fields": [],
                    "source_public_ids": [str(c.public_id) for c in source_categories],
                    "target_public_id": str(target_public_id),
                    "transactions_moved": tx_count,
                    "recurring_rules_moved": recurring_count,
                    "budgets_summed": budgets_summed,
                    "budgets_repointed": budgets_repointed,
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


async def resolve_account_id(
    account_repo: AccountRepository, workspace_id: int, account_public_id: uuid.UUID | None
) -> int | None:
    if account_public_id is None:
        return None
    account = await account_repo.get_by_public_id(workspace_id, account_public_id)
    if not account:
        raise NotFoundError(
            detail=f"Account with id {account_public_id} not found in this workspace"
        )
    return account.id


async def resolve_create_account_id(
    account_repo: AccountRepository,
    setting_repo: FinanceSettingRepository | None,
    workspace_id: int,
    account_public_id: uuid.UUID | None,
) -> int:
    """Every new transaction must resolve to an account (spec-054, extended to
    recurring transactions by spec-084): explicit account_id, else the
    workspace default, else a 422 telling the caller how to fix it. Historical
    NULL-account rows are untouched — this only governs creates."""
    if account_public_id is not None:
        account = await account_repo.get_by_public_id(workspace_id, account_public_id)
        if not account or not account.is_active:
            raise NotFoundError(
                detail=(
                    f"Account with id {account_public_id} not found in this workspace. "
                    "Cross-workspace account references are not permitted."
                )
            )
        return account.id  # type: ignore[return-value]

    if setting_repo is not None:
        setting = await setting_repo.get_by_workspace(workspace_id)
        if setting and setting.default_spending_account_id is not None:
            # Defense in depth: the default is cleared when its account
            # is deactivated through the API (AccountService.update_account),
            # but don't trust that path alone — re-check is_active here.
            default_account = await account_repo.get_by_id(
                workspace_id, setting.default_spending_account_id
            )
            if default_account and default_account.is_active:
                return setting.default_spending_account_id

    raise ValidationError(
        detail=("Provide account_id or set a default spending account in Finance Settings.")
    )


class TransactionService:
    def __init__(
        self,
        transaction_repo: TransactionRepository,
        category_repo: CategoryRepository,
        account_repo: AccountRepository,
        setting_repo: FinanceSettingRepository | None = None,
    ):
        self.transaction_repo = transaction_repo
        self.category_repo = category_repo
        self.account_repo = account_repo
        self.setting_repo = setting_repo

    async def _build_category_cache(self, workspace_id: int) -> dict[int, uuid.UUID]:
        cats, _ = await self.category_repo.get_all(workspace_id, limit=10000, offset=0)
        return {c.id: c.public_id for c in cats}

    async def _build_account_cache(self, workspace_id: int) -> dict[int, uuid.UUID]:
        accounts, _ = await self.account_repo.list_workspace_accounts(
            workspace_id, limit=10000, offset=0
        )
        return {a.id: a.public_id for a in accounts}

    async def _build_import_batch_cache(
        self, workspace_id: int, transactions: Sequence[SpendingTransaction]
    ) -> dict[int, "ImportBatch"]:
        import_batch_ids = {
            tx.source_import_id for tx in transactions if tx.source_import_id is not None
        }
        if not import_batch_ids:
            return {}
        import_repo = ImportRepository(self.transaction_repo.session)
        return await import_repo.get_by_ids(workspace_id, import_batch_ids)

    async def list_transactions_with_details(
        self,
        workspace_id: int,
        category_public_id: uuid.UUID | None = None,
        account_public_id: uuid.UUID | None = None,
        unassigned_only: bool = False,
        type_filter: TransactionType | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        sort: TransactionSort | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[list[TransactionResponse], int]:
        txs, total = await self.list_transactions(
            workspace_id,
            category_public_id=category_public_id,
            account_public_id=account_public_id,
            unassigned_only=unassigned_only,
            type_filter=type_filter,
            from_date=from_date,
            to_date=to_date,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        cat_cache = await self._build_category_cache(workspace_id)
        account_cache = await self._build_account_cache(workspace_id)
        import_cache = await self._build_import_batch_cache(workspace_id, txs)

        missing_category_ids = {tx.category_id for tx in txs if tx.category_id not in cat_cache}
        if missing_category_ids:
            raise NotFoundError(detail="One or more transaction categories were not found")

        detailed_items = [
            transaction_response(
                tx,
                cat_cache[tx.category_id],
                account_cache.get(tx.account_id) if tx.account_id is not None else None,
                import_cache.get(tx.source_import_id) if tx.source_import_id is not None else None,
            )
            for tx in txs
        ]
        return detailed_items, total

    async def get_transaction_with_details(
        self, workspace_id: int, public_id: uuid.UUID
    ) -> TransactionResponse:
        tx = await self.get_transaction(workspace_id, public_id)
        cat_cache = await self._build_category_cache(workspace_id)
        account_cache = await self._build_account_cache(workspace_id)
        import_cache = await self._build_import_batch_cache(workspace_id, [tx])

        category_public_id = cat_cache.get(tx.category_id)
        if category_public_id is None:
            raise NotFoundError(detail="Transaction category was not found")

        return transaction_response(
            tx,
            category_public_id,
            account_cache.get(tx.account_id) if tx.account_id is not None else None,
            import_cache.get(tx.source_import_id) if tx.source_import_id is not None else None,
        )

    async def create_transaction_with_details(
        self,
        actor_id: int,
        workspace_id: int,
        tx_in: TransactionCreate,
        audit_logger: AuditLogger | None = None,
    ) -> TransactionResponse:
        tx = await self.create_transaction(actor_id, workspace_id, tx_in, audit_logger)
        account_public_id = None
        if tx.account_id is not None:
            if tx_in.account_id is not None:
                account_public_id = tx_in.account_id
            else:
                account = await self.account_repo.get_by_id(workspace_id, tx.account_id)
                account_public_id = account.public_id if account else None

        return transaction_response(tx, tx_in.category_id, account_public_id)

    async def update_transaction_with_details(
        self,
        workspace_id: int,
        transaction_id: uuid.UUID,
        tx_in: TransactionUpdate,
        actor_id: int,
        audit_logger: AuditLogger | None = None,
    ) -> TransactionResponse:
        tx = await self.update_transaction(
            workspace_id, transaction_id, tx_in, actor_id=actor_id, audit_logger=audit_logger
        )
        category = await self.category_repo.get_by_id(workspace_id, tx.category_id)
        if not category:
            raise NotFoundError(detail="Transaction category was not found")

        account_public_id = None
        if tx.account_id is not None:
            account = await self.account_repo.get_by_id(workspace_id, tx.account_id)
            account_public_id = account.public_id if account else None

        import_batch = None
        if tx.source_import_id is not None:
            import_repo = ImportRepository(self.repository.session)
            import_batch = await import_repo.get_by_id(workspace_id, tx.source_import_id)

        return transaction_response(
            tx,
            category.public_id,
            account_public_id,
            import_batch,
        )

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
        return await resolve_account_id(self.account_repo, workspace_id, account_public_id)

    async def _resolve_create_account_id(
        self, workspace_id: int, account_public_id: uuid.UUID | None
    ) -> int:
        return await resolve_create_account_id(
            self.account_repo, self.setting_repo, workspace_id, account_public_id
        )

    async def list_transactions(
        self,
        workspace_id: int,
        category_public_id: uuid.UUID | None = None,
        account_public_id: uuid.UUID | None = None,
        unassigned_only: bool = False,
        type_filter: TransactionType | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        sort: TransactionSort | None = None,
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
            unassigned_only=unassigned_only,
            type_filter=type_filter,
            from_date=from_date,
            to_date=to_date,
            sort=sort,
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

    async def get_ledger(
        self,
        workspace_id: int,
        account_public_id: uuid.UUID,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> LedgerResponse:
        """Return a paginated ledger view for a spending account with running balance.

        Entries are ordered most-recent first (DESC) and include both spending
        transactions and capital transfers. The running_balance field on each
        entry represents the cumulative account balance AFTER that entry
        (viewing from oldest to newest).
        """
        account = await self.account_repo.get_by_public_id(workspace_id, account_public_id)
        if not account:
            raise NotFoundError(
                detail=f"Account with id {account_public_id} not found in this workspace"
            )
        if account.id is None:
            raise ValidationError(detail="Account ID is missing.")
        account_id: int = account.id  # type: ignore[assignment]

        rows, total = await self.transaction_repo.get_ledger_page(
            workspace_id,
            account_id,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )

        total_balance = Decimal("0")
        # Compute the tail balance: net of all entries BEFORE the oldest row in this page
        if rows:
            tail_balance = await self.transaction_repo.get_account_net_balance(
                workspace_id,
                account_id,
                before_row=rows[-1],
            )
        else:
            tail_balance = Decimal("0")
            total_balance = await self.transaction_repo.get_account_net_balance(
                workspace_id,
                account_id,
                from_date=from_date,
                to_date=to_date,
            )

        # Build running_balance per entry (desc page → oldest row first, accumulate, re-reverse)
        def _is_credit(row: LedgerRow) -> bool:
            """Return True if this entry adds to the balance."""
            return row.entry_kind == "transfer_in" or row.type == "income"

        reversed_rows = list(reversed(rows))
        running = tail_balance
        entries_reversed: list[LedgerEntry] = []
        for row in reversed_rows:
            if _is_credit(row):
                running += row.amount
            else:
                running -= row.amount
            entry = LedgerEntry(
                public_id=row.public_id,
                entry_kind=row.entry_kind,  # type: ignore[arg-type]
                category_id=None,  # resolved below for transaction rows
                account_id=account.public_id,
                amount=row.amount,
                type=row.type,  # type: ignore[arg-type]
                occurred_at=row.occurred_at,
                description=row.description,
                wallet_name=row.wallet_name,
                labels=row.labels,
                source_type=row.source_type,
                running_balance=running,
                created_at=row.created_at,
            )
            entries_reversed.append(entry)

        # Re-reverse to restore desc order (most recent first)
        entries = list(reversed(entries_reversed))

        # Resolve category public_ids in bulk (transaction rows only)
        cat_ids = [r.category_id for r in rows if r.category_id is not None]
        unique_cat_ids = list(set(cat_ids))
        cat_map: dict[int, uuid.UUID] = {}
        if unique_cat_ids:
            cat_rows = await self.category_repo.session.execute(
                select(SpendingCategory).where(
                    SpendingCategory.workspace_id == workspace_id,
                    SpendingCategory.id.in_(unique_cat_ids),
                )
            )
            for cat_row in cat_rows.scalars().all():
                cat_map[cat_row.id] = cat_row.public_id

        for i, (row, entry) in enumerate(zip(rows, entries, strict=True)):
            if row.category_id is not None:
                entries[i] = LedgerEntry(**{
                    **entry.model_dump(),
                    "category_id": cat_map.get(row.category_id),
                })

        # Opening/closing balances for this page
        if entries:
            last_row = rows[-1]
            last_entry = entries[-1]
            opening = (
                last_entry.running_balance - last_entry.amount
                if _is_credit(last_row)
                else last_entry.running_balance + last_entry.amount
            )
            closing = entries[0].running_balance
        else:
            opening = total_balance
            closing = total_balance

        return LedgerResponse(
            account_public_id=account.public_id,
            account_name=account.name,
            account_currency=account.default_currency_code,
            opening_balance=opening,
            closing_balance=closing,
            total_entries=total,
            items=entries,
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
        account_id = await self._resolve_create_account_id(workspace_id, tx_in.account_id)
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

        breaking_fields = {"amount", "occurred_at", "type"}
        is_breaking_edit = any(
            field in update_data and getattr(transaction, field) != update_data[field]
            for field in breaking_fields
        )

        for key, value in update_data.items():
            setattr(transaction, key, value)
        transaction.updated_at = datetime.now(UTC)
        transaction = await self.transaction_repo.save(transaction)

        if is_breaking_edit and transaction.id is not None:
            # Statement matching is metadata, never mutation (spec-078
            # INV-1): this only clears a match *reference* on the
            # statement_lines side, never touches this transaction further.
            await StatementService(self.transaction_repo.session).break_matches_for_transaction(
                workspace_id, transaction.id
            )

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
        transaction_id = transaction.id

        if transaction_id is not None:
            # Must clear the statement_lines FK reference before deleting
            # the row it points to (no ON DELETE CASCADE on
            # matched_transaction_id — the reference is metadata the match
            # engine owns, not something the transaction delete should
            # cascade through implicitly).
            await StatementService(self.transaction_repo.session).break_matches_for_transaction(
                workspace_id, transaction_id
            )

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

    async def get_category_breakdown(
        self,
        workspace_id: int,
        from_date: date,
        to_date: date,
        type_filter: TransactionType,
        limit: int = 10,
    ) -> CategoryBreakdownResponse:
        if from_date > to_date:
            raise ValidationError(detail="from_date cannot be after to_date")
        if (to_date - from_date).days > 24 * 31:
            raise ValidationError(detail="Date range cannot exceed 24 months")

        start_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=UTC)
        end_dt = datetime(to_date.year, to_date.month, to_date.day, 23, 59, 59, 999999, tzinfo=UTC)

        # 1. Total amount
        total_stmt = select(func.coalesce(func.sum(SpendingTransaction.amount), 0)).where(
            SpendingTransaction.workspace_id == workspace_id,
            SpendingTransaction.type == type_filter.value,
            SpendingTransaction.occurred_at >= start_dt,
            SpendingTransaction.occurred_at <= end_dt,
        )
        total_amount = Decimal(await self.transaction_repo.session.scalar(total_stmt) or 0)

        # 2. Categories sums
        stmt = (
            select(
                SpendingCategory.public_id,
                SpendingCategory.name,
                func.coalesce(func.sum(SpendingTransaction.amount), 0).label("amount"),
                func.count(SpendingTransaction.id).label("count"),
            )
            .join(SpendingTransaction, SpendingTransaction.category_id == SpendingCategory.id)
            .where(
                SpendingTransaction.workspace_id == workspace_id,
                SpendingTransaction.type == type_filter.value,
                SpendingTransaction.occurred_at >= start_dt,
                SpendingTransaction.occurred_at <= end_dt,
            )
            .group_by(SpendingCategory.id, SpendingCategory.public_id, SpendingCategory.name)
            .order_by(desc(func.sum(SpendingTransaction.amount)))
        )
        rows = (await self.transaction_repo.session.execute(stmt)).all()

        items: list[CategoryBreakdownItem] = []
        other_amount = Decimal("0")
        other_count = 0
        other_cats = 0

        for idx, row in enumerate(rows):
            amount = Decimal(row.amount)
            pct = float((amount / total_amount) * 100) if total_amount > 0 else 0.0
            if idx < limit:
                items.append(
                    CategoryBreakdownItem(
                        category_id=row.public_id,
                        category_name=row.name,
                        amount=amount,
                        pct_of_total=pct,
                        transaction_count=int(row.count),
                    )
                )
            else:
                other_amount += amount
                other_count += int(row.count)
                other_cats += 1

        other = None
        if other_cats > 0:
            other = CategoryBreakdownOther(
                amount=other_amount,
                pct_of_total=float((other_amount / total_amount) * 100)
                if total_amount > 0
                else 0.0,
                category_count=other_cats,
            )

        return CategoryBreakdownResponse(
            from_date=from_date,
            to_date=to_date,
            type=type_filter,
            total=total_amount,
            categories=items,
            other=other,
        )

    async def get_savings_rate(
        self, workspace_id: int, from_month: date, to_month: date
    ) -> SavingsRateResponse:
        if from_month > to_month:
            raise ValidationError(detail="from_month cannot be after to_month")
        if (to_month.year - from_month.year) * 12 + (to_month.month - from_month.month) >= 24:
            raise ValidationError(detail="Date range cannot exceed 24 months")

        start_dt = datetime(from_month.year, from_month.month, 1, tzinfo=UTC)
        if to_month.month == 12:
            end_dt = datetime(to_month.year + 1, 1, 1, tzinfo=UTC)
        else:
            end_dt = datetime(to_month.year, to_month.month + 1, 1, tzinfo=UTC)

        month_bucket = func.date_trunc("month", SpendingTransaction.occurred_at)
        stmt = (
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
            )
            .where(
                SpendingTransaction.workspace_id == workspace_id,
                SpendingTransaction.occurred_at >= start_dt,
                SpendingTransaction.occurred_at < end_dt,
            )
            .group_by(month_bucket)
            .order_by(month_bucket)
        )
        rows = (await self.transaction_repo.session.execute(stmt)).all()

        data_by_month = {}
        for row in rows:
            month_str = row.month.strftime("%Y-%m")
            income = Decimal(row.income)
            expense = Decimal(row.expense)
            savings = income - expense
            rate = float((savings / income) * 100) if income > 0 else None
            data_by_month[month_str] = (income, expense, savings, rate)

        cursor = from_month.replace(day=1)
        end = to_month.replace(day=1)
        points: list[SavingsRatePoint] = []
        total_income = Decimal("0")
        total_expense = Decimal("0")

        while cursor <= end:
            month_str = cursor.strftime("%Y-%m")
            if month_str in data_by_month:
                income, expense, savings, rate = data_by_month[month_str]
            else:
                income, expense, savings, rate = Decimal("0"), Decimal("0"), Decimal("0"), None

            points.append(
                SavingsRatePoint(
                    month=month_str,
                    income=income,
                    expense=expense,
                    savings=savings,
                    savings_rate_pct=rate,
                )
            )
            total_income += income
            total_expense += expense

            if cursor.month == 12:
                cursor = cursor.replace(year=cursor.year + 1, month=1)
            else:
                cursor = cursor.replace(month=cursor.month + 1)

        total_savings = total_income - total_expense
        avg_rate = float((total_savings / total_income) * 100) if total_income > 0 else None

        return SavingsRateResponse(
            from_month=from_month.strftime("%Y-%m"),
            to_month=to_month.strftime("%Y-%m"),
            months=points,
            period_totals=SavingsRateTotals(
                total_income=total_income,
                total_expense=total_expense,
                total_savings=total_savings,
                average_savings_rate_pct=avg_rate,
            ),
        )


class BudgetService:
    def __init__(
        self,
        budget_repo: BudgetRepository,
        category_repo: CategoryRepository,
        group_repo: CategoryGroupRepository | None = None,
    ):
        self.budget_repo = budget_repo
        self.category_repo = category_repo
        self.group_repo = group_repo

    async def _build_category_cache(self, workspace_id: int) -> dict[int, uuid.UUID]:
        cats, _ = await self.category_repo.get_all(workspace_id, limit=10000, offset=0)
        return {c.id: c.public_id for c in cats}

    async def _build_group_cache(self, workspace_id: int) -> dict[int, uuid.UUID]:
        if self.group_repo is None:
            return {}
        groups, _ = await self.group_repo.get_all(workspace_id, limit=10000, offset=0)
        return {g.id: g.public_id for g in groups}

    async def _build_import_batch_cache(
        self, workspace_id: int, budgets: Sequence[SpendingBudget]
    ) -> dict[int, "ImportBatch"]:
        import_batch_ids = {b.source_import_id for b in budgets if b.source_import_id is not None}
        if not import_batch_ids:
            return {}
        import_repo = ImportRepository(self.budget_repo.session)
        return await import_repo.get_by_ids(workspace_id, import_batch_ids)

    async def list_budgets_with_details(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        month_start: date | None = None,
    ) -> tuple[list[BudgetResponse], int]:
        budgets, total = await self.list_budgets(
            workspace_id, limit=limit, offset=offset, month_start=month_start
        )
        cat_cache = await self._build_category_cache(workspace_id)
        group_cache = await self._build_group_cache(workspace_id)
        import_cache = await self._build_import_batch_cache(workspace_id, budgets)
        detailed = []
        for b in budgets:
            cat_uuid = cat_cache.get(b.category_id) if b.category_id else None
            group_uuid = group_cache.get(b.category_group_id) if b.category_group_id else None

            data = b.model_dump()
            data["category_id"] = cat_uuid
            data["category_group_id"] = group_uuid
            data["source_metadata"] = source_metadata_response(
                b.source_type, b.source_ref, import_cache.get(b.source_import_id)
            )
            detailed.append(BudgetResponse.model_validate(data))
        return detailed, total

    async def create_budget_with_details(
        self,
        workspace_id: int,
        budget_in: BudgetCreate,
        actor_id: int,
        audit_logger: AuditLogger | None = None,
    ) -> BudgetResponse:
        budget = await self.create_budget(
            workspace_id, budget_in, actor_id=actor_id, audit_logger=audit_logger
        )
        cat_uuid = None
        group_uuid = None
        if budget.category_id:
            category = await self.category_repo.get_by_id(workspace_id, budget.category_id)
            cat_uuid = category.public_id if category else None
        if budget.category_group_id and self.group_repo:
            group = await self.group_repo.get_by_id(workspace_id, budget.category_group_id)
            group_uuid = group.public_id if group else None

        return (
            budget_response(budget, cat_uuid)
            if budget.category_id
            else BudgetResponse.model_validate({
                **budget.model_dump(),
                "category_id": None,
                "category_group_id": group_uuid,
                "source_metadata": source_metadata_response(
                    budget.source_type, budget.source_ref, None
                ),
            })
        )

    async def update_budget_with_details(
        self,
        workspace_id: int,
        budget_id: uuid.UUID,
        budget_in: BudgetUpdate,
        actor_id: int,
        audit_logger: AuditLogger | None = None,
    ) -> BudgetResponse:
        budget = await self.update_budget(
            workspace_id, budget_id, budget_in, actor_id=actor_id, audit_logger=audit_logger
        )
        cat_uuid = None
        group_uuid = None
        if budget.category_id:
            category = await self.category_repo.get_by_id(workspace_id, budget.category_id)
            cat_uuid = category.public_id if category else None
        if budget.category_group_id and self.group_repo:
            group = await self.group_repo.get_by_id(workspace_id, budget.category_group_id)
            group_uuid = group.public_id if group else None

        return (
            budget_response(budget, cat_uuid)
            if budget.category_id
            else BudgetResponse.model_validate({
                **budget.model_dump(),
                "category_id": None,
                "category_group_id": group_uuid,
                "source_metadata": source_metadata_response(
                    budget.source_type, budget.source_ref, None
                ),
            })
        )

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

    async def _resolve_group(self, workspace_id: int, group_public_id: uuid.UUID) -> CategoryGroup:
        if self.group_repo is None:
            raise ValidationError(detail="Group repository is not available")
        group = await self.group_repo.get_by_public_id(workspace_id, group_public_id)
        if not group:
            raise NotFoundError(
                detail=f"Category group with id {group_public_id} not found in this workspace."
            )
        return group

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
        category_id = None
        category_group_id = None
        if budget_in.category_id is not None:
            category = await self._resolve_category(workspace_id, budget_in.category_id)
            category_id = category.id
        elif budget_in.category_group_id is not None:
            group = await self._resolve_group(workspace_id, budget_in.category_group_id)
            category_group_id = group.id

        overlapping = await self.budget_repo.get_overlapping_budgets(
            workspace_id,
            category_id,
            category_group_id,
            budget_in.start_month,
            budget_in.end_month,
        )
        if overlapping:
            exact_match = any(
                b.category_id == category_id
                and b.category_group_id == category_group_id
                and b.start_month == budget_in.start_month
                and b.end_month == budget_in.end_month
                for b in overlapping
            )
            if exact_match:
                raise ConflictError(
                    detail="A budget for this scope already exists. Use PATCH to update it."
                )
            raise ConflictError(detail="A budget for this scope overlaps with the requested range.")

        budget = SpendingBudget(
            workspace_id=workspace_id,
            category_id=category_id,
            category_group_id=category_group_id,
            amount=budget_in.amount,
            start_month=budget_in.start_month,
            end_month=budget_in.end_month,
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
                entity_id=budget.id,
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

        # Enforce range containment overlap checks
        new_end = budget.end_month
        if budget_in.end_month is not None:
            new_end = budget_in.end_month

        if new_end is not None and new_end < budget.start_month:
            raise ValidationError(detail="end_month cannot be before the budget's start_month")

        overlapping = await self.budget_repo.get_overlapping_budgets(
            workspace_id,
            budget.category_id,
            budget.category_group_id,
            budget.start_month,
            new_end,
            exclude_id=budget.id,
        )
        if overlapping:
            raise ConflictError(detail="A budget for this scope overlaps with the requested range.")

        if budget_in.amount is not None:
            budget.amount = budget_in.amount
        if budget_in.end_month is not None:
            budget.end_month = budget_in.end_month

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
                entity_id=budget.id,
                details={
                    "entity_public_id": str(budget.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )
        return budget

    async def change_budget_amount(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        amount: Decimal,
        from_month: date,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> SpendingBudget:
        budget = await self.get_budget(workspace_id, public_id)
        if from_month <= budget.start_month:
            raise ValidationError(detail="from_month must be after the budget's start_month")
        if budget.end_month is not None and from_month > budget.end_month:
            raise ValidationError(detail="from_month cannot be after the budget's end_month")

        before_snap = _snapshot_budget(budget)
        original_end = budget.end_month

        # 1. Update old budget range
        budget.end_month = add_months(from_month, -1)
        budget.updated_at = datetime.now(UTC)
        await self.budget_repo.save(budget)

        if audit_logger and actor_id is not None:
            after_snap = _snapshot_budget(budget)
            changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="update",
                module="spending",
                entity_type="spending_budget",
                entity_id=budget.id,
                details={
                    "entity_public_id": str(budget.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )

        # 2. Create successor budget
        successor = SpendingBudget(
            workspace_id=workspace_id,
            category_id=budget.category_id,
            category_group_id=budget.category_group_id,
            amount=amount,
            start_month=from_month,
            end_month=original_end,
            source_type="manual",
        )
        await self.budget_repo.create(successor)

        if audit_logger and actor_id is not None:
            after_snap_succ = _snapshot_budget(successor)
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="create",
                module="spending",
                entity_type="spending_budget",
                entity_id=successor.id,
                details={
                    "entity_public_id": str(successor.public_id),
                    "before": None,
                    "after": after_snap_succ,
                    "changed_fields": list(after_snap_succ.keys()),
                },
            )

        return successor

    async def get_budget_performance(
        self, workspace_id: int, from_month: date, to_month: date
    ) -> BudgetPerformanceResponse:
        from_month = from_month.replace(day=1)
        to_month = to_month.replace(day=1)
        if from_month > to_month:
            raise ValidationError(detail="from_month cannot be after to_month")
        if (to_month.year - from_month.year) * 12 + (to_month.month - from_month.month) >= 24:
            raise ValidationError(detail="Date range cannot exceed 24 months")

        start_dt = datetime(from_month.year, from_month.month, 1, tzinfo=UTC)
        if to_month.month == 12:
            end_dt = datetime(to_month.year + 1, 1, 1, tzinfo=UTC)
        else:
            end_dt = datetime(to_month.year, to_month.month + 1, 1, tzinfo=UTC)

        # Months in range
        months_in_range = []
        curr = from_month
        while curr <= to_month:
            months_in_range.append(curr)
            curr = add_months(curr, 1)

        # Fetch categories, groups, budgets, transactions
        cats, _ = await self.category_repo.get_all(workspace_id, limit=10000)
        group_repo_active = self.group_repo
        if group_repo_active:
            groups, _ = await group_repo_active.get_all(workspace_id, limit=10000)
        else:
            groups = []

        all_budgets, _ = await self.budget_repo.get_all(workspace_id, limit=10000)

        tx_stmt = select(SpendingTransaction).where(
            SpendingTransaction.workspace_id == workspace_id,
            SpendingTransaction.type == TransactionType.expense.value,
            SpendingTransaction.occurred_at >= start_dt,
            SpendingTransaction.occurred_at < end_dt,
        )
        txs = (await self.budget_repo.session.execute(tx_stmt)).scalars().all()

        # 1. Categories Performance
        categories_items = []
        for cat in cats:
            cat_txs = [t for t in txs if t.category_id == cat.id]
            actual_amount = sum(t.amount for t in cat_txs)

            # Sum covering budgets for each month in range
            budget_amount = Decimal("0")
            has_budget = False
            for m in months_in_range:
                covering = [
                    b
                    for b in all_budgets
                    if b.category_id == cat.id
                    and b.start_month <= m
                    and (b.end_month is None or b.end_month >= m)
                ]
                if covering:
                    budget_amount += covering[0].amount
                    has_budget = True

            if not has_budget:
                budget_amount_val = None
                utilization_pct = None
                remaining = None
                status = "exceeded" if actual_amount > 0 else "on_track"
            else:
                budget_amount_val = budget_amount
                utilization_pct = (
                    float((actual_amount / budget_amount) * 100) if budget_amount > 0 else None
                )
                remaining = budget_amount - actual_amount
                if utilization_pct is None or utilization_pct < 90.0:
                    status = "on_track"
                elif utilization_pct <= 100.0:
                    status = "warning"
                else:
                    status = "exceeded"

            categories_items.append(
                BudgetPerformanceItem(
                    category_id=cat.public_id,
                    category_name=cat.name,
                    budget_amount=budget_amount_val,
                    actual_amount=actual_amount,
                    utilization_pct=utilization_pct,
                    remaining=remaining,
                    status=status,
                )
            )

        # 2. Groups Performance
        groups_items = []
        for g in groups:
            member_cat_ids = [c.id for c in cats if c.category_group_id == g.id]
            group_txs = [t for t in txs if t.category_id in member_cat_ids]
            actual_amount = sum(t.amount for t in group_txs)

            # Sum covering budgets for each month in range
            budget_amount = Decimal("0")
            has_budget = False
            for m in months_in_range:
                covering = [
                    b
                    for b in all_budgets
                    if b.category_group_id == g.id
                    and b.start_month <= m
                    and (b.end_month is None or b.end_month >= m)
                ]
                if covering:
                    budget_amount += covering[0].amount
                    has_budget = True

            if not has_budget:
                budget_amount_val = None
                utilization_pct = None
                remaining = None
                status = "exceeded" if actual_amount > 0 else "on_track"
            else:
                budget_amount_val = budget_amount
                utilization_pct = (
                    float((actual_amount / budget_amount) * 100) if budget_amount > 0 else None
                )
                remaining = budget_amount - actual_amount
                if utilization_pct is None or utilization_pct < 90.0:
                    status = "on_track"
                elif utilization_pct <= 100.0:
                    status = "warning"
                else:
                    status = "exceeded"

            groups_items.append(
                BudgetPerformanceItem(
                    category_group_id=g.public_id,
                    category_group_name=g.name,
                    budget_amount=budget_amount_val,
                    actual_amount=actual_amount,
                    utilization_pct=utilization_pct,
                    remaining=remaining,
                    status=status,
                )
            )

        # Totals
        cat_budget_total = sum(
            item.budget_amount for item in categories_items if item.budget_amount is not None
        )
        cat_actual_total = sum(item.actual_amount for item in categories_items)
        cat_overall_utilization = (
            float((cat_actual_total / cat_budget_total) * 100) if cat_budget_total > 0 else None
        )

        group_budget_total = sum(
            item.budget_amount for item in groups_items if item.budget_amount is not None
        )
        group_actual_total = sum(item.actual_amount for item in groups_items)
        group_overall_utilization = (
            float((group_actual_total / group_budget_total) * 100)
            if group_budget_total > 0
            else None
        )

        return BudgetPerformanceResponse(
            from_month=from_month.strftime("%Y-%m"),
            to_month=to_month.strftime("%Y-%m"),
            categories=categories_items,
            totals=BudgetPerformanceTotals(
                total_budgeted=cat_budget_total,
                total_actual=cat_actual_total,
                overall_utilization_pct=cat_overall_utilization,
            ),
            groups=groups_items,
            group_totals=BudgetPerformanceTotals(
                total_budgeted=group_budget_total,
                total_actual=group_actual_total,
                overall_utilization_pct=group_overall_utilization,
            ),
        )


class RecurringTransactionService:
    def __init__(
        self,
        recurring_repo: RecurringTransactionRepository,
        tx_repo: TransactionRepository,
        category_repo: CategoryRepository,
        account_repo: AccountRepository,
        setting_repo: FinanceSettingRepository | None = None,
    ):
        self.recurring_repo = recurring_repo
        self.tx_repo = tx_repo
        self.category_repo = category_repo
        self.account_repo = account_repo
        self.setting_repo = setting_repo

    async def _build_category_cache(self, workspace_id: int) -> dict[int, uuid.UUID]:
        cats, _ = await self.category_repo.get_all(workspace_id, limit=10000, offset=0)
        return {c.id: c.public_id for c in cats}

    async def _build_account_cache(self, workspace_id: int) -> dict[int, uuid.UUID]:
        accounts, _ = await self.account_repo.list_workspace_accounts(
            workspace_id, limit=10000, offset=0
        )
        return {a.id: a.public_id for a in accounts}

    async def get_due_between(
        self, workspace_id: int, start_date, end_date
    ) -> list[tuple[RecurringTransaction, str]]:
        """Active recurring rules due in [start_date, end_date], paired with
        their category name — the morning briefing's "recurring due soon"
        line (spec-067) needs a human label, not just a category_id."""
        rules = await self.recurring_repo.get_due_between(workspace_id, start_date, end_date)
        if not rules:
            return []
        cats, _ = await self.category_repo.get_all(workspace_id, limit=10000, offset=0)
        name_by_id = {c.id: c.name for c in cats}
        return [(rule, name_by_id.get(rule.category_id, "Uncategorized")) for rule in rules]

    async def list_recurring_with_details(
        self,
        workspace_id: int,
        is_active: bool | None = True,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[list[RecurringTransactionResponse], int]:
        items, total = await self.list_recurring(workspace_id, is_active, limit, offset)
        cat_cache = await self._build_category_cache(workspace_id)
        acct_cache = await self._build_account_cache(workspace_id)
        detailed = [
            recurring_response(
                item, cat_cache.get(item.category_id), acct_cache.get(item.account_id)
            )
            for item in items
        ]
        return detailed, total

    async def create_recurring_with_details(
        self,
        workspace_id: int,
        actor_id: int,
        payload: RecurringTransactionCreate,
    ) -> RecurringTransactionResponse:
        item = await self.create_recurring(workspace_id, actor_id, payload)
        account_public_id = None
        if item.account_id is not None:
            if payload.account_id is not None:
                account_public_id = payload.account_id
            else:
                account = await self.account_repo.get_by_id(workspace_id, item.account_id)
                account_public_id = account.public_id if account else None
        return recurring_response(item, payload.category_id, account_public_id)

    async def get_recurring_with_details(
        self,
        workspace_id: int,
        recurring_id: uuid.UUID,
    ) -> RecurringTransactionResponse:
        item = await self.get_recurring(workspace_id, recurring_id)
        category = await self.category_repo.get_by_id(workspace_id, item.category_id)
        category_public_id = category.public_id if category else None
        account_public_id = None
        if item.account_id is not None:
            account = await self.account_repo.get_by_id(workspace_id, item.account_id)
            account_public_id = account.public_id if account else None
        return recurring_response(item, category_public_id, account_public_id)

    async def update_recurring_with_details(
        self,
        workspace_id: int,
        recurring_id: uuid.UUID,
        payload: RecurringTransactionUpdate,
    ) -> RecurringTransactionResponse:
        item = await self.update_recurring(workspace_id, recurring_id, payload)
        category = await self.category_repo.get_by_id(workspace_id, item.category_id)
        category_public_id = category.public_id if category else None
        account_public_id = None
        if item.account_id is not None:
            account = await self.account_repo.get_by_id(workspace_id, item.account_id)
            account_public_id = account.public_id if account else None
        return recurring_response(item, category_public_id, account_public_id)

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
        account_id = await resolve_create_account_id(
            self.account_repo, self.setting_repo, workspace_id, payload.account_id
        )
        recurring = RecurringTransaction(
            workspace_id=workspace_id,
            user_id=user_id,
            category_id=category.id,  # type: ignore[arg-type]
            account_id=account_id,
            amount=payload.amount,
            type=payload.type,
            description=payload.description,
            frequency=payload.frequency,
            interval=payload.interval,
            anchor_date=payload.anchor_date,
            next_due_date=first_due_date(
                payload.anchor_date,
                datetime.now(UTC).date(),
                payload.frequency,
                payload.interval,
                monthly_mode=payload.monthly_mode,
                by_weekday=payload.by_weekday,
                by_ordinal=payload.by_ordinal,
            ),
            end_date=payload.end_date,
            monthly_mode=payload.monthly_mode,
            by_weekday=payload.by_weekday,
            by_ordinal=payload.by_ordinal,
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
        if "account_id" in update_data:
            account_public_id = update_data.pop("account_id")
            if account_public_id is None:
                if recurring.account_id is not None:
                    raise ValidationError(
                        detail=(
                            "account_id cannot be cleared once set; provide a replacement "
                            "account_id."
                        )
                    )
            else:
                update_data["account_id"] = await resolve_account_id(
                    self.account_repo, workspace_id, account_public_id
                )
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
        try:
            validate_recurrence_fields(
                recurring.frequency,
                recurring.monthly_mode,
                recurring.by_weekday,
                recurring.by_ordinal,
            )
        except ValueError as exc:
            raise ValidationError(detail=str(exc)) from exc
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

        # Build category/account public_id lookups
        cats, _ = await category_repo.get_all(workspace_id, limit=10000, offset=0)
        cat_pub_id: dict[int, uuid.UUID] = {c.id: c.public_id for c in cats}  # type: ignore[union-attr]
        acct_pub_id = await self._build_account_cache(workspace_id)

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
                            account_id=acct_pub_id.get(recurrence.account_id),
                            amount=recurrence.amount,
                            type=recurrence.type,
                            description=recurrence.description,
                            projected_date=projected,
                            frequency=recurrence.frequency,
                            interval=recurrence.interval,
                        )
                    )
                projected = advance_due_date(
                    projected,
                    recurrence.frequency,
                    recurrence.interval,
                    anchor_day=recurrence.anchor_date.day,
                    monthly_mode=recurrence.monthly_mode,
                    by_weekday=recurrence.by_weekday,
                    by_ordinal=recurrence.by_ordinal,
                )

        items.sort(key=lambda x: x.projected_date)
        return UpcomingPreviewResponse(
            days=days,
            from_date=today,
            to_date=horizon,
            items=items,
        )


class KpiService:
    """Custom financial KPIs (spec-077).

    Evaluation is read-only over ``spending_transactions`` via the existing
    aggregation paths — no stored aggregates, KPI values are always
    recomputed from the ledger. Single-currency-per-KPI is re-validated at
    every evaluation, not just at create/update, because the resolved
    account set can drift after the KPI is defined (spec-050 bug class)."""

    def __init__(
        self,
        kpi_repo: KpiRepository,
        category_repo: CategoryRepository,
        group_repo: CategoryGroupRepository,
        account_repo: AccountRepository,
        transaction_repo: TransactionRepository,
    ) -> None:
        self.kpi_repo = kpi_repo
        self.category_repo = category_repo
        self.group_repo = group_repo
        self.account_repo = account_repo
        self.transaction_repo = transaction_repo

    async def _resolve_currency(self, workspace_id: int, account_id: int | None) -> str:
        """Determine the single currency a KPI's filter resolves to.

        An ``account_id`` filter pins the currency to that one account. Any
        other filter (category/group/none) can touch transactions across
        every account in the workspace, so every active account must share
        one currency — an unfiltered KPI in a mixed-currency workspace is
        invalid in v1 (spec-077)."""
        if account_id is not None:
            account = await self.account_repo.get_by_id(workspace_id, account_id)
            if not account:
                raise NotFoundError(detail="Account not found in this workspace")
            return account.default_currency_code

        accounts, _ = await self.account_repo.list_workspace_accounts(
            workspace_id, limit=1000, offset=0
        )
        currencies = {a.default_currency_code for a in accounts if a.is_active}
        if not currencies:
            raise ValidationError(detail="Workspace has no active accounts to evaluate a KPI over")
        if len(currencies) > 1:
            raise ValidationError(
                detail=(
                    "This KPI's filter spans accounts in multiple currencies "
                    f"({sorted(currencies)}). Filter by a single account, or keep all "
                    "workspace accounts on one currency, until cross-currency KPIs land."
                )
            )
        return currencies.pop()

    async def _resolve_filter_ids(
        self,
        workspace_id: int,
        category_public_id: uuid.UUID | None,
        category_group_public_id: uuid.UUID | None,
        account_public_id: uuid.UUID | None,
    ) -> tuple[int | None, int | None, int | None]:
        category_id = None
        category_group_id = None
        account_id = None
        if category_public_id is not None:
            category = await self.category_repo.get_by_public_id(workspace_id, category_public_id)
            if not category:
                raise NotFoundError(detail="Category not found in this workspace")
            category_id = category.id
        if category_group_public_id is not None:
            group = await self.group_repo.get_by_public_id(workspace_id, category_group_public_id)
            if not group:
                raise NotFoundError(detail="Category group not found in this workspace")
            category_group_id = group.id
        if account_public_id is not None:
            account = await self.account_repo.get_by_public_id(workspace_id, account_public_id)
            if not account:
                raise NotFoundError(detail="Account not found in this workspace")
            account_id = account.id
        return category_id, category_group_id, account_id

    @staticmethod
    def _window_bounds(
        evaluation_window: KpiWindow, today: date
    ) -> tuple[datetime, datetime, date, date]:
        """Return (from_dt, to_dt, window_start, window_end) for ``today``.

        ``to_dt`` is exclusive; ``window_end`` is the last calendar day
        included, for display."""
        if evaluation_window == KpiWindow.calendar_month:
            start = today.replace(day=1)
            next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            from_dt = datetime(start.year, start.month, 1, tzinfo=UTC)
            to_dt = datetime(next_month.year, next_month.month, 1, tzinfo=UTC)
            return from_dt, to_dt, start, next_month - timedelta(days=1)
        if evaluation_window == KpiWindow.calendar_week:
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=6)
            from_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
            to_dt = from_dt + timedelta(days=7)
            return from_dt, to_dt, start, end
        # rolling_30d: 30 days inclusive of today
        start = today - timedelta(days=29)
        from_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
        to_dt = datetime(today.year, today.month, today.day, tzinfo=UTC) + timedelta(days=1)
        return from_dt, to_dt, start, today

    async def _evaluate_value(
        self,
        workspace_id: int,
        kpi: FinancialKpi,
        from_dt: datetime,
        to_dt: datetime,
    ) -> Decimal:
        to_dt_inclusive = to_dt - timedelta(microseconds=1)
        common = {
            "category_id": kpi.category_id,
            "account_id": kpi.account_id,
            "category_group_id": kpi.category_group_id,
        }
        if kpi.metric_type == KpiMetricType.spend_total:
            return await self.transaction_repo.get_sum_by_type(
                workspace_id, TransactionType.expense, from_dt, to_dt_inclusive, **common
            )
        if kpi.metric_type == KpiMetricType.income_total:
            return await self.transaction_repo.get_sum_by_type(
                workspace_id, TransactionType.income, from_dt, to_dt_inclusive, **common
            )
        income = await self.transaction_repo.get_sum_by_type(
            workspace_id, TransactionType.income, from_dt, to_dt_inclusive, **common
        )
        expense = await self.transaction_repo.get_sum_by_type(
            workspace_id, TransactionType.expense, from_dt, to_dt_inclusive, **common
        )
        return income - expense

    @staticmethod
    def _is_breached(kpi: FinancialKpi, current_value: Decimal) -> bool:
        if kpi.target_value is None or kpi.target_direction is None:
            return False
        if kpi.target_direction == "lte":
            return current_value > kpi.target_value
        return current_value < kpi.target_value

    async def _to_response(
        self,
        workspace_id: int,
        kpi: FinancialKpi,
        category_map: dict[int, SpendingCategory] | None = None,
        group_map: dict[int, CategoryGroup] | None = None,
        account_map: dict[int, Account] | None = None,
    ) -> KpiResponse:
        category_uuid = group_uuid = account_uuid = None
        if kpi.category_id:
            category = (
                category_map.get(kpi.category_id)
                if category_map is not None
                else await self.category_repo.get_by_id(workspace_id, kpi.category_id)
            )
            category_uuid = category.public_id if category else None
        if kpi.category_group_id:
            group = (
                group_map.get(kpi.category_group_id)
                if group_map is not None
                else await self.group_repo.get_by_id(workspace_id, kpi.category_group_id)
            )
            group_uuid = group.public_id if group else None
        if kpi.account_id:
            account = (
                account_map.get(kpi.account_id)
                if account_map is not None
                else await self.account_repo.get_by_id(workspace_id, kpi.account_id)
            )
            account_uuid = account.public_id if account else None

        today = datetime.now(UTC).date()
        from_dt, to_dt, window_start, window_end = self._window_bounds(kpi.evaluation_window, today)
        current_value = await self._evaluate_value(workspace_id, kpi, from_dt, to_dt)

        return KpiResponse(
            public_id=kpi.public_id,
            name=kpi.name,
            metric_type=kpi.metric_type,
            evaluation_window=kpi.evaluation_window,
            category_id=category_uuid,
            category_group_id=group_uuid,
            account_id=account_uuid,
            currency_code=kpi.currency_code,
            target_value=kpi.target_value,
            target_direction=kpi.target_direction,
            display_format=kpi.display_format,
            is_active=kpi.is_active,
            current_value=current_value,
            is_breached=self._is_breached(kpi, current_value),
            window_start=window_start,
            window_end=window_end,
            created_at=kpi.created_at,
            updated_at=kpi.updated_at,
        )

    async def list_kpis(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[list[KpiResponse], int]:
        kpis, total = await self.kpi_repo.get_all(workspace_id, limit=limit, offset=offset)
        category_map = await self.category_repo.get_by_ids(
            workspace_id, {k.category_id for k in kpis if k.category_id}
        )
        group_map = await self.group_repo.get_by_ids(
            workspace_id, {k.category_group_id for k in kpis if k.category_group_id}
        )
        account_map = await self.account_repo.get_by_ids(
            workspace_id, {k.account_id for k in kpis if k.account_id}
        )
        return [
            await self._to_response(workspace_id, k, category_map, group_map, account_map)
            for k in kpis
        ], total

    async def create_kpi(self, workspace_id: int, kpi_in: KpiCreate) -> KpiResponse:
        category_id, category_group_id, account_id = await self._resolve_filter_ids(
            workspace_id, kpi_in.category_id, kpi_in.category_group_id, kpi_in.account_id
        )
        currency_code = await self._resolve_currency(workspace_id, account_id)

        kpi = FinancialKpi(
            workspace_id=workspace_id,
            name=kpi_in.name,
            metric_type=kpi_in.metric_type,
            evaluation_window=kpi_in.evaluation_window,
            category_id=category_id,
            category_group_id=category_group_id,
            account_id=account_id,
            currency_code=currency_code,
            target_value=kpi_in.target_value,
            target_direction=kpi_in.target_direction,
            display_format=kpi_in.display_format,
        )
        kpi = await self.kpi_repo.create(kpi)
        return await self._to_response(workspace_id, kpi)

    async def _resolve_kpi(self, workspace_id: int, kpi_id: uuid.UUID) -> FinancialKpi:
        kpi = await self.kpi_repo.get_by_public_id(workspace_id, kpi_id)
        if not kpi:
            raise NotFoundError(detail=f"KPI with id {kpi_id} not found in this workspace")
        return kpi

    async def update_kpi(
        self, workspace_id: int, kpi_id: uuid.UUID, kpi_in: KpiUpdate
    ) -> KpiResponse:
        kpi = await self._resolve_kpi(workspace_id, kpi_id)

        # model_fields_set (not `is not None`) so a client can explicitly
        # clear a filter or target by sending it as null — an `is not None`
        # check can't distinguish "field omitted" from "field set to null"
        # and would silently keep the old value in the latter case.
        fields_set = kpi_in.model_fields_set
        filter_touched = bool({"category_id", "category_group_id", "account_id"} & fields_set)
        if filter_touched:
            category_id, category_group_id, account_id = await self._resolve_filter_ids(
                workspace_id, kpi_in.category_id, kpi_in.category_group_id, kpi_in.account_id
            )
            kpi.category_id = category_id
            kpi.category_group_id = category_group_id
            kpi.account_id = account_id
            kpi.currency_code = await self._resolve_currency(workspace_id, account_id)

        if "name" in fields_set and kpi_in.name is not None:
            kpi.name = kpi_in.name
        if {"target_value", "target_direction"} & fields_set:
            kpi.target_value = kpi_in.target_value
            kpi.target_direction = kpi_in.target_direction
        if "is_active" in fields_set and kpi_in.is_active is not None:
            kpi.is_active = kpi_in.is_active
        kpi.updated_at = datetime.now(UTC)

        kpi = await self.kpi_repo.save(kpi)
        return await self._to_response(workspace_id, kpi)

    async def delete_kpi(self, workspace_id: int, kpi_id: uuid.UUID) -> None:
        kpi = await self._resolve_kpi(workspace_id, kpi_id)
        await self.kpi_repo.delete(kpi)

    async def evaluate_active_kpis(
        self, workspace_id: int
    ) -> list[tuple[FinancialKpi, Decimal, bool]]:
        """Evaluate every active KPI, re-checking the single-currency
        constraint. A KPI that now fails the constraint is skipped rather
        than raising — used by the guardrails job, which must keep
        evaluating the rest of the workspace's KPIs."""
        results: list[tuple[FinancialKpi, Decimal, bool]] = []
        for kpi in await self.kpi_repo.get_active(workspace_id):
            try:
                await self._resolve_currency(workspace_id, kpi.account_id)
            except (ValidationError, NotFoundError):
                continue
            today = datetime.now(UTC).date()
            from_dt, to_dt, _, _ = self._window_bounds(kpi.evaluation_window, today)
            current_value = await self._evaluate_value(workspace_id, kpi, from_dt, to_dt)
            results.append((kpi, current_value, self._is_breached(kpi, current_value)))
        return results
