from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import and_, case, func, literal, or_, select, union_all

from app.core.pagination import DEFAULT_LIMIT
from app.core.repository import BaseRepository
from app.finance.models import CapitalTransfer
from app.spending.models import (
    CategoryGroup,
    FinancialKpi,
    RecurringTransaction,
    SpendingBudget,
    SpendingCategory,
    SpendingTag,
    SpendingTransaction,
    SpendingTransactionTag,
    TransactionSort,
)


def _transaction_order_by(sort: TransactionSort | None) -> list:  # type: ignore[type-arg]
    """Build the ORDER BY clause for a transaction listing.

    ``created_at`` (then ``id``) is always appended as a tiebreaker so results
    are deterministic and pagination stays stable when the primary sort key
    ties. ``sort=None`` preserves the historical default (newest-created first).
    """
    clauses = []
    if sort == TransactionSort.date_desc:
        clauses.append(SpendingTransaction.occurred_at.desc())
    elif sort == TransactionSort.date_asc:
        clauses.append(SpendingTransaction.occurred_at.asc())
    elif sort == TransactionSort.amount_desc:
        clauses.append(SpendingTransaction.amount.desc())
    elif sort == TransactionSort.amount_asc:
        clauses.append(SpendingTransaction.amount.asc())

    clauses.extend([SpendingTransaction.created_at.desc(), SpendingTransaction.id.desc()])
    return clauses


@dataclass
class LedgerRow:
    """Unified row type returned by get_ledger_page representing either a
    spending transaction or a capital transfer event for an account."""

    id: int
    public_id: UUID
    occurred_at: datetime
    amount: Decimal
    entry_kind: str  # "transaction" | "transfer_out" | "transfer_in"
    type: str | None  # "income" | "expense" | None (None for transfers)
    description: str | None
    wallet_name: str | None
    labels: str | None
    source_type: str
    category_id: int | None
    created_at: datetime


class CategoryGroupRepository(BaseRepository[CategoryGroup]):
    async def get_all(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[CategoryGroup], int]:
        base = select(CategoryGroup).where(CategoryGroup.workspace_id == workspace_id)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(CategoryGroup.created_at.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> CategoryGroup | None:
        result = await self.session.execute(
            select(CategoryGroup).where(
                CategoryGroup.workspace_id == workspace_id,
                CategoryGroup.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, workspace_id: int, group_id: int) -> CategoryGroup | None:
        result = await self.session.execute(
            select(CategoryGroup).where(
                CategoryGroup.workspace_id == workspace_id,
                CategoryGroup.id == group_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_normalized_name(
        self, workspace_id: int, normalized_name: str
    ) -> CategoryGroup | None:
        result = await self.session.execute(
            select(CategoryGroup).where(
                CategoryGroup.workspace_id == workspace_id,
                CategoryGroup.normalized_name == normalized_name,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_ids(self, workspace_id: int, ids: set[int]) -> dict[int, CategoryGroup]:
        if not ids:
            return {}
        result = await self.session.execute(
            select(CategoryGroup).where(
                CategoryGroup.workspace_id == workspace_id,
                CategoryGroup.id.in_(ids),
            )
        )
        return {group.id: group for group in result.scalars().all() if group.id is not None}


class CategoryRepository(BaseRepository[SpendingCategory]):
    async def get_all(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[SpendingCategory], int]:
        base = select(SpendingCategory).where(SpendingCategory.workspace_id == workspace_id)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(SpendingCategory.created_at.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> SpendingCategory | None:
        result = await self.session.execute(
            select(SpendingCategory).where(
                SpendingCategory.workspace_id == workspace_id,
                SpendingCategory.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, workspace_id: int, category_id: int) -> SpendingCategory | None:
        result = await self.session.execute(
            select(SpendingCategory).where(
                SpendingCategory.workspace_id == workspace_id,
                SpendingCategory.id == category_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_normalized_name(
        self, workspace_id: int, normalized_name: str
    ) -> SpendingCategory | None:
        result = await self.session.execute(
            select(SpendingCategory).where(
                SpendingCategory.workspace_id == workspace_id,
                SpendingCategory.normalized_name == normalized_name,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_ids(self, workspace_id: int, ids: set[int]) -> dict[int, SpendingCategory]:
        if not ids:
            return {}
        result = await self.session.execute(
            select(SpendingCategory).where(
                SpendingCategory.workspace_id == workspace_id,
                SpendingCategory.id.in_(ids),
            )
        )
        return {
            category.id: category for category in result.scalars().all() if category.id is not None
        }

    async def has_usage(self, category_id: int) -> bool:
        tx_exists = (
            await self.session.execute(
                select(SpendingTransaction.id)
                .where(SpendingTransaction.category_id == category_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if tx_exists is not None:
            return True

        budget_exists = (
            await self.session.execute(
                select(SpendingBudget.id).where(SpendingBudget.category_id == category_id).limit(1)
            )
        ).scalar_one_or_none()
        if budget_exists is not None:
            return True

        recurring_exists = (
            await self.session.execute(
                select(RecurringTransaction.id)
                .where(RecurringTransaction.category_id == category_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        return recurring_exists is not None

    async def create_many(self, categories: list[SpendingCategory]) -> None:
        """Bulk insert categories (used during workspace provisioning)."""
        for category in categories:
            self.session.add(category)
        await self.session.flush()

    async def reassign_transactions(
        self, workspace_id: int, source_ids: list[int], target_id: int
    ) -> int:
        stmt = (
            sa
            .update(SpendingTransaction)
            .where(
                SpendingTransaction.workspace_id == workspace_id,
                SpendingTransaction.category_id.in_(source_ids),
            )
            .values(category_id=target_id)
        )
        result = await self.session.execute(stmt)
        return result.rowcount

    async def reassign_recurring_rules(
        self, workspace_id: int, source_ids: list[int], target_id: int
    ) -> int:
        stmt = (
            sa
            .update(RecurringTransaction)
            .where(
                RecurringTransaction.workspace_id == workspace_id,
                RecurringTransaction.category_id.in_(source_ids),
            )
            .values(category_id=target_id)
        )
        result = await self.session.execute(stmt)
        return result.rowcount

    async def ungroup_categories(self, workspace_id: int, group_id: int) -> None:
        stmt = (
            sa
            .update(SpendingCategory)
            .where(
                SpendingCategory.workspace_id == workspace_id,
                SpendingCategory.category_group_id == group_id,
            )
            .values(category_group_id=None)
        )
        await self.session.execute(stmt)


class TagRepository(BaseRepository[SpendingTag]):
    async def get_all(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        search: str | None = None,
    ) -> tuple[Sequence[SpendingTag], int]:
        base = select(SpendingTag).where(SpendingTag.workspace_id == workspace_id)
        if search and search.strip():
            base = base.where(SpendingTag.name.ilike(f"%{search.strip()}%"))
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(SpendingTag.name.asc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> SpendingTag | None:
        result = await self.session.execute(
            select(SpendingTag).where(
                SpendingTag.workspace_id == workspace_id,
                SpendingTag.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_normalized_name(
        self, workspace_id: int, normalized_name: str
    ) -> SpendingTag | None:
        result = await self.session.execute(
            select(SpendingTag).where(
                SpendingTag.workspace_id == workspace_id,
                SpendingTag.normalized_name == normalized_name,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_ids(self, workspace_id: int, ids: set[int]) -> dict[int, SpendingTag]:
        if not ids:
            return {}
        result = await self.session.execute(
            select(SpendingTag).where(
                SpendingTag.workspace_id == workspace_id,
                SpendingTag.id.in_(ids),
            )
        )
        return {tag.id: tag for tag in result.scalars().all() if tag.id is not None}

    async def get_for_transactions(
        self, workspace_id: int, transaction_ids: set[int]
    ) -> dict[int, list[SpendingTag]]:
        if not transaction_ids:
            return {}
        result = await self.session.execute(
            select(SpendingTransactionTag.transaction_id, SpendingTag)
            .join(SpendingTag, SpendingTag.id == SpendingTransactionTag.tag_id)
            .where(
                SpendingTransactionTag.workspace_id == workspace_id,
                SpendingTransactionTag.transaction_id.in_(transaction_ids),
            )
            .order_by(SpendingTag.name.asc())
        )
        grouped: dict[int, list[SpendingTag]] = {}
        for transaction_id, tag in result.all():
            grouped.setdefault(transaction_id, []).append(tag)
        return grouped

    async def replace_transaction_tags(
        self, workspace_id: int, transaction_id: int, tag_ids: set[int]
    ) -> None:
        await self.session.execute(
            sa.delete(SpendingTransactionTag).where(
                SpendingTransactionTag.workspace_id == workspace_id,
                SpendingTransactionTag.transaction_id == transaction_id,
            )
        )
        if tag_ids:
            self.session.add_all([
                SpendingTransactionTag(
                    workspace_id=workspace_id,
                    transaction_id=transaction_id,
                    tag_id=tag_id,
                )
                for tag_id in tag_ids
            ])
        await self.session.flush()


class TransactionRepository(BaseRepository[SpendingTransaction]):
    async def get_all(
        self,
        workspace_id: int,
        category_id: int | None = None,
        account_id: int | None = None,
        unassigned_only: bool = False,
        type_filter: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        search: str | None = None,
        amount: Decimal | None = None,
        tag_id: int | None = None,
        sort: TransactionSort | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[Sequence[SpendingTransaction], int]:
        base = select(SpendingTransaction).where(SpendingTransaction.workspace_id == workspace_id)
        if category_id is not None:
            base = base.where(SpendingTransaction.category_id == category_id)
        if unassigned_only:
            base = base.where(SpendingTransaction.account_id.is_(None))
        elif account_id is not None:
            base = base.where(SpendingTransaction.account_id == account_id)
        if type_filter is not None:
            base = base.where(SpendingTransaction.type == type_filter)
        if from_date is not None:
            base = base.where(SpendingTransaction.occurred_at >= from_date)
        if to_date is not None:
            base = base.where(SpendingTransaction.occurred_at <= to_date)
        if amount is not None:
            base = base.where(SpendingTransaction.amount == amount)
        if search and search.strip():
            search_pattern = f"%{search.strip()}%"
            matching_tag = (
                select(SpendingTransactionTag.transaction_id)
                .join(SpendingTag, SpendingTag.id == SpendingTransactionTag.tag_id)
                .where(
                    SpendingTransactionTag.workspace_id == workspace_id,
                    SpendingTransactionTag.transaction_id == SpendingTransaction.id,
                    SpendingTag.name.ilike(search_pattern),
                )
                .correlate(SpendingTransaction)
                .exists()
            )
            base = base.where(
                sa.or_(
                    SpendingTransaction.description.ilike(search_pattern),
                    SpendingTransaction.wallet_name.ilike(search_pattern),
                    SpendingTransaction.labels.ilike(search_pattern),
                    matching_tag,
                )
            )
        if tag_id is not None:
            base = base.where(
                select(SpendingTransactionTag.transaction_id)
                .where(
                    SpendingTransactionTag.workspace_id == workspace_id,
                    SpendingTransactionTag.transaction_id == SpendingTransaction.id,
                    SpendingTransactionTag.tag_id == tag_id,
                )
                .correlate(SpendingTransaction)
                .exists()
            )
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(*_transaction_order_by(sort)).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def find_same_day_duplicates(
        self,
        workspace_id: int,
        *,
        category_id: int,
        account_id: int | None,
        amount: Decimal,
        from_date: datetime,
        to_date: datetime,
        description: str | None,
        limit: int = 10,
    ) -> Sequence[SpendingTransaction]:
        """Find likely duplicate expenses within one local calendar day."""
        where = [
            SpendingTransaction.workspace_id == workspace_id,
            SpendingTransaction.type == "expense",
            SpendingTransaction.category_id == category_id,
            SpendingTransaction.amount == amount,
            SpendingTransaction.occurred_at >= from_date,
            SpendingTransaction.occurred_at < to_date,
        ]
        if account_id is None:
            where.append(SpendingTransaction.account_id.is_(None))
        else:
            where.append(SpendingTransaction.account_id == account_id)

        result = await self.session.execute(
            select(SpendingTransaction)
            .where(*where)
            .order_by(SpendingTransaction.occurred_at.desc(), SpendingTransaction.id.desc())
            .limit(limit)
        )
        normalized_description = " ".join((description or "").strip().lower().split())
        return [
            transaction
            for transaction in result.scalars().all()
            if " ".join((transaction.description or "").strip().lower().split())
            == normalized_description
        ]

    async def get_sum_by_type(
        self,
        workspace_id: int,
        type_filter: str,
        from_date: datetime,
        to_date: datetime,
        category_id: int | None = None,
        account_id: int | None = None,
        category_group_id: int | None = None,
    ) -> Decimal:
        query = select(func.sum(SpendingTransaction.amount)).where(
            SpendingTransaction.workspace_id == workspace_id,
            SpendingTransaction.type == type_filter,
            SpendingTransaction.occurred_at >= from_date,
            SpendingTransaction.occurred_at <= to_date,
        )
        if category_id is not None:
            query = query.where(SpendingTransaction.category_id == category_id)
        if account_id is not None:
            query = query.where(SpendingTransaction.account_id == account_id)
        if category_group_id is not None:
            query = query.join(
                SpendingCategory, SpendingCategory.id == SpendingTransaction.category_id
            ).where(SpendingCategory.category_group_id == category_group_id)
        result = await self.session.execute(query)
        val = result.scalar_one_or_none()
        return Decimal(val or 0)

    async def get_category_totals(
        self,
        workspace_id: int,
        from_date: datetime,
        to_date: datetime,
        type_filter: str | None = None,
        account_id: int | None = None,
    ) -> Sequence[tuple[int, Decimal]]:
        query = (
            select(
                SpendingTransaction.category_id,
                func.sum(SpendingTransaction.amount).label("total"),
            )
            .where(
                SpendingTransaction.workspace_id == workspace_id,
                SpendingTransaction.occurred_at >= from_date,
                SpendingTransaction.occurred_at <= to_date,
            )
            .group_by(SpendingTransaction.category_id)
        )
        if type_filter is not None:
            query = query.where(SpendingTransaction.type == type_filter)
        if account_id is not None:
            query = query.where(SpendingTransaction.account_id == account_id)
        result = await self.session.execute(query)
        rows = result.all()
        return [(category_id, Decimal(total or 0)) for category_id, total in rows]

    async def get_by_public_id(
        self, workspace_id: int, public_id: UUID
    ) -> SpendingTransaction | None:
        result = await self.session.execute(
            select(SpendingTransaction).where(
                SpendingTransaction.workspace_id == workspace_id,
                SpendingTransaction.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_ledger_page(
        self,
        workspace_id: int,
        account_id: int,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[list[LedgerRow], int]:
        """Return paginated ledger entries for an account ordered by occurred_at DESC.

        Includes both spending_transactions AND capital_transfers in the same
        timeline, giving each a consistent entry_kind discriminator:
          - ``transaction``  : spending income/expense row
          - ``transfer_out`` : capital transfer leaving this account
          - ``transfer_in``  : capital transfer arriving at this account

        Returns (ledger_rows, total_count_for_account).
        """
        # --- Leg 1: spending transactions for this account ------------------
        tx_where = [
            SpendingTransaction.workspace_id == workspace_id,
            SpendingTransaction.account_id == account_id,
        ]
        if from_date is not None:
            tx_where.append(SpendingTransaction.occurred_at >= from_date)
        if to_date is not None:
            tx_where.append(SpendingTransaction.occurred_at <= to_date)

        tx_select = select(
            SpendingTransaction.id.label("id"),
            SpendingTransaction.public_id.label("public_id"),
            SpendingTransaction.occurred_at.label("occurred_at"),
            SpendingTransaction.amount.label("amount"),
            literal("transaction", type_=sa.String()).label("entry_kind"),
            SpendingTransaction.type.label("type"),
            SpendingTransaction.description.label("description"),
            SpendingTransaction.wallet_name.label("wallet_name"),
            SpendingTransaction.labels.label("labels"),
            SpendingTransaction.source_type.label("source_type"),
            SpendingTransaction.category_id.label("category_id"),
            SpendingTransaction.created_at.label("created_at"),
        ).where(*tx_where)

        # --- Leg 2: capital transfers involving this account -----------------
        xfer_where = [
            CapitalTransfer.workspace_id == workspace_id,
            or_(
                CapitalTransfer.from_account_id == account_id,
                CapitalTransfer.to_account_id == account_id,
            ),
        ]
        if from_date is not None:
            xfer_where.append(CapitalTransfer.occurred_at >= from_date)
        if to_date is not None:
            xfer_where.append(CapitalTransfer.occurred_at <= to_date)

        xfer_select = select(
            CapitalTransfer.id.label("id"),
            CapitalTransfer.public_id.label("public_id"),
            CapitalTransfer.occurred_at.label("occurred_at"),
            # net_amount_received: what the destination account actually received,
            # in to_currency (correct for the receiving leg). gross_amount is in
            # from_currency and is only correct for the leg that lost it. See the
            # matching comment in app/finance/repository.py's reconciliation query.
            case(
                (CapitalTransfer.from_account_id == account_id, CapitalTransfer.gross_amount),
                else_=CapitalTransfer.net_amount_received,
            ).label("amount"),
            case(
                (CapitalTransfer.from_account_id == account_id, literal("transfer_out")),
                else_=literal("transfer_in"),
            ).label("entry_kind"),
            # Transfers don't have a transaction type; service uses entry_kind instead
            literal(None, type_=sa.String()).label("type"),
            CapitalTransfer.notes.label("description"),
            literal(None, type_=sa.String()).label("wallet_name"),
            literal(None, type_=sa.String()).label("labels"),
            CapitalTransfer.source_type.label("source_type"),
            literal(None, type_=sa.Integer()).label("category_id"),
            CapitalTransfer.created_at.label("created_at"),
        ).where(*xfer_where)

        combined = union_all(tx_select, xfer_select).alias("ledger_entries")

        # Total count (without pagination)
        total = (
            await self.session.execute(select(func.count()).select_from(combined))
        ).scalar_one()

        # Paged result ordered most-recent first
        paged = (
            select(combined)
            .order_by(
                combined.c.occurred_at.desc(),
                combined.c.entry_kind.desc(),
                combined.c.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )

        rows = (await self.session.execute(paged)).mappings().all()

        result: list[LedgerRow] = [
            LedgerRow(
                id=int(r["id"]),
                public_id=r["public_id"],
                occurred_at=r["occurred_at"],
                amount=Decimal(str(r["amount"])),
                entry_kind=str(r["entry_kind"]),
                type=r["type"],
                description=r["description"],
                wallet_name=r["wallet_name"],
                labels=r["labels"],
                source_type=str(r["source_type"]),
                category_id=r["category_id"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
        return result, int(total)

    async def get_account_net_balance(
        self,
        workspace_id: int,
        account_id: int,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        before_row: LedgerRow | None = None,
    ) -> Decimal:
        """Return the net balance of an account including both spending transactions
        AND capital transfer contributions.

        balance = SUM(income txns) - SUM(expense txns)
                + SUM(transfer_in net_amount_received) - SUM(transfer_out gross_amount)

        ``before_row`` restricts the query to entries strictly preceding that row
        (used for computing the running balance tail in paginated ledger views).
        """
        # Spending transactions net: income - expense
        tx_where = [
            SpendingTransaction.workspace_id == workspace_id,
            SpendingTransaction.account_id == account_id,
        ]
        if from_date is not None:
            tx_where.append(SpendingTransaction.occurred_at >= from_date)
        if to_date is not None:
            tx_where.append(SpendingTransaction.occurred_at <= to_date)
        if before_row is not None:
            tx_where.append(
                or_(
                    SpendingTransaction.occurred_at < before_row.occurred_at,
                    and_(
                        SpendingTransaction.occurred_at == before_row.occurred_at,
                        SpendingTransaction.id < before_row.id,
                    ),
                )
            )

        tx_net_stmt = select(
            func.coalesce(
                func.sum(
                    case(
                        (SpendingTransaction.type == "income", SpendingTransaction.amount),
                        else_=SpendingTransaction.amount * -1,
                    )
                ),
                Decimal("0"),
            )
        ).where(*tx_where)

        tx_net = Decimal(
            str((await self.session.execute(tx_net_stmt)).scalar_one() or Decimal("0"))
        )

        # Capital transfers net: inflows - outflows
        xfer_where_in = [
            CapitalTransfer.workspace_id == workspace_id,
            CapitalTransfer.to_account_id == account_id,
        ]
        xfer_where_out = [
            CapitalTransfer.workspace_id == workspace_id,
            CapitalTransfer.from_account_id == account_id,
        ]

        def _apply_date_filters(where_list: list, occurred_at_col: sa.Column) -> None:  # type: ignore[type-arg]
            if from_date is not None:
                where_list.append(occurred_at_col >= from_date)
            if to_date is not None:
                where_list.append(occurred_at_col <= to_date)
            if before_row is not None:
                where_list.append(
                    or_(
                        occurred_at_col < before_row.occurred_at,
                        and_(
                            occurred_at_col == before_row.occurred_at,
                            CapitalTransfer.id < before_row.id,
                        ),
                    )
                )

        _apply_date_filters(xfer_where_in, CapitalTransfer.occurred_at)
        _apply_date_filters(xfer_where_out, CapitalTransfer.occurred_at)

        # net_amount_received (not gross_amount) for inflows — see the comment on
        # the matching CASE in get_ledger_page above.
        inflow_stmt = select(
            func.coalesce(func.sum(CapitalTransfer.net_amount_received), Decimal("0"))
        ).where(*xfer_where_in)
        outflow_stmt = select(
            func.coalesce(func.sum(CapitalTransfer.gross_amount), Decimal("0"))
        ).where(*xfer_where_out)

        inflow = Decimal(
            str((await self.session.execute(inflow_stmt)).scalar_one() or Decimal("0"))
        )
        outflow = Decimal(
            str((await self.session.execute(outflow_stmt)).scalar_one() or Decimal("0"))
        )

        return tx_net + inflow - outflow


class BudgetRepository(BaseRepository[SpendingBudget]):
    async def get_all(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        month_start: date | None = None,
    ) -> tuple[Sequence[SpendingBudget], int]:
        base = select(SpendingBudget).where(SpendingBudget.workspace_id == workspace_id)
        if month_start is not None:
            base = base.where(
                and_(
                    SpendingBudget.start_month <= month_start,
                    or_(
                        SpendingBudget.end_month.is_(None),
                        SpendingBudget.end_month >= month_start,
                    ),
                )
            )
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(SpendingBudget.created_at.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> SpendingBudget | None:
        result = await self.session.execute(
            select(SpendingBudget).where(
                SpendingBudget.workspace_id == workspace_id,
                SpendingBudget.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_category_and_month(
        self, workspace_id: int, category_id: int, month_start: date
    ) -> SpendingBudget | None:
        result = await self.session.execute(
            select(SpendingBudget).where(
                SpendingBudget.workspace_id == workspace_id,
                SpendingBudget.category_id == category_id,
                SpendingBudget.start_month <= month_start,
                or_(
                    SpendingBudget.end_month.is_(None),
                    SpendingBudget.end_month >= month_start,
                ),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_group_and_month(
        self, workspace_id: int, category_group_id: int, month_start: date
    ) -> SpendingBudget | None:
        result = await self.session.execute(
            select(SpendingBudget).where(
                SpendingBudget.workspace_id == workspace_id,
                SpendingBudget.category_group_id == category_group_id,
                SpendingBudget.start_month <= month_start,
                or_(
                    SpendingBudget.end_month.is_(None),
                    SpendingBudget.end_month >= month_start,
                ),
            )
        )
        return result.scalar_one_or_none()

    async def get_overlapping_budgets(
        self,
        workspace_id: int,
        category_id: int | None,
        category_group_id: int | None,
        start_month: date,
        end_month: date | None,
        exclude_id: int | None = None,
    ) -> list[SpendingBudget]:
        conds = [SpendingBudget.workspace_id == workspace_id]
        if category_id is not None:
            conds.append(SpendingBudget.category_id == category_id)
        else:
            conds.append(SpendingBudget.category_group_id == category_group_id)

        overlap_conds = [
            or_(
                SpendingBudget.end_month.is_(None),
                SpendingBudget.end_month >= start_month,
            )
        ]
        if end_month is not None:
            overlap_conds.append(SpendingBudget.start_month <= end_month)

        conds.append(and_(*overlap_conds))

        if exclude_id is not None:
            conds.append(SpendingBudget.id != exclude_id)

        result = await self.session.execute(select(SpendingBudget).where(*conds))
        return list(result.scalars().all())

    async def get_by_category_ids(
        self, workspace_id: int, category_ids: list[int]
    ) -> list[SpendingBudget]:
        result = await self.session.execute(
            select(SpendingBudget).where(
                SpendingBudget.workspace_id == workspace_id,
                SpendingBudget.category_id.in_(category_ids),
            )
        )
        return list(result.scalars().all())

    async def delete_by_category_ids(self, workspace_id: int, category_ids: list[int]) -> None:
        if workspace_id is None or not category_ids:
            return
        stmt = sa.delete(SpendingBudget).where(
            SpendingBudget.workspace_id == workspace_id,
            SpendingBudget.category_id.in_(category_ids),
        )
        await self.session.execute(stmt)

    async def delete_by_group_id(self, workspace_id: int, category_group_id: int) -> None:
        if workspace_id is None or category_group_id is None:
            return
        stmt = sa.delete(SpendingBudget).where(
            SpendingBudget.workspace_id == workspace_id,
            SpendingBudget.category_group_id == category_group_id,
        )
        await self.session.execute(stmt)

    async def has_current_or_future_budget(self, workspace_id: int, category_group_id: int) -> bool:
        today = date.today()
        first_of_month = date(today.year, today.month, 1)
        result = await self.session.execute(
            select(SpendingBudget.id)
            .where(
                SpendingBudget.workspace_id == workspace_id,
                SpendingBudget.category_group_id == category_group_id,
                or_(
                    SpendingBudget.end_month.is_(None),
                    SpendingBudget.end_month >= first_of_month,
                ),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def get_month_total(self, workspace_id: int, month_start: date) -> Decimal:
        query = select(func.sum(SpendingBudget.amount)).where(
            SpendingBudget.workspace_id == workspace_id,
            SpendingBudget.category_id.is_not(None),
            SpendingBudget.start_month <= month_start,
            or_(
                SpendingBudget.end_month.is_(None),
                SpendingBudget.end_month >= month_start,
            ),
        )
        result = await self.session.execute(query)
        total = result.scalar_one_or_none()
        return Decimal(total or 0)


class RecurringTransactionRepository(BaseRepository[RecurringTransaction]):
    async def get_all(
        self,
        workspace_id: int,
        is_active: bool | None = True,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[Sequence[RecurringTransaction], int]:
        base = select(RecurringTransaction).where(RecurringTransaction.workspace_id == workspace_id)
        if is_active is not None:
            base = base.where(RecurringTransaction.is_active == is_active)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(RecurringTransaction.created_at.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), int(total)

    async def get_by_public_id(
        self, workspace_id: int, public_id: UUID
    ) -> RecurringTransaction | None:
        return (
            await self.session.execute(
                select(RecurringTransaction).where(
                    RecurringTransaction.workspace_id == workspace_id,
                    RecurringTransaction.public_id == public_id,
                )
            )
        ).scalar_one_or_none()

    async def get_due_between(
        self, workspace_id: int, start_date: date, end_date: date
    ) -> Sequence[RecurringTransaction]:
        """Active recurring rules whose next occurrence falls in [start_date,
        end_date] — used by the morning briefing's "recurring due soon" line
        (spec-067). Ordered by next_due_date so the earliest is named first."""
        result = await self.session.execute(
            select(RecurringTransaction)
            .where(
                RecurringTransaction.workspace_id == workspace_id,
                RecurringTransaction.is_active == True,  # noqa: E712
                RecurringTransaction.next_due_date >= start_date,
                RecurringTransaction.next_due_date <= end_date,
            )
            .order_by(RecurringTransaction.next_due_date.asc())
        )
        return result.scalars().all()


class KpiRepository(BaseRepository[FinancialKpi]):
    async def get_all(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[Sequence[FinancialKpi], int]:
        base = select(FinancialKpi).where(FinancialKpi.workspace_id == workspace_id)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(FinancialKpi.created_at.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def get_active(self, workspace_id: int) -> Sequence[FinancialKpi]:
        result = await self.session.execute(
            select(FinancialKpi).where(
                FinancialKpi.workspace_id == workspace_id,
                FinancialKpi.is_active == True,  # noqa: E712
            )
        )
        return result.scalars().all()

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> FinancialKpi | None:
        result = await self.session.execute(
            select(FinancialKpi).where(
                FinancialKpi.workspace_id == workspace_id,
                FinancialKpi.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()
