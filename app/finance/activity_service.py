"""Unified activity-feed service (spec-097).

Queries spending transactions, capital transfers, investing orders, and
dividends into a chronological stream.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.exceptions import ValidationError
from app.finance.activity_schemas import ActivityFeedItem, ActivityFeedResponse
from app.finance.models import Account, CapitalTransfer
from app.finance.repository import AccountRepository
from app.investing.models import Dividend, InvestingOrder
from app.spending.models import SpendingCategory, SpendingTransaction, TransactionType


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class ActivityFeedService:
    def __init__(
        self,
        session: AsyncSession,
        account_repo: AccountRepository,
    ):
        self.session = session
        self.account_repo = account_repo

    async def get_activity_feed(
        self,
        workspace_id: int,
        *,
        account_id: uuid.UUID | None = None,
        event_types: list[str] | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ActivityFeedResponse:
        # Load all workspace accounts into a dictionary
        accounts_seq, _ = await self.account_repo.list_workspace_accounts(workspace_id, limit=200)
        account_map: dict[int, Account] = {a.id: a for a in accounts_seq if a.id is not None}
        public_id_map: dict[uuid.UUID, Account] = {
            a.public_id: a for a in accounts_seq if a.public_id is not None
        }

        filter_internal_acc_id: int | None = None
        if account_id is not None:
            acc = public_id_map.get(account_id)
            if not acc:
                raise ValidationError(detail=f"Account with id {account_id} not found")
            filter_internal_acc_id = acc.id

        allowed_types = {"spend", "transfer", "order", "dividend"}
        selected_types = set(event_types) if event_types else allowed_types

        items: list[ActivityFeedItem] = []

        # 1. SPEND transactions
        if "spend" in selected_types:
            stmt = select(SpendingTransaction).where(
                SpendingTransaction.workspace_id == workspace_id
            )
            if filter_internal_acc_id is not None:
                stmt = stmt.where(SpendingTransaction.account_id == filter_internal_acc_id)
            if from_date is not None:
                stmt = stmt.where(SpendingTransaction.occurred_at >= from_date)
            if to_date is not None:
                stmt = stmt.where(SpendingTransaction.occurred_at <= to_date)

            res = await self.session.execute(stmt)
            txs = res.scalars().all()

            # Preload categories
            cat_stmt = select(SpendingCategory).where(SpendingCategory.workspace_id == workspace_id)
            cat_res = await self.session.execute(cat_stmt)
            cat_map: dict[int, SpendingCategory] = {
                c.id: c for c in cat_res.scalars().all() if c.id is not None
            }

            for tx in txs:
                # Omit auto-generated dividend transactions from spend feed
                # so they appear under dividend instead
                if tx.source_ref and tx.source_ref.startswith("dividend:"):
                    continue

                acc = account_map.get(tx.account_id) if tx.account_id else None
                cat = cat_map.get(tx.category_id) if tx.category_id else None

                amt = tx.amount
                amt_str = f"+{amt:.2f}" if tx.type == TransactionType.income else f"-{amt:.2f}"

                acc_id = acc.public_id if acc else uuid.UUID(int=0)
                acc_name = acc.name if acc else "Unknown Account"
                acc_type = acc.account_type if acc else "wallet"
                curr = acc.default_currency_code if acc else "INR"

                items.append(
                    ActivityFeedItem(
                        id=tx.public_id,
                        event_type="spend",
                        date=_ensure_utc(tx.occurred_at),
                        description=tx.description or (cat.name if cat else "Transaction"),
                        amount=amt_str,
                        currency=curr,
                        account_id=acc_id,
                        account_name=acc_name,
                        account_type=acc_type,
                        category_name=cat.name if cat else None,
                        category_color=cat.color if cat else None,
                        category_icon=cat.icon if cat else None,
                        source_ref=tx.public_id,
                    )
                )

        # 2. TRANSFERS
        if "transfer" in selected_types:
            stmt = select(CapitalTransfer).where(CapitalTransfer.workspace_id == workspace_id)
            if filter_internal_acc_id is not None:
                stmt = stmt.where(
                    (CapitalTransfer.from_account_id == filter_internal_acc_id)
                    | (CapitalTransfer.to_account_id == filter_internal_acc_id)
                )
            if from_date is not None:
                stmt = stmt.where(CapitalTransfer.occurred_at >= from_date)
            if to_date is not None:
                stmt = stmt.where(CapitalTransfer.occurred_at <= to_date)

            res = await self.session.execute(stmt)
            transfers = res.scalars().all()

            for tf in transfers:
                from_acc = account_map.get(tf.from_account_id)
                to_acc = account_map.get(tf.to_account_id)
                if not from_acc or not to_acc:
                    continue

                fx_display = None
                if tf.fx_rate_used and tf.from_currency_code != tf.to_currency_code:
                    if tf.fx_rate_used < 1:
                        inv = round(Decimal("1") / tf.fx_rate_used, 2)
                        fx_display = f"{inv} {tf.from_currency_code}/{tf.to_currency_code}"
                    else:
                        fx_display = f"{round(tf.fx_rate_used, 4)} {tf.to_currency_code}/{tf.from_currency_code}"

                # Outflow item
                if filter_internal_acc_id is None or tf.from_account_id == filter_internal_acc_id:
                    outflow_id = uuid.uuid5(uuid.NAMESPACE_OID, f"transfer:{tf.public_id}:outflow")
                    items.append(
                        ActivityFeedItem(
                            id=outflow_id,
                            event_type="transfer",
                            date=_ensure_utc(tf.occurred_at),
                            description=f"Transfer to {to_acc.name}",
                            amount=f"-{tf.gross_amount:.2f}",
                            currency=tf.from_currency_code,
                            account_id=from_acc.public_id,
                            account_name=from_acc.name,
                            account_type=from_acc.account_type,
                            counterpart_account_id=to_acc.public_id,
                            counterpart_account_name=to_acc.name,
                            fx_rate=str(tf.fx_rate_used) if tf.fx_rate_used else None,
                            fx_display=fx_display,
                            source_ref=tf.public_id,
                        )
                    )

                # Inflow item
                if filter_internal_acc_id is None or tf.to_account_id == filter_internal_acc_id:
                    inflow_id = uuid.uuid5(uuid.NAMESPACE_OID, f"transfer:{tf.public_id}:inflow")
                    items.append(
                        ActivityFeedItem(
                            id=inflow_id,
                            event_type="transfer",
                            date=_ensure_utc(tf.occurred_at),
                            description=f"Transfer from {from_acc.name}",
                            amount=f"+{tf.net_amount_received:.2f}",
                            currency=tf.to_currency_code,
                            account_id=to_acc.public_id,
                            account_name=to_acc.name,
                            account_type=to_acc.account_type,
                            counterpart_account_id=from_acc.public_id,
                            counterpart_account_name=from_acc.name,
                            fx_rate=str(tf.fx_rate_used) if tf.fx_rate_used else None,
                            fx_display=fx_display,
                            source_ref=tf.public_id,
                        )
                    )

        # 3. ORDERS
        if "order" in selected_types:
            stmt = select(InvestingOrder).where(InvestingOrder.workspace_id == workspace_id)
            if filter_internal_acc_id is not None:
                stmt = stmt.where(InvestingOrder.account_id == filter_internal_acc_id)
            if from_date is not None:
                stmt = stmt.where(InvestingOrder.occurred_at >= from_date)
            if to_date is not None:
                stmt = stmt.where(InvestingOrder.occurred_at <= to_date)

            res = await self.session.execute(stmt)
            orders = res.scalars().all()

            for ord in orders:
                acc = account_map.get(ord.account_id)
                if not acc:
                    continue

                if ord.order_type == "buy":
                    amt_str = f"-{ord.net_amount:.2f}"
                else:
                    amt_str = f"+{ord.net_amount:.2f}"

                desc = (
                    f"{ord.order_type.upper()} {ord.symbol} ({ord.quantity} @ {ord.price_per_unit})"
                )

                items.append(
                    ActivityFeedItem(
                        id=ord.public_id,
                        event_type="order",
                        date=_ensure_utc(ord.occurred_at),
                        description=desc,
                        amount=amt_str,
                        currency=acc.default_currency_code,
                        account_id=acc.public_id,
                        account_name=acc.name,
                        account_type=acc.account_type,
                        symbol=ord.symbol,
                        order_type=ord.order_type,
                        quantity=str(ord.quantity),
                        price_per_unit=str(ord.price_per_unit),
                        source_ref=ord.public_id,
                    )
                )

        # 4. DIVIDENDS
        if "dividend" in selected_types:
            stmt = select(Dividend).where(Dividend.workspace_id == workspace_id)
            if filter_internal_acc_id is not None:
                stmt = stmt.where(
                    (Dividend.credit_account_id == filter_internal_acc_id)
                    | (
                        Dividend.credit_account_id.is_(None)
                        & (Dividend.account_id == filter_internal_acc_id)
                    )
                )
            if from_date is not None:
                stmt = stmt.where(
                    sa.func.date(Dividend.pay_date) >= from_date.date()
                    if hasattr(from_date, "date")
                    else Dividend.pay_date >= from_date
                )
            if to_date is not None:
                stmt = stmt.where(
                    sa.func.date(Dividend.pay_date) <= to_date.date()
                    if hasattr(to_date, "date")
                    else Dividend.pay_date <= to_date
                )

            res = await self.session.execute(stmt)
            dividends = res.scalars().all()

            for div in dividends:
                credited_acc_id = div.credit_account_id or div.account_id
                target_acc = account_map.get(credited_acc_id)
                holding_acc = account_map.get(div.account_id)
                if not target_acc:
                    continue

                pay_dt = datetime.combine(div.pay_date, datetime.min.time(), tzinfo=UTC)
                desc = f"Dividend: {div.symbol}" if div.symbol else "Dividend income"
                if div.notes:
                    desc += f" - {div.notes}"

                counterpart_id = None
                counterpart_name = None
                if holding_acc and holding_acc.id != target_acc.id:
                    counterpart_id = holding_acc.public_id
                    counterpart_name = holding_acc.name

                items.append(
                    ActivityFeedItem(
                        id=div.public_id,
                        event_type="dividend",
                        date=pay_dt,
                        description=desc,
                        amount=f"+{div.net_amount:.2f}",
                        currency=div.currency,
                        account_id=target_acc.public_id,
                        account_name=target_acc.name,
                        account_type=target_acc.account_type,
                        counterpart_account_id=counterpart_id,
                        counterpart_account_name=counterpart_name,
                        symbol=div.symbol,
                        income_type=div.income_type,
                        source_ref=div.public_id,
                    )
                )

        # Sort descending by date, then id
        items.sort(key=lambda x: (x.date, str(x.id)), reverse=True)

        total = len(items)
        paged_items = items[offset : offset + limit]

        return ActivityFeedResponse(
            items=paged_items,
            total=total,
            limit=limit,
            offset=offset,
        )
