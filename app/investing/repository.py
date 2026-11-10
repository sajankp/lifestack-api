import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import DEFAULT_LIMIT
from app.core.repository import BaseRepository
from app.investing.models import (
    CashBalance,
    Company,
    CorporateAction,
    Dividend,
    Holding,
    HoldingCompliance,
    HoldingPrice,
    HoldingVerification,
    Instrument,
    InstrumentConstituent,
    InvestingOrder,
    LotConsumption,
    OrderLot,
    PortfolioReport,
    PortfolioSnapshot,
    ReferenceSecurity,
)

_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

# Advisory-lock namespace for order writes (first int4 of pg_advisory_xact_lock(int, int)).
_ORDER_WRITE_LOCK_NAMESPACE = 1_330_597_700


def normalize_company_name(name: str) -> str:
    """Collapse "Apple Inc" / "Apple Inc." to the same key (spec-083 §4.2)."""
    stripped = _PUNCTUATION_RE.sub("", name.strip().lower())
    return _WHITESPACE_RE.sub(" ", stripped).strip()


class HoldingRepository(BaseRepository[Holding]):
    async def get_all(
        self,
        workspace_id: int,
        limit: int | None = DEFAULT_LIMIT,
        offset: int = 0,
        *,
        quantity_state: str = "all",
        symbol: str | None = None,
        account_id: int | None = None,
        currency: str | None = None,
        instrument_type: str | None = None,
        sort_by: str = "created_at",
        sort_direction: str = "desc",
    ) -> tuple[Sequence[Holding], int]:
        base = select(Holding).where(Holding.workspace_id == workspace_id)
        if quantity_state == "nonzero":
            base = base.where(Holding.quantity != 0)
        elif quantity_state == "zero":
            base = base.where(Holding.quantity == 0)
        elif quantity_state != "all":
            raise ValueError("quantity_state must be one of: all, nonzero, zero")
        if symbol:
            base = base.where(Holding.symbol == symbol.upper().strip())
        if account_id is not None:
            base = base.where(Holding.account_id == account_id)
        if currency:
            base = base.where(Holding.currency == currency.upper().strip())
        if instrument_type:
            base = base.where(
                Holding.instrument_id.in_(
                    select(Instrument.id).where(Instrument.instrument_type == instrument_type)
                )
            )

        sort_columns = {
            "symbol": Holding.symbol,
            "quantity": Holding.quantity,
            "book_value": Holding.quantity * Holding.avg_cost,
            "created_at": Holding.created_at,
            "updated_at": Holding.updated_at,
        }
        if sort_by not in sort_columns:
            raise ValueError(
                "sort_by must be one of: symbol, quantity, book_value, created_at, updated_at"
            )
        if sort_direction not in {"asc", "desc"}:
            raise ValueError("sort_direction must be either asc or desc")

        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        sort_column = sort_columns[sort_by]
        order_by = sort_column.asc() if sort_direction == "asc" else sort_column.desc()
        result = await self.session.execute(
            base.order_by(order_by, Holding.public_id.asc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> Holding | None:
        result = await self.session.execute(
            select(Holding).where(
                Holding.workspace_id == workspace_id,
                Holding.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id_in_workspace(self, workspace_id: int, holding_id: int) -> Holding | None:
        result = await self.session.execute(
            select(Holding).where(
                Holding.workspace_id == workspace_id,
                Holding.id == holding_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_unique_key(
        self, workspace_id: int, symbol: str, account_id: int
    ) -> Holding | None:
        result = await self.session.execute(
            select(Holding).where(
                Holding.workspace_id == workspace_id,
                Holding.symbol == symbol,
                Holding.account_id == account_id,
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        holding: Holding,
    ) -> Holding:
        """Atomic upsert on (workspace_id, symbol, account_id).

        Plain check-then-insert let two concurrent order placements for the
        same symbol and account both find no existing holding and both
        INSERT, raising IntegrityError on uq_holding_workspace_symbol_account
        and returning 500 to the caller. The ON CONFLICT DO UPDATE makes the
        write idempotent under concurrency.
        """
        stmt = pg_insert(Holding).values(
            public_id=holding.public_id,
            workspace_id=holding.workspace_id,
            user_id=holding.user_id,
            instrument_id=holding.instrument_id,
            symbol=holding.symbol,
            account_id=holding.account_id,
            quantity=holding.quantity,
            avg_cost=holding.avg_cost,
            currency=holding.currency,
            source_type=holding.source_type,
            source_ref=holding.source_ref,
            source_import_id=holding.source_import_id,
            created_at=holding.created_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["workspace_id", "symbol", "account_id"],
            set_={
                "quantity": stmt.excluded.quantity,
                "avg_cost": stmt.excluded.avg_cost,
                "instrument_id": stmt.excluded.instrument_id,
                "updated_at": func.now(),
            },
        )
        await self.session.execute(stmt)
        # The ON CONFLICT UPDATE above is raw Core DML and bypasses the
        # unit-of-work, so a Holding already in this session's identity map
        # would otherwise be returned with its stale pre-conflict values.
        result = await self.session.execute(
            select(Holding)
            .where(
                Holding.workspace_id == holding.workspace_id,
                Holding.symbol == holding.symbol,
                Holding.account_id == holding.account_id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one()


class CashBalanceRepository(BaseRepository[CashBalance]):
    async def get_all(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        account_id: int | None = None,
    ) -> tuple[Sequence[CashBalance], int]:
        base = select(CashBalance).where(CashBalance.workspace_id == workspace_id)
        if account_id is not None:
            base = base.where(CashBalance.account_id == account_id)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(CashBalance.as_of.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> CashBalance | None:
        result = await self.session.execute(
            select(CashBalance).where(
                CashBalance.workspace_id == workspace_id,
                CashBalance.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_trigger_ref(self, workspace_id: int, trigger_ref: UUID) -> CashBalance | None:
        result = await self.session.execute(
            select(CashBalance).where(
                CashBalance.workspace_id == workspace_id,
                CashBalance.trigger_ref == trigger_ref,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_trigger_ref_and_account(
        self, workspace_id: int, trigger_ref: UUID, account_id: int
    ) -> CashBalance | None:
        """Account-scoped variant of get_by_trigger_ref.

        A single capital transfer can produce two snapshots sharing one
        trigger_ref (an investing-to-investing transfer writes both a
        from-side and a to-side snapshot), so callers that need one specific
        side must disambiguate by account_id.
        """
        result = await self.session.execute(
            select(CashBalance).where(
                CashBalance.workspace_id == workspace_id,
                CashBalance.trigger_ref == trigger_ref,
                CashBalance.account_id == account_id,
            )
        )
        return result.scalar_one_or_none()

    async def count_newer_than(
        self, workspace_id: int, account_id: int, currency: str, created_after: datetime
    ) -> int:
        result = await self.session.execute(
            select(func.count()).where(
                CashBalance.workspace_id == workspace_id,
                CashBalance.account_id == account_id,
                CashBalance.currency == currency,
                CashBalance.created_at > created_after,
            )
        )
        return result.scalar_one()

    async def get_latest_for_account_currency(
        self, workspace_id: int, account_id: int, currency: str
    ) -> CashBalance | None:
        result = await self.session.execute(
            select(CashBalance)
            .where(
                CashBalance.workspace_id == workspace_id,
                CashBalance.account_id == account_id,
                CashBalance.currency == currency,
            )
            .order_by(CashBalance.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_latest_per_account_currency(
        self, workspace_id: int, as_of: datetime | None = None
    ) -> Sequence[CashBalance]:
        """Return the single latest CashBalance row per (account_id, currency) pair."""
        order_by_clause = [CashBalance.created_at.desc(), CashBalance.as_of.desc()]
        subq_stmt = select(
            CashBalance.id,
            func
            .row_number()
            .over(
                partition_by=[CashBalance.account_id, CashBalance.currency],
                order_by=order_by_clause,
            )
            .label("rn"),
        ).where(CashBalance.workspace_id == workspace_id)

        if as_of is not None:
            subq_stmt = subq_stmt.where(CashBalance.as_of <= as_of)

        subq = subq_stmt.subquery()
        result = await self.session.execute(
            select(CashBalance)
            .join(subq, CashBalance.id == subq.c.id)
            .where(CashBalance.workspace_id == workspace_id, subq.c.rn == 1)
        )
        return result.scalars().all()


class InvestingOrderRepository(BaseRepository[InvestingOrder]):
    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> InvestingOrder | None:
        result = await self.session.execute(
            select(InvestingOrder).where(
                InvestingOrder.workspace_id == workspace_id,
                InvestingOrder.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_source_ref(self, workspace_id: int, source_ref: str) -> InvestingOrder | None:
        result = await self.session.execute(
            select(InvestingOrder)
            .where(
                InvestingOrder.workspace_id == workspace_id,
                InvestingOrder.source_ref == source_ref,
            )
            .order_by(InvestingOrder.id.asc())
            .limit(1)
        )
        return result.scalars().first()

    async def lock_idempotent_writes(self, workspace_id: int) -> None:
        """Serialise order creates per workspace (transaction-scoped advisory lock).

        ``source_ref`` has no unique index, so check-then-insert is otherwise
        racy. See ``TransactionRepository.lock_idempotent_writes``.
        """
        await self.session.execute(
            select(func.pg_advisory_xact_lock(_ORDER_WRITE_LOCK_NAMESPACE, workspace_id))
        )

    async def find_possible_duplicate_orders(
        self,
        workspace_id: int,
        *,
        account_id: int,
        symbol: str,
        order_type: str,
        quantity: Decimal,
        price_per_unit: Decimal,
        from_date: datetime,
        to_date: datetime,
    ) -> Sequence[InvestingOrder]:
        """Orders matching account + symbol + side + quantity + price in a window.

        ``to_date`` is exclusive; callers pass the workspace-local calendar day
        converted to UTC. Used only to surface a ``possible_duplicate`` warning,
        never to skip a write silently (fees and rounding differ too much).
        """
        result = await self.session.execute(
            select(InvestingOrder)
            .where(
                InvestingOrder.workspace_id == workspace_id,
                InvestingOrder.account_id == account_id,
                InvestingOrder.symbol == symbol.strip().upper(),
                InvestingOrder.order_type == order_type,
                InvestingOrder.quantity == quantity,
                InvestingOrder.price_per_unit == price_per_unit,
                InvestingOrder.occurred_at >= from_date,
                InvestingOrder.occurred_at < to_date,
            )
            .order_by(InvestingOrder.id.asc())
        )
        return result.scalars().all()

    async def list_by_workspace(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        symbol: str | None = None,
        account_id: int | None = None,
        order_type: str | None = None,
        search: str | None = None,
    ) -> tuple[Sequence[InvestingOrder], int]:
        base = select(InvestingOrder).where(InvestingOrder.workspace_id == workspace_id)
        if symbol is not None:
            base = base.where(InvestingOrder.symbol == symbol.upper())
        if search and search.strip():
            like = f"%{search.strip()}%"
            # Match the order's own symbol or the joined instrument's name so a
            # mutual fund with a numeric folio symbol is searchable by name.
            base = base.outerjoin(Instrument, InvestingOrder.instrument_id == Instrument.id).where(
                or_(InvestingOrder.symbol.ilike(like), Instrument.name.ilike(like))
            )
        if account_id is not None:
            base = base.where(InvestingOrder.account_id == account_id)
        if order_type is not None:
            base = base.where(InvestingOrder.order_type == order_type)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(InvestingOrder.occurred_at.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def list_by_holding(
        self, workspace_id: int, symbol: str, account_id: int
    ) -> Sequence[InvestingOrder]:
        result = await self.session.execute(
            select(InvestingOrder)
            .where(
                InvestingOrder.workspace_id == workspace_id,
                InvestingOrder.symbol == symbol.upper(),
                InvestingOrder.account_id == account_id,
            )
            .order_by(InvestingOrder.occurred_at.asc(), InvestingOrder.id.asc())
        )
        return result.scalars().all()

    async def bulk_create(self, orders: list[InvestingOrder]) -> list[InvestingOrder]:
        self.session.add_all(orders)
        await self.session.flush()
        for o in orders:
            await self.session.refresh(o)
        return orders

    async def rename_symbol(
        self, workspace_id: int, account_id: int, old_symbol: str, new_symbol: str
    ) -> int:
        result = await self.session.execute(
            update(InvestingOrder)
            .where(
                InvestingOrder.workspace_id == workspace_id,
                InvestingOrder.account_id == account_id,
                InvestingOrder.symbol == old_symbol.upper(),
            )
            .values(symbol=new_symbol.upper(), instrument_id=None, updated_at=datetime.now(UTC))
        )
        await self.session.flush()
        return result.rowcount or 0


class LotRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def delete_for_holding(self, holding_id: int) -> None:
        if holding_id is None:
            raise ValueError("holding_id must not be None")
        await self.session.execute(delete(OrderLot).where(OrderLot.holding_id == holding_id))
        await self.session.flush()

    async def list_open_for_holdings(
        self, workspace_id: int, holding_ids: Sequence[int]
    ) -> dict[int, list[OrderLot]]:
        """Open lots (``remaining_quantity > 0``) for many holdings in ONE query.

        Workspace-scoped on the lot row itself, oldest acquisition first
        (FIFO), so a caller never issues one query per holding.
        """
        ids = {i for i in holding_ids if i is not None}
        if not ids:
            return {}
        rows = (
            (
                await self.session.execute(
                    select(OrderLot)
                    .where(
                        OrderLot.workspace_id == workspace_id,
                        OrderLot.holding_id.in_(ids),
                        OrderLot.remaining_quantity > 0,
                    )
                    .order_by(OrderLot.holding_id, OrderLot.acquired_at, OrderLot.id)
                )
            )
            .scalars()
            .all()
        )
        by_holding: dict[int, list[OrderLot]] = {}
        for row in rows:
            by_holding.setdefault(row.holding_id, []).append(row)
        return by_holding

    async def create_lots(self, lots: list[OrderLot]) -> list[OrderLot]:
        # flush() already populates auto-generated PKs; no refresh() needed.
        self.session.add_all(lots)
        await self.session.flush()
        return lots

    async def create_consumptions(self, consumptions: list[LotConsumption]) -> list[LotConsumption]:
        self.session.add_all(consumptions)
        await self.session.flush()
        return consumptions


class CorporateActionRepository(BaseRepository[CorporateAction]):
    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> CorporateAction | None:
        result = await self.session.execute(
            select(CorporateAction).where(
                CorporateAction.workspace_id == workspace_id,
                CorporateAction.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_holding(
        self, workspace_id: int, symbol: str, account_id: int
    ) -> Sequence[CorporateAction]:
        result = await self.session.execute(
            select(CorporateAction)
            .where(
                CorporateAction.workspace_id == workspace_id,
                CorporateAction.symbol == symbol.upper(),
                CorporateAction.account_id == account_id,
            )
            .order_by(CorporateAction.ex_date.asc(), CorporateAction.id.asc())
        )
        return result.scalars().all()

    async def list_by_workspace(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        symbol: str | None = None,
        account_id: int | None = None,
    ) -> tuple[Sequence[CorporateAction], int]:
        base = select(CorporateAction).where(CorporateAction.workspace_id == workspace_id)
        if symbol is not None:
            base = base.where(CorporateAction.symbol == symbol.upper())
        if account_id is not None:
            base = base.where(CorporateAction.account_id == account_id)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(CorporateAction.ex_date.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total


class HoldingVerificationRepository(BaseRepository[HoldingVerification]):
    async def list_by_workspace(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        account_id: int | None = None,
    ) -> tuple[Sequence[HoldingVerification], int]:
        base = select(HoldingVerification).where(HoldingVerification.workspace_id == workspace_id)
        if account_id is not None:
            base = base.where(HoldingVerification.account_id == account_id)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(HoldingVerification.created_at.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def delete_for_import_batch(self, workspace_id: int, source_import_id: int) -> int:
        result = await self.session.execute(
            delete(HoldingVerification).where(
                HoldingVerification.workspace_id == workspace_id,
                HoldingVerification.source_import_id == source_import_id,
            )
        )
        return result.rowcount or 0


class InstrumentRepository(BaseRepository[Instrument]):
    async def list_workspace(self, workspace_id: int) -> Sequence[Instrument]:
        result = await self.session.execute(
            select(Instrument)
            .where((Instrument.workspace_id == workspace_id) | (Instrument.workspace_id.is_(None)))
            .order_by(Instrument.workspace_id.desc().nulls_last(), Instrument.created_at.desc())
        )
        rows = result.scalars().all()
        seen = set()
        deduped = []
        for r in rows:
            if r.symbol not in seen:
                seen.add(r.symbol)
                deduped.append(r)
        return deduped

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> Instrument | None:
        result = await self.session.execute(
            select(Instrument).where(
                (Instrument.workspace_id == workspace_id) | (Instrument.workspace_id.is_(None)),
                Instrument.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_symbol(self, workspace_id: int | None, symbol: str) -> Instrument | None:
        normalized_symbol = symbol.upper()
        if workspace_id is not None:
            # Query workspace first (override)
            result = await self.session.execute(
                select(Instrument).where(
                    Instrument.workspace_id == workspace_id,
                    Instrument.symbol == normalized_symbol,
                )
            )
            val = result.scalar_one_or_none()
            if val is not None:
                return val

        # Query global
        res_global = await self.session.execute(
            select(Instrument).where(
                Instrument.workspace_id.is_(None), Instrument.symbol == normalized_symbol
            )
        )
        return res_global.scalar_one_or_none()

    async def get_by_symbols(
        self, workspace_id: int, symbols: Sequence[str]
    ) -> dict[str, Instrument]:
        normalized = {s for s in symbols if s}
        if not normalized:
            return {}
        result = await self.session.execute(
            select(Instrument)
            .where(
                (Instrument.workspace_id == workspace_id) | (Instrument.workspace_id.is_(None)),
                Instrument.symbol.in_(normalized),
            )
            .order_by(Instrument.workspace_id.desc().nulls_last())
        )
        rows = result.scalars().all()
        res = {}
        for row in rows:
            if row.symbol not in res:
                res[row.symbol] = row
        return res

    async def get_by_ids(self, ids: Sequence[int]) -> dict[int, Instrument]:
        unique_ids = {i for i in ids if i is not None}
        if not unique_ids:
            return {}
        result = await self.session.execute(select(Instrument).where(Instrument.id.in_(unique_ids)))
        rows = result.scalars().all()
        return {row.id: row for row in rows if row.id is not None}

    async def get_instruments_by_symbol_exchange(
        self, workspace_id: int | None, symbol: str, exchange: str
    ) -> Sequence[Instrument]:
        """Instruments matching symbol+exchange visible to the workspace."""
        result = await self.session.execute(
            select(Instrument).where(
                Instrument.symbol == symbol.strip().upper(),
                func.upper(Instrument.exchange) == exchange.strip().upper(),
                (Instrument.workspace_id == workspace_id) | (Instrument.workspace_id.is_(None)),
            )
        )
        return result.scalars().all()


class CompanyRepository(BaseRepository[Company]):
    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> Company | None:
        result = await self.session.execute(
            select(Company).where(
                (Company.workspace_id == workspace_id) | (Company.workspace_id.is_(None)),
                Company.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, company_id: int) -> Company | None:
        result = await self.session.execute(select(Company).where(Company.id == company_id))
        return result.scalar_one_or_none()

    async def get_by_ids(self, ids: Sequence[int]) -> dict[int, Company]:
        unique_ids = {i for i in ids if i is not None}
        if not unique_ids:
            return {}
        result = await self.session.execute(select(Company).where(Company.id.in_(unique_ids)))
        rows = result.scalars().all()
        return {row.id: row for row in rows if row.id is not None}

    async def get_by_isin(self, workspace_id: int | None, isin: str) -> Company | None:
        """Company by ISIN, visible to the workspace (workspace-scoped or global).

        A workspace-scoped copy shadows the global row (same precedent as
        instruments), so after a global company has been forked both rows
        carry the ISIN and the workspace copy wins deterministically.
        """
        result = await self.session.execute(
            select(Company)
            .where(
                Company.isin == isin.strip().upper(),
                (Company.workspace_id == workspace_id) | (Company.workspace_id.is_(None)),
            )
            .order_by(Company.workspace_id.asc().nulls_last(), Company.id.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _scope_candidates(self, workspace_id: int | None) -> Sequence[Company]:
        result = await self.session.execute(
            select(Company)
            .where((Company.workspace_id == workspace_id) | (Company.workspace_id.is_(None)))
            # Workspace-scoped rows first so a forked copy always shadows the
            # global company it was copied from.
            .order_by(Company.workspace_id.asc().nulls_last(), Company.id.asc())
        )
        return result.scalars().all()

    async def fork_for_workspace(self, workspace_id: int, company: Company) -> Company:
        """Return a company this workspace may edit, forking a global one.

        Global companies (``workspace_id IS NULL``) are shared reference rows
        and are never mutated in place. The fork is idempotent: an existing
        workspace copy (same ISIN, else same name) is reused, so re-running a
        write never creates a second copy. Everything this workspace owns that
        pointed at the global row is re-pointed to the copy so reads stay
        consistent: workspace instruments, workspace holdings (via a
        workspace shadow of each global instrument) and the workspace's
        constituent rows.
        """
        if company.workspace_id == workspace_id:
            return company
        if company.workspace_id is not None:
            raise ValueError("company belongs to another workspace")
        assert company.id is not None

        copy: Company | None = None
        if company.isin:
            copy = (
                (
                    await self.session.execute(
                        select(Company).where(
                            Company.workspace_id == workspace_id, Company.isin == company.isin
                        )
                    )
                )
                .scalars()
                .first()
            )
        if copy is None:
            by_name = (
                (
                    await self.session.execute(
                        select(Company).where(
                            Company.workspace_id == workspace_id, Company.name == company.name
                        )
                    )
                )
                .scalars()
                .first()
            )
            if by_name is not None:
                if by_name.isin and company.isin and by_name.isin != company.isin:
                    raise ValueError(
                        f"workspace already has a different company named '{company.name}'"
                    )
                copy = by_name
        if copy is None:
            copy = await self.create(
                Company(
                    workspace_id=workspace_id,
                    name=company.name,
                    ticker=company.ticker,
                    isin=company.isin,
                    sector=company.sector,
                    industry=company.industry,
                    country_code=company.country_code,
                )
            )
        assert copy.id is not None
        await self._repoint_workspace_to_copy(workspace_id, company.id, copy.id)
        return copy

    async def _repoint_workspace_to_copy(
        self, workspace_id: int, global_company_id: int, copy_id: int
    ) -> None:
        now = datetime.now(UTC)
        # Workspace instruments linked to the global company.
        await self.session.execute(
            update(Instrument)
            .where(
                Instrument.workspace_id == workspace_id,
                Instrument.company_id == global_company_id,
            )
            .values(company_id=copy_id, updated_at=now)
        )
        # Global instruments linked to it get a workspace shadow (reused when
        # one already exists for the symbol) and this workspace's holdings
        # move onto the shadow.
        global_instruments = (
            (
                await self.session.execute(
                    select(Instrument).where(
                        Instrument.workspace_id.is_(None),
                        Instrument.company_id == global_company_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for global_instrument in global_instruments:
            shadow = (
                await self.session.execute(
                    select(Instrument).where(
                        Instrument.workspace_id == workspace_id,
                        Instrument.symbol == global_instrument.symbol,
                    )
                )
            ).scalar_one_or_none()
            if shadow is None:
                shadow = Instrument(
                    workspace_id=workspace_id,
                    symbol=global_instrument.symbol,
                    name=global_instrument.name,
                    instrument_type=global_instrument.instrument_type,
                    isin=global_instrument.isin,
                    exchange=global_instrument.exchange,
                    exchange_country_override=global_instrument.exchange_country_override,
                    provider_key=global_instrument.provider_key,
                    company_id=copy_id,
                    is_active=global_instrument.is_active,
                )
                self.session.add(shadow)
                await self.session.flush()
            await self.session.execute(
                update(Holding)
                .where(
                    Holding.workspace_id == workspace_id,
                    Holding.instrument_id == global_instrument.id,
                )
                .values(instrument_id=shadow.id, updated_at=now)
            )
        # Constituent rows of this workspace's own instruments.
        await self.session.execute(
            update(InstrumentConstituent)
            .where(
                InstrumentConstituent.constituent_company_id == global_company_id,
                InstrumentConstituent.instrument_id.in_(
                    select(Instrument.id).where(Instrument.workspace_id == workspace_id)
                ),
            )
            .values(constituent_company_id=copy_id)
        )

    async def resolve_or_create_company(
        self,
        workspace_id: int | None,
        *,
        name: str,
        ticker: str | None = None,
        isin: str | None = None,
        country_code: str | None = None,
    ) -> Company:
        """Resolve `name`/`ticker`/`isin` to a single stable `Company` row.

        Precedence: ISIN -> ticker -> normalized(name) (spec-083 §4.2). Both
        ingestion paths (CSV import, constituent API upsert) must call this
        instead of matching on raw name, or "Apple Inc"/"Apple Inc."/"AAPL"
        fragment into separate companies and understate look-through overlap.

        An explicitly supplied ISIN always wins: if it matches a company, that
        company is returned even when the ticker points elsewhere. If the ISIN
        matches nothing while the ticker matches a *different* company, the
        row is rejected instead of silently stamping the ISIN onto the
        ticker-matched company (that re-pointed IDEX Corp at INE022Q01020 and
        let a constituent write overwrite its sector).
        """
        isin = isin.strip().upper() if isin else None
        ticker = ticker.strip().upper() if ticker else None
        candidates = await self._scope_candidates(workspace_id)

        isin_company: Company | None = None
        if isin:
            for candidate in candidates:
                if candidate.isin and candidate.isin.strip().upper() == isin:
                    isin_company = candidate
                    break

        ticker_company: Company | None = None
        if ticker:
            for candidate in candidates:
                if not (candidate.ticker and candidate.ticker.strip().upper() == ticker):
                    continue
                # Same ticker string can denote different companies on different
                # markets (e.g. a symbol reused across exchanges); when both sides
                # know the market, require it to match so we don't false-merge.
                if (
                    country_code
                    and candidate.country_code
                    and candidate.country_code.upper() != country_code.upper()
                ):
                    continue
                ticker_company = candidate
                break

        if isin and isin_company is not None:
            # ISIN wins. A ticker that points at a different company is a
            # genuine identity conflict: reject rather than merge.
            if ticker_company is not None and ticker_company is not isin_company:
                raise ValueError(
                    f"company '{name}': ISIN '{isin}' resolves to "
                    f"'{isin_company.name}' but ticker '{ticker}' resolves to "
                    f"'{ticker_company.name}' — they denote different "
                    f"companies. Correct one record first, then re-send the row."
                )
            return isin_company

        if isin and ticker_company is not None and isin_company is None:
            # Explicit ISIN matches nothing: refuse to backfill it onto the
            # company the ticker happens to point at.
            raise ValueError(
                f"company '{name}': no company with ISIN '{isin}' is visible in "
                f"this workspace, but ticker '{ticker}' resolves to "
                f"'{ticker_company.name}'. ISIN and ticker disagree — correct "
                f"one record first (or omit the ISIN to match by ticker alone)."
            )

        if ticker and ticker_company is not None:
            return ticker_company

        normalized_target = normalize_company_name(name)
        for candidate in candidates:
            if normalize_company_name(candidate.name) == normalized_target:
                return candidate

        return await self.create(
            Company(
                workspace_id=workspace_id,
                name=name,
                ticker=ticker,
                isin=isin,
                country_code=country_code,
            )
        )


class ReferenceSecurityRepository(BaseRepository[ReferenceSecurity]):
    async def get_by_isin(self, isin: str) -> ReferenceSecurity | None:
        result = await self.session.execute(
            select(ReferenceSecurity).where(ReferenceSecurity.isin == isin.strip().upper())
        )
        return result.scalar_one_or_none()

    async def get_by_ticker_exchange(
        self, ticker: str, exchange: str | None
    ) -> ReferenceSecurity | None:
        stmt = select(ReferenceSecurity).where(ReferenceSecurity.ticker == ticker.strip().upper())
        stmt = stmt.where(ReferenceSecurity.exchange == exchange) if exchange else stmt
        result = await self.session.execute(stmt)
        rows = result.scalars().all()
        return rows[0] if rows else None

    async def list_by_ticker(self, ticker: str) -> Sequence[ReferenceSecurity]:
        result = await self.session.execute(
            select(ReferenceSecurity).where(ReferenceSecurity.ticker == ticker.strip().upper())
        )
        return result.scalars().all()

    async def list_by_tickers(self, tickers: Sequence[str]) -> dict[str, list[ReferenceSecurity]]:
        """Reference rows for many tickers in one query, keyed by upper-case ticker."""
        normalized = {t.strip().upper() for t in tickers if t and t.strip()}
        if not normalized:
            return {}
        result = await self.session.execute(
            select(ReferenceSecurity).where(ReferenceSecurity.ticker.in_(normalized))
        )
        grouped: dict[str, list[ReferenceSecurity]] = {}
        for row in result.scalars().all():
            grouped.setdefault(row.ticker.upper(), []).append(row)
        return grouped

    async def get_by_amfi_code(self, amfi_code: str) -> ReferenceSecurity | None:
        result = await self.session.execute(
            select(ReferenceSecurity).where(ReferenceSecurity.amfi_code == amfi_code.strip())
        )
        return result.scalar_one_or_none()

    async def find_by_normalized_name(self, name: str) -> ReferenceSecurity | None:
        normalized_target = normalize_company_name(name)
        result = await self.session.execute(select(ReferenceSecurity))
        for row in result.scalars().all():
            if normalize_company_name(row.name) == normalized_target:
                return row
            if any(normalize_company_name(alias) == normalized_target for alias in row.aliases):
                return row
        return None

    async def upsert(self, entity: ReferenceSecurity) -> ReferenceSecurity:
        existing: ReferenceSecurity | None = None
        if entity.isin:
            existing = await self.get_by_isin(entity.isin)
        if existing is None and entity.ticker:
            existing = await self.get_by_ticker_exchange(entity.ticker, entity.exchange)
        if existing is None and entity.amfi_code:
            existing = await self.get_by_amfi_code(entity.amfi_code)

        if existing is None:
            return await self.create(entity)

        existing.isin = entity.isin or existing.isin
        existing.ticker = entity.ticker or existing.ticker
        existing.exchange = entity.exchange or existing.exchange
        existing.amfi_code = entity.amfi_code or existing.amfi_code
        existing.security_type = entity.security_type
        existing.name = entity.name
        existing.aliases = entity.aliases
        existing.country_code = entity.country_code or existing.country_code
        existing.source = entity.source
        existing.fetched_at = entity.fetched_at
        return await self.save(existing)


class InstrumentConstituentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def delete_snapshot(self, instrument_id: int, as_of_date: date, source: str) -> int:
        result = await self.session.execute(
            select(InstrumentConstituent).where(
                InstrumentConstituent.instrument_id == instrument_id,
                InstrumentConstituent.as_of_date == as_of_date,
                InstrumentConstituent.source == source,
            )
        )
        rows = result.scalars().all()
        for row in rows:
            await self.session.delete(row)
        await self.session.flush()
        return len(rows)

    async def create_many(self, rows: list[InstrumentConstituent]) -> list[InstrumentConstituent]:
        self.session.add_all(rows)
        await self.session.flush()
        return rows

    async def list_snapshot(
        self, instrument_id: int, as_of_date: date, source: str | None = None
    ) -> Sequence[InstrumentConstituent]:
        conditions = [
            InstrumentConstituent.instrument_id == instrument_id,
            InstrumentConstituent.as_of_date == as_of_date,
        ]
        if source is not None:
            conditions.append(InstrumentConstituent.source == source)
        result = await self.session.execute(select(InstrumentConstituent).where(*conditions))
        return result.scalars().all()

    async def get_latest_on_or_before(
        self, instrument_id: int, as_of_date: date, source: str | None = None
    ) -> Sequence[InstrumentConstituent]:
        """Return the rows of exactly one snapshot per instrument.

        The snapshot is the newest by ``(as_of_date, fetched_at)`` among rows
        with ``as_of_date <= as_of_date`` (optionally pinned to ``source``).
        Every row of that snapshot is returned; rows from any other snapshot
        (older date, or same date but a different source with an older
        ``fetched_at``) are not, so look-through reads never double count.
        """
        conditions = [
            InstrumentConstituent.instrument_id == instrument_id,
            InstrumentConstituent.as_of_date <= as_of_date,
        ]
        if source is not None:
            conditions.append(InstrumentConstituent.source == source)
        result = await self.session.execute(
            select(InstrumentConstituent)
            .where(*conditions)
            .order_by(
                InstrumentConstituent.as_of_date.desc(),
                InstrumentConstituent.fetched_at.desc(),
                InstrumentConstituent.source.asc(),
            )
        )
        rows = result.scalars().all()
        if not rows:
            return []
        chosen = rows[0]
        return [
            row
            for row in rows
            if row.as_of_date == chosen.as_of_date and row.source == chosen.source
        ]

    async def get_latest_on_or_before_many(
        self, instrument_ids: Sequence[int], as_of_date: date
    ) -> dict[int, list[InstrumentConstituent]]:
        unique_ids = {i for i in instrument_ids if i is not None}
        if not unique_ids:
            return {}

        result = await self.session.execute(
            select(InstrumentConstituent)
            .where(InstrumentConstituent.instrument_id.in_(unique_ids))
            .where(InstrumentConstituent.as_of_date <= as_of_date)
            .order_by(
                InstrumentConstituent.instrument_id.asc(),
                InstrumentConstituent.as_of_date.desc(),
                InstrumentConstituent.fetched_at.desc(),
                InstrumentConstituent.source.asc(),
            )
        )
        rows = result.scalars().all()
        grouped: dict[int, list[InstrumentConstituent]] = {}
        chosen_key: dict[int, tuple[date, str]] = {}
        for row in rows:
            iid = row.instrument_id
            if iid is None:
                continue
            if iid not in chosen_key:
                # First row for this instrument is the newest snapshot by
                # (as_of_date, fetched_at); only that snapshot's rows count.
                chosen_key[iid] = (row.as_of_date, row.source)
                grouped[iid] = [row]
                continue
            if (row.as_of_date, row.source) == chosen_key[iid]:
                grouped[iid].append(row)
        return grouped


class HoldingPriceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert_price(
        self,
        workspace_id: int,
        holding_id: int,
        price_date: date,
        unit_price,
        source: str = "manual",
    ) -> HoldingPrice:
        existing = (
            await self.session.execute(
                select(HoldingPrice).where(
                    HoldingPrice.workspace_id == workspace_id,
                    HoldingPrice.holding_id == holding_id,
                    HoldingPrice.price_date == price_date,
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.unit_price = unit_price
            existing.source = source
            self.session.add(existing)
            await self.session.flush()
            return existing
        row = HoldingPrice(
            workspace_id=workspace_id,
            holding_id=holding_id,
            price_date=price_date,
            unit_price=unit_price,
            source=source,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def bulk_upsert_prices(
        self,
        workspace_id: int,
        price_date: date,
        prices: list[tuple[int, object]],
        source: str = "manual",
    ) -> None:
        if not prices:
            return
        holding_ids = [holding_id for holding_id, _ in prices]
        existing = (
            (
                await self.session.execute(
                    select(HoldingPrice).where(
                        HoldingPrice.workspace_id == workspace_id,
                        HoldingPrice.holding_id.in_(holding_ids),
                        HoldingPrice.price_date == price_date,
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_map = {row.holding_id: row for row in existing}
        for holding_id, unit_price in prices:
            row = existing_map.get(holding_id)
            if row is not None:
                row.unit_price = unit_price
                row.source = source
                self.session.add(row)
                continue
            self.session.add(
                HoldingPrice(
                    workspace_id=workspace_id,
                    holding_id=holding_id,
                    price_date=price_date,
                    unit_price=unit_price,
                    source=source,
                )
            )
        await self.session.flush()

    async def list_prices(
        self, workspace_id: int, holding_id: int, from_date: date, to_date: date
    ) -> list[HoldingPrice]:
        return list(
            (
                await self.session.execute(
                    select(HoldingPrice)
                    .where(
                        HoldingPrice.workspace_id == workspace_id,
                        HoldingPrice.holding_id == holding_id,
                        HoldingPrice.price_date >= from_date,
                        HoldingPrice.price_date <= to_date,
                    )
                    .order_by(HoldingPrice.price_date.asc())
                )
            )
            .scalars()
            .all()
        )

    async def delete_for_holding(self, workspace_id: int, holding_id: int) -> None:
        if workspace_id is None or holding_id is None:
            raise ValueError("workspace_id and holding_id must not be None")
        await self.session.execute(
            delete(HoldingPrice).where(
                HoldingPrice.workspace_id == workspace_id,
                HoldingPrice.holding_id == holding_id,
            )
        )
        await self.session.flush()

    async def latest_price_on_or_before(
        self, workspace_id: int, holding_id: int, as_of: date
    ) -> HoldingPrice | None:
        return (
            await self.session.execute(
                select(HoldingPrice)
                .where(
                    HoldingPrice.workspace_id == workspace_id,
                    HoldingPrice.holding_id == holding_id,
                    HoldingPrice.price_date <= as_of,
                )
                .order_by(HoldingPrice.price_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def latest_prices_on_or_before_bulk(
        self, workspace_id: int, holding_ids: list[int], as_of: date
    ) -> dict[int, HoldingPrice]:
        if not holding_ids:
            return {}
        latest_dates_subquery = (
            select(
                HoldingPrice.holding_id.label("holding_id"),
                func.max(HoldingPrice.price_date).label("max_price_date"),
            )
            .where(
                HoldingPrice.workspace_id == workspace_id,
                HoldingPrice.holding_id.in_(holding_ids),
                HoldingPrice.price_date <= as_of,
            )
            .group_by(HoldingPrice.holding_id)
            .subquery()
        )
        rows = (
            (
                await self.session.execute(
                    select(HoldingPrice)
                    .join(
                        latest_dates_subquery,
                        (HoldingPrice.holding_id == latest_dates_subquery.c.holding_id)
                        & (HoldingPrice.price_date == latest_dates_subquery.c.max_price_date),
                    )
                    .where(HoldingPrice.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        return {row.holding_id: row for row in rows}


class PortfolioSnapshotRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(self, snapshot: PortfolioSnapshot) -> PortfolioSnapshot:
        """Atomic upsert on (workspace_id, snapshot_date) via ON CONFLICT DO
        UPDATE. Plain check-then-insert let two unlocked callers (e.g. a
        dashboard request and morning_briefing_job) both find no row for
        today and both INSERT, raising IntegrityError on
        uq_snapshot_workspace_date (prod incident 2026-07-18/19)."""
        stmt = pg_insert(PortfolioSnapshot).values(
            workspace_id=snapshot.workspace_id,
            snapshot_date=snapshot.snapshot_date,
            total_value=snapshot.total_value,
            total_cost=snapshot.total_cost,
            holdings_value=snapshot.holdings_value,
            cash_value=snapshot.cash_value,
            currency_code=snapshot.currency_code,
            fx_rates_used=snapshot.fx_rates_used,
            created_at=snapshot.created_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["workspace_id", "snapshot_date"],
            set_={
                "total_value": stmt.excluded.total_value,
                "total_cost": stmt.excluded.total_cost,
                "holdings_value": stmt.excluded.holdings_value,
                "cash_value": stmt.excluded.cash_value,
                "currency_code": stmt.excluded.currency_code,
                "fx_rates_used": stmt.excluded.fx_rates_used,
            },
        )
        await self.session.execute(stmt)
        # populate_existing=True: the ON CONFLICT UPDATE above is raw Core DML
        # and bypasses the unit-of-work, so a PortfolioSnapshot already in this
        # session's identity map (e.g. loaded earlier via `latest()`) would
        # otherwise be returned with its stale pre-conflict attribute values.
        return (
            await self.session.execute(
                select(PortfolioSnapshot)
                .where(
                    PortfolioSnapshot.workspace_id == snapshot.workspace_id,
                    PortfolioSnapshot.snapshot_date == snapshot.snapshot_date,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    async def delete_for_date(self, workspace_id: int, snapshot_date: date) -> None:
        await self.session.execute(
            delete(PortfolioSnapshot).where(
                PortfolioSnapshot.workspace_id == workspace_id,
                PortfolioSnapshot.snapshot_date == snapshot_date,
            )
        )
        await self.session.flush()

    async def latest(self, workspace_id: int) -> PortfolioSnapshot | None:
        return (
            await self.session.execute(
                select(PortfolioSnapshot)
                .where(PortfolioSnapshot.workspace_id == workspace_id)
                .order_by(PortfolioSnapshot.snapshot_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def latest_before(
        self, workspace_id: int, snapshot_date: date
    ) -> PortfolioSnapshot | None:
        return (
            await self.session.execute(
                select(PortfolioSnapshot)
                .where(
                    PortfolioSnapshot.workspace_id == workspace_id,
                    PortfolioSnapshot.snapshot_date < snapshot_date,
                )
                .order_by(PortfolioSnapshot.snapshot_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def list_range(
        self,
        workspace_id: int,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[PortfolioSnapshot]:
        query = select(PortfolioSnapshot).where(PortfolioSnapshot.workspace_id == workspace_id)
        if from_date is not None:
            query = query.where(PortfolioSnapshot.snapshot_date >= from_date)
        if to_date is not None:
            query = query.where(PortfolioSnapshot.snapshot_date <= to_date)
        query = query.order_by(PortfolioSnapshot.snapshot_date.asc())
        return list((await self.session.execute(query)).scalars().all())


class DividendRepository(BaseRepository[Dividend]):
    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> Dividend | None:
        result = await self.session.execute(
            select(Dividend).where(
                Dividend.workspace_id == workspace_id,
                Dividend.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_external_ref(
        self, workspace_id: int, account_id: int, external_ref: str
    ) -> Dividend | None:
        result = await self.session.execute(
            select(Dividend).where(
                Dividend.workspace_id == workspace_id,
                Dividend.account_id == account_id,
                Dividend.external_ref == external_ref,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_fallback_key(
        self, workspace_id: int, account_id: int, symbol: str | None, pay_date: date
    ) -> Dividend | None:
        """Fallback import identity for rows without external_ref (spec-073
        INV-5): (workspace, account, symbol, pay_date). symbol is normalized
        upper() by the caller; None matches account-level income rows."""
        result = await self.session.execute(
            select(Dividend).where(
                Dividend.workspace_id == workspace_id,
                Dividend.account_id == account_id,
                Dividend.symbol == symbol,
                Dividend.pay_date == pay_date,
                Dividend.external_ref.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_by_workspace(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        account_id: int | None = None,
        symbol: str | None = None,
    ) -> tuple[Sequence[Dividend], int]:
        base = select(Dividend).where(Dividend.workspace_id == workspace_id)
        if account_id is not None:
            base = base.where(
                or_(
                    Dividend.account_id == account_id,
                    Dividend.credit_account_id == account_id,
                )
            )
        if symbol is not None:
            base = base.where(Dividend.symbol == symbol.upper())
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(Dividend.pay_date.desc(), Dividend.id.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def sum_gross_by_holding(
        self,
        workspace_id: int,
        holding_id: int,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[tuple[str, int, Decimal]]:
        """Return ``(currency, event_count, gross_total)`` per currency for one holding.

        Aggregated in SQL (one query, no per-dividend loading).
        """
        query = (
            select(Dividend.currency, func.count(Dividend.id), func.sum(Dividend.gross_amount))
            .where(Dividend.workspace_id == workspace_id, Dividend.holding_id == holding_id)
            .group_by(Dividend.currency)
            .order_by(Dividend.currency)
        )
        if from_date is not None:
            query = query.where(Dividend.pay_date >= from_date)
        if to_date is not None:
            query = query.where(Dividend.pay_date <= to_date)
        rows = (await self.session.execute(query)).all()
        return [(row[0], int(row[1]), Decimal(row[2])) for row in rows]


class HoldingComplianceRepository(BaseRepository[HoldingCompliance]):
    async def get_by_holding(self, workspace_id: int, holding_id: int) -> HoldingCompliance | None:
        result = await self.session.execute(
            select(HoldingCompliance).where(
                HoldingCompliance.workspace_id == workspace_id,
                HoldingCompliance.holding_id == holding_id,
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        workspace_id: int,
        holding_id: int,
        *,
        verdict: str,
        sources: list[dict[str, Any]],
        as_of_date: date,
        impure_income_pct: Decimal | None,
    ) -> HoldingCompliance:
        """Atomically insert or replace the single compliance row for a holding.

        Uses ``INSERT .. ON CONFLICT DO UPDATE`` on the
        ``(workspace_id, holding_id)`` unique key so two concurrent first
        writes cannot raise an IntegrityError or create duplicates.
        """
        now = datetime.now(UTC)
        values = {
            "verdict": verdict,
            "sources": sources,
            "as_of_date": as_of_date,
            "impure_income_pct": impure_income_pct,
        }
        stmt = (
            pg_insert(HoldingCompliance)
            .values(
                public_id=uuid4(),
                workspace_id=workspace_id,
                holding_id=holding_id,
                created_at=now,
                updated_at=now,
                **values,
            )
            .on_conflict_do_update(
                constraint="uq_investing_holding_compliance_workspace_holding",
                set_={**values, "updated_at": now},
            )
            .returning(HoldingCompliance)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one()


class PortfolioReportRepository:
    """Append-only report history (spec-107).

    ``create`` upserts with ON CONFLICT DO NOTHING on
    ``(workspace_id, as_of, run_id)``: a biweekly re-run never overwrites an
    existing row, and returns the existing record when present.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_if_absent(
        self,
        *,
        workspace_id: int,
        as_of: date,
        run_id: str,
        scalars_json: dict | None = None,
        foundation_json: dict | None = None,
        actions_md: str | None = None,
        theme_weights: dict | None = None,
        harvest_candidates: list | None = None,
        lrs_headroom_inr: Decimal | None = None,
        tax_buckets: dict | None = None,
    ) -> tuple[PortfolioReport, bool]:
        """Insert one report row, or return the existing one for the run.

        Returns ``(row, created)``; ``created`` is False when the run already existed.

        Idempotent on ``(workspace_id, as_of, run_id)`` — append-only
        semantics: conflicting inserts are silently dropped, so a biweekly
        re-run never overwrites an existing row.
        """
        stmt = (
            pg_insert(PortfolioReport)
            .values(
                workspace_id=workspace_id,
                as_of=as_of,
                run_id=run_id,
                scalars_json=scalars_json if scalars_json is not None else {},
                foundation_json=foundation_json if foundation_json is not None else {},
                actions_md=actions_md,
                theme_weights=theme_weights if theme_weights is not None else {},
                harvest_candidates=harvest_candidates if harvest_candidates is not None else [],
                lrs_headroom_inr=lrs_headroom_inr,
                tax_buckets=tax_buckets if tax_buckets is not None else {},
            )
            .on_conflict_do_nothing(index_elements=["workspace_id", "as_of", "run_id"])
            .returning(PortfolioReport.id)
        )
        created = (await self.session.execute(stmt)).scalar_one_or_none() is not None
        # populate_existing: ON CONFLICT DO NOTHING is raw Core DML that
        # bypasses the unit-of-work, so re-select to return the row that
        # actually landed (new or pre-existing), complete with server-side
        # defaults (id, public_id, created_at).
        result = await self.session.execute(
            select(PortfolioReport)
            .where(
                PortfolioReport.workspace_id == workspace_id,
                PortfolioReport.as_of == as_of,
                PortfolioReport.run_id == run_id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one(), created

    async def create(self, **fields: Any) -> PortfolioReport:
        """Insert one report row, or return the existing one (see ``create_if_absent``)."""
        row, _ = await self.create_if_absent(**fields)
        return row

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> PortfolioReport | None:
        result = await self.session.execute(
            select(PortfolioReport).where(
                PortfolioReport.workspace_id == workspace_id,
                PortfolioReport.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_workspace_range(
        self,
        workspace_id: int,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[Sequence[PortfolioReport], int]:
        base = select(PortfolioReport).where(PortfolioReport.workspace_id == workspace_id)
        if from_date is not None:
            base = base.where(PortfolioReport.as_of >= from_date)
        if to_date is not None:
            base = base.where(PortfolioReport.as_of <= to_date)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base
            .order_by(PortfolioReport.as_of.desc(), PortfolioReport.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all(), total
