from collections.abc import Sequence
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import DEFAULT_LIMIT
from app.investing.models import (
    CashBalance,
    Company,
    Holding,
    HoldingPrice,
    Instrument,
    InstrumentConstituent,
    InvestingOrder,
    LotConsumption,
    OrderLot,
    PortfolioSnapshot,
)


class HoldingRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

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

    async def create(self, holding: Holding) -> Holding:
        self.session.add(holding)
        await self.session.flush()
        await self.session.refresh(holding)
        return holding

    async def save(self, holding: Holding) -> Holding:
        self.session.add(holding)
        await self.session.flush()
        await self.session.refresh(holding)
        return holding

    async def delete(self, holding: Holding) -> None:
        await self.session.delete(holding)
        await self.session.flush()


class CashBalanceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_all(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[CashBalance], int]:
        base = select(CashBalance).where(CashBalance.workspace_id == workspace_id)
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

    async def create(self, cash_balance: CashBalance) -> CashBalance:
        self.session.add(cash_balance)
        await self.session.flush()
        await self.session.refresh(cash_balance)
        return cash_balance

    async def save(self, cash_balance: CashBalance) -> CashBalance:
        self.session.add(cash_balance)
        await self.session.flush()
        await self.session.refresh(cash_balance)
        return cash_balance

    async def delete(self, cash_balance: CashBalance) -> None:
        await self.session.delete(cash_balance)
        await self.session.flush()

    async def get_by_trigger_ref(self, workspace_id: int, trigger_ref: UUID) -> CashBalance | None:
        result = await self.session.execute(
            select(CashBalance).where(
                CashBalance.workspace_id == workspace_id,
                CashBalance.trigger_ref == trigger_ref,
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


class InvestingOrderRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, order: InvestingOrder) -> InvestingOrder:
        self.session.add(order)
        await self.session.flush()
        await self.session.refresh(order)
        return order

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
    ) -> tuple[Sequence[InvestingOrder], int]:
        base = select(InvestingOrder).where(InvestingOrder.workspace_id == workspace_id)
        if symbol is not None:
            base = base.where(InvestingOrder.symbol == symbol.upper())
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

    async def save(self, order: InvestingOrder) -> InvestingOrder:
        self.session.add(order)
        await self.session.flush()
        await self.session.refresh(order)
        return order

    async def delete(self, order: InvestingOrder) -> None:
        await self.session.delete(order)
        await self.session.flush()

    async def bulk_create(self, orders: list[InvestingOrder]) -> list[InvestingOrder]:
        self.session.add_all(orders)
        await self.session.flush()
        for o in orders:
            await self.session.refresh(o)
        return orders


class LotRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def delete_for_holding(self, holding_id: int) -> None:
        await self.session.execute(delete(OrderLot).where(OrderLot.holding_id == holding_id))
        await self.session.flush()

    async def create_lots(self, lots: list[OrderLot]) -> list[OrderLot]:
        self.session.add_all(lots)
        await self.session.flush()
        for lot in lots:
            await self.session.refresh(lot)
        return lots

    async def create_consumptions(self, consumptions: list[LotConsumption]) -> list[LotConsumption]:
        self.session.add_all(consumptions)
        await self.session.flush()
        for consumption in consumptions:
            await self.session.refresh(consumption)
        return consumptions


class InstrumentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

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

    async def create(self, instrument: Instrument) -> Instrument:
        self.session.add(instrument)
        await self.session.flush()
        await self.session.refresh(instrument)
        return instrument

    async def save(self, instrument: Instrument) -> Instrument:
        self.session.add(instrument)
        await self.session.flush()
        await self.session.refresh(instrument)
        return instrument


class CompanyRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> Company | None:
        result = await self.session.execute(
            select(Company).where(
                (Company.workspace_id == workspace_id) | (Company.workspace_id.is_(None)),
                Company.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_name(self, workspace_id: int | None, name: str) -> Company | None:
        if workspace_id is not None:
            # Query workspace first (override)
            result = await self.session.execute(
                select(Company).where(
                    Company.workspace_id == workspace_id,
                    Company.name == name,
                )
            )
            val = result.scalar_one_or_none()
            if val is not None:
                return val

        # Query global
        res_global = await self.session.execute(
            select(Company).where(Company.workspace_id.is_(None), Company.name == name)
        )
        return res_global.scalar_one_or_none()

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

    async def get_by_names(self, workspace_id: int, names: Sequence[str]) -> dict[str, Company]:
        unique_names = {n for n in names if n}
        if not unique_names:
            return {}
        result = await self.session.execute(
            select(Company)
            .where(
                (Company.workspace_id == workspace_id) | (Company.workspace_id.is_(None)),
                Company.name.in_(unique_names),
            )
            .order_by(Company.workspace_id.desc().nulls_last())
        )
        rows = result.scalars().all()
        res = {}
        for row in rows:
            if row.name not in res:
                res[row.name] = row
        return res

    async def create(self, company: Company) -> Company:
        self.session.add(company)
        await self.session.flush()
        await self.session.refresh(company)
        return company


class InstrumentConstituentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def delete_snapshot(self, instrument_id: int, as_of_date: date, source: str) -> None:
        result = await self.session.execute(
            select(InstrumentConstituent).where(
                InstrumentConstituent.instrument_id == instrument_id,
                InstrumentConstituent.as_of_date == as_of_date,
                InstrumentConstituent.source == source,
            )
        )
        for row in result.scalars().all():
            await self.session.delete(row)
        await self.session.flush()

    async def create_many(self, rows: list[InstrumentConstituent]) -> list[InstrumentConstituent]:
        self.session.add_all(rows)
        await self.session.flush()
        return rows

    async def list_snapshot(
        self, instrument_id: int, as_of_date: date
    ) -> Sequence[InstrumentConstituent]:
        result = await self.session.execute(
            select(InstrumentConstituent).where(
                InstrumentConstituent.instrument_id == instrument_id,
                InstrumentConstituent.as_of_date == as_of_date,
            )
        )
        return result.scalars().all()

    async def get_latest_on_or_before(
        self, instrument_id: int, as_of_date: date
    ) -> Sequence[InstrumentConstituent]:
        latest_date = (
            await self.session.execute(
                select(func.max(InstrumentConstituent.as_of_date)).where(
                    InstrumentConstituent.instrument_id == instrument_id,
                    InstrumentConstituent.as_of_date <= as_of_date,
                )
            )
        ).scalar_one_or_none()
        if latest_date is None:
            return []
        return await self.list_snapshot(instrument_id, latest_date)

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
        existing = (
            await self.session.execute(
                select(PortfolioSnapshot).where(
                    PortfolioSnapshot.workspace_id == snapshot.workspace_id,
                    PortfolioSnapshot.snapshot_date == snapshot.snapshot_date,
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.total_value = snapshot.total_value
            existing.total_cost = snapshot.total_cost
            existing.holdings_value = snapshot.holdings_value
            existing.cash_value = snapshot.cash_value
            existing.currency_code = snapshot.currency_code
            existing.fx_rates_used = snapshot.fx_rates_used
            self.session.add(existing)
            await self.session.flush()
            return existing
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

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
