import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from uuid import UUID

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
    HoldingPrice,
    HoldingVerification,
    Instrument,
    InstrumentConstituent,
    InvestingOrder,
    LotConsumption,
    OrderLot,
    PortfolioSnapshot,
    ReferenceSecurity,
)

_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_company_name(name: str) -> str:
    """Collapse "Apple Inc" / "Apple Inc." to the same key (spec-083 §4.2)."""
    stripped = _PUNCTUATION_RE.sub("", name.strip().lower())
    return _WHITESPACE_RE.sub(" ", stripped).strip()


class HoldingRepository(BaseRepository[Holding]):
    async def get_all(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[Holding], int]:
        base = select(Holding).where(Holding.workspace_id == workspace_id)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(Holding.created_at.desc()).limit(limit).offset(offset)
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
        subq_stmt = select(
            CashBalance.id,
            func
            .row_number()
            .over(
                partition_by=[CashBalance.account_id, CashBalance.currency],
                order_by=[CashBalance.as_of.desc(), CashBalance.created_at.desc()],
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

    async def _scope_candidates(self, workspace_id: int | None) -> Sequence[Company]:
        result = await self.session.execute(
            select(Company).where(
                (Company.workspace_id == workspace_id) | (Company.workspace_id.is_(None))
            )
        )
        return result.scalars().all()

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
        """
        isin = isin.strip().upper() if isin else None
        ticker = ticker.strip().upper() if ticker else None
        candidates = await self._scope_candidates(workspace_id)

        if isin:
            for candidate in candidates:
                if candidate.isin and candidate.isin.strip().upper() == isin:
                    return candidate

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
                if isin and not candidate.isin:
                    candidate.isin = isin
                    await self.save(candidate)
                return candidate

        normalized_target = normalize_company_name(name)
        for candidate in candidates:
            if normalize_company_name(candidate.name) == normalized_target:
                changed = False
                if isin and not candidate.isin:
                    candidate.isin = isin
                    changed = True
                if ticker and not candidate.ticker:
                    candidate.ticker = ticker
                    changed = True
                if changed:
                    await self.save(candidate)
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
        conditions = [
            InstrumentConstituent.instrument_id == instrument_id,
            InstrumentConstituent.as_of_date <= as_of_date,
        ]
        if source is not None:
            conditions.append(InstrumentConstituent.source == source)
        latest_date = (
            await self.session.execute(
                select(func.max(InstrumentConstituent.as_of_date)).where(*conditions)
            )
        ).scalar_one_or_none()
        if latest_date is None:
            return []
        return await self.list_snapshot(instrument_id, latest_date, source=source)

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
            )
        )
        rows = result.scalars().all()
        latest_date_by_instrument: dict[int, date] = {}
        grouped: dict[int, list[InstrumentConstituent]] = {}
        for row in rows:
            iid = row.instrument_id
            if iid is None:
                continue
            latest = latest_date_by_instrument.get(iid)
            if latest is None:
                latest_date_by_instrument[iid] = row.as_of_date
                grouped[iid] = [row]
                continue
            if row.as_of_date == latest:
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
            base = base.where(Dividend.account_id == account_id)
        if symbol is not None:
            base = base.where(Dividend.symbol == symbol.upper())
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        result = await self.session.execute(
            base.order_by(Dividend.pay_date.desc(), Dividend.id.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total
