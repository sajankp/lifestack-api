import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, desc, func, select

from app.core.audit import AuditLogger, snapshot_columns
from app.core.exceptions import (
    CategoryInUseError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.core.pagination import DEFAULT_LIMIT
from app.core.recurrence import advance_due_date, validate_recurrence_fields
from app.finance.repository import AccountRepository, FinanceSettingRepository
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
    LedgerRow,
    RecurringTransactionRepository,
    TransactionRepository,
)
from app.spending.schemas import (
    BudgetCreate,
    BudgetPerformanceItem,
    BudgetPerformanceResponse,
    BudgetPerformanceTotals,
    BudgetUpdate,
    CategoryBreakdownItem,
    CategoryBreakdownOther,
    CategoryBreakdownResponse,
    CategoryCreate,
    CategoryUpdate,
    LedgerEntry,
    LedgerResponse,
    RecurringTransactionCreate,
    RecurringTransactionUpdate,
    SavingsRatePoint,
    SavingsRateResponse,
    SavingsRateTotals,
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
    "amount",
    "month_start",
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
    if data.get("month_start") is not None:
        data["month_start"] = data["month_start"].isoformat()
    return data


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
        setting_repo: FinanceSettingRepository | None = None,
    ):
        self.transaction_repo = transaction_repo
        self.category_repo = category_repo
        self.account_repo = account_repo
        self.setting_repo = setting_repo

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

    async def _resolve_create_account_id(
        self, workspace_id: int, account_public_id: uuid.UUID | None
    ) -> int:
        """Every new transaction must resolve to an account (spec-054):
        explicit account_id, else the workspace default, else a 422 telling
        the caller how to fix it. Historical NULL-account rows are untouched
        — this only governs creates."""
        if account_public_id is not None:
            account = await self.account_repo.get_by_public_id(workspace_id, account_public_id)
            if not account or not account.is_active:
                raise NotFoundError(
                    detail=(
                        f"Account with id {account_public_id} not found in this workspace. "
                        "Cross-workspace account references are not permitted."
                    )
                )
            return account.id  # type: ignore[return-value]

        if self.setting_repo is not None:
            setting = await self.setting_repo.get_by_workspace(workspace_id)
            if setting and setting.default_spending_account_id is not None:
                # Defense in depth: the default is cleared when its account
                # is deactivated through the API (AccountService.update_account),
                # but don't trust that path alone — re-check is_active here.
                default_account = await self.account_repo.get_by_id(
                    workspace_id, setting.default_spending_account_id
                )
                if default_account and default_account.is_active:
                    return setting.default_spending_account_id

        raise ValidationError(
            detail=("Provide account_id or set a default spending account in Finance Settings.")
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

        # 1. Sum budgets by category
        budget_stmt = (
            select(
                SpendingCategory.id,
                SpendingCategory.public_id,
                SpendingCategory.name,
                func.sum(SpendingBudget.amount).label("budget_amount"),
            )
            .join(SpendingBudget, SpendingBudget.category_id == SpendingCategory.id)
            .where(
                SpendingBudget.workspace_id == workspace_id,
                SpendingBudget.month_start >= from_month,
                SpendingBudget.month_start <= to_month,
            )
            .group_by(SpendingCategory.id, SpendingCategory.public_id, SpendingCategory.name)
        )
        budget_rows = (await self.budget_repo.session.execute(budget_stmt)).all()

        # 2. Sum transactions (expense type) by category
        tx_stmt = (
            select(
                SpendingCategory.id,
                SpendingCategory.public_id,
                SpendingCategory.name,
                func.sum(SpendingTransaction.amount).label("actual_amount"),
            )
            .join(SpendingTransaction, SpendingTransaction.category_id == SpendingCategory.id)
            .where(
                SpendingTransaction.workspace_id == workspace_id,
                SpendingTransaction.type == TransactionType.expense.value,
                SpendingTransaction.occurred_at >= start_dt,
                SpendingTransaction.occurred_at < end_dt,
            )
            .group_by(SpendingCategory.id, SpendingCategory.public_id, SpendingCategory.name)
        )
        tx_rows = (await self.budget_repo.session.execute(tx_stmt)).all()

        categories_map = {}
        for row in budget_rows:
            categories_map[row.id] = {
                "public_id": row.public_id,
                "name": row.name,
                "budget_amount": Decimal(row.budget_amount),
                "actual_amount": Decimal("0"),
            }

        for row in tx_rows:
            if row.id in categories_map:
                categories_map[row.id]["actual_amount"] = Decimal(row.actual_amount)
            else:
                categories_map[row.id] = {
                    "public_id": row.public_id,
                    "name": row.name,
                    "budget_amount": None,
                    "actual_amount": Decimal(row.actual_amount),
                }

        items: list[BudgetPerformanceItem] = []
        for data in categories_map.values():
            budget_amount = data["budget_amount"]
            actual_amount = data["actual_amount"]

            if budget_amount is None or budget_amount == 0:
                utilization_pct = None
                remaining = -actual_amount if budget_amount == 0 else None
                status = "exceeded" if actual_amount > 0 else "on_track"
            else:
                utilization_pct = float((actual_amount / budget_amount) * 100)
                remaining = budget_amount - actual_amount
                if utilization_pct < 90.0:
                    status = "on_track"
                elif utilization_pct <= 100.0:
                    status = "warning"
                else:
                    status = "exceeded"

            items.append(
                BudgetPerformanceItem(
                    category_id=data["public_id"],
                    category_name=data["name"],
                    budget_amount=budget_amount,
                    actual_amount=actual_amount,
                    utilization_pct=utilization_pct,
                    remaining=remaining,
                    status=status,
                )
            )

        # Calculate totals
        total_budgeted = sum(
            item["budget_amount"]
            for item in categories_map.values()
            if item["budget_amount"] is not None
        )
        total_actual = sum(item["actual_amount"] for item in categories_map.values())
        overall_utilization = (
            float((total_actual / total_budgeted) * 100) if total_budgeted > 0 else None
        )

        return BudgetPerformanceResponse(
            from_month=from_month.strftime("%Y-%m"),
            to_month=to_month.strftime("%Y-%m"),
            categories=items,
            totals=BudgetPerformanceTotals(
                total_budgeted=total_budgeted,
                total_actual=total_actual,
                overall_utilization_pct=overall_utilization,
            ),
        )


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
