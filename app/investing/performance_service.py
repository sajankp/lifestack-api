"""Portfolio valuation, performance snapshots, and summary aggregation.

Split out of ``app/investing/service.py`` (chore/split-investing-service) —
pure move, no behavior change. External market-data fetch helpers
(``_fetch_stock_price``, ``_fetch_all_amfi_navs``, etc.) stay defined in
``app.investing.service`` since ``HoldingService``/``InstrumentService``
there need them too; this module calls them via the module reference (not
``from ... import``) so ``unittest.mock.patch("app.investing.service.
_fetch_stock_price", ...)`` in existing tests keeps working unchanged.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select

from app.core.currency import (
    build_required_pairs as _build_required_pairs,
)
from app.core.currency import (
    convert_amount as _convert_amount,
)
from app.core.currency import (
    effective_display_as_of,
)
from app.core.currency import (
    fx_rates_used as _fx_rates_used,
)
from app.core.exceptions import NotFoundError, ValidationError
from app.finance.models import FxRate
from app.finance.repository import AccountRepository, FinanceSettingRepository, FxRateRepository
from app.investing import service as core_service
from app.investing.models import Company, Dividend, Instrument, InstrumentType, PortfolioSnapshot
from app.investing.repository import (
    CashBalanceRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InstrumentRepository,
    PortfolioSnapshotRepository,
)
from app.investing.schemas import (
    AssetClassAllocationItem,
    DividendHistoryResponse,
    HoldingPriceBulkCreate,
    InvestingSummaryResponse,
    MonthlyDividendPoint,
    PerformanceHistoryPoint,
    PerformanceHistoryResponse,
    PerformanceSummaryResponse,
    PortfolioAllocationResponse,
    SectorAllocationItem,
)
from app.investing.service import MONEY_QUANT


def _value_change(current: Decimal, baseline: Decimal) -> tuple[Decimal, Decimal | None]:
    change = current - baseline
    percentage = change / baseline * Decimal("100") if baseline else None
    return change, percentage


class PerformanceService:
    def __init__(
        self,
        holding_repo: HoldingRepository,
        cash_repo: CashBalanceRepository,
        holding_price_repo: HoldingPriceRepository,
        snapshot_repo: PortfolioSnapshotRepository,
        finance_setting_repo: FinanceSettingRepository | None = None,
        fx_rate_repo: FxRateRepository | None = None,
        instrument_repo: InstrumentRepository | None = None,
        account_repo: AccountRepository | None = None,
    ):
        self.holding_repo = holding_repo
        self.cash_repo = cash_repo
        self.holding_price_repo = holding_price_repo
        self.snapshot_repo = snapshot_repo
        self.finance_setting_repo = finance_setting_repo
        self.fx_rate_repo = fx_rate_repo
        self.instrument_repo = instrument_repo
        self.account_repo = account_repo

    async def refresh_workspace_prices(self, workspace_id: int) -> dict[str, Decimal]:
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
        if not holdings:
            return {}

        today = datetime.now(UTC).date()
        expected_close_date = core_service._previous_weekday(today)
        holding_ids = [holding.id for holding in holdings if holding.id is not None]
        if not holding_ids:
            return {}
        latest_prices = await self.holding_price_repo.latest_prices_on_or_before_bulk(
            workspace_id, holding_ids, expected_close_date
        )
        holdings_to_fetch = [
            holding
            for holding in holdings
            if holding.id is not None
            and not (
                (latest := latest_prices.get(holding.id))
                and (
                    latest.price_date == expected_close_date
                    or (
                        latest.source == "api" and latest.created_at.astimezone(UTC).date() == today
                    )
                )
            )
        ]
        if not holdings_to_fetch:
            return {}

        instruments = (
            await self.instrument_repo.get_by_ids([
                holding.instrument_id for holding in holdings_to_fetch if holding.instrument_id
            ])
            if self.instrument_repo is not None
            else {}
        )
        unique_keys = sorted({
            (
                h.symbol.upper().strip(),
                h.currency.upper().strip(),
                instruments[h.instrument_id].instrument_type
                if h.instrument_id in instruments
                else InstrumentType.stock.value,
            )
            for h in holdings_to_fetch
        })
        sem = asyncio.Semaphore(5)

        async with httpx.AsyncClient(timeout=10.0) as client:
            amfi_navs = (
                await core_service._fetch_all_amfi_navs(client)
                if any(
                    kind == InstrumentType.mutual_fund.value and curr == "INR"
                    for _, curr, kind in unique_keys
                )
                else {}
            )

            async def throttled_fetch(
                sym: str, curr: str, instrument_type: str
            ) -> tuple[date, Decimal] | None:
                async with sem:
                    if instrument_type == InstrumentType.mutual_fund.value and curr == "INR":
                        return core_service._get_amfi_nav(sym, amfi_navs)
                    return await core_service._fetch_stock_price(client, sym, curr)

            tasks = [throttled_fetch(sym, curr, kind) for sym, curr, kind in unique_keys]
            results = await asyncio.gather(*tasks)

        price_map = {
            (sym, curr, kind): close
            for (sym, curr, kind), close in zip(unique_keys, results, strict=False)
            if close is not None
        }

        prices_updated = {}
        if price_map:
            for h in holdings_to_fetch:
                if h.id is not None:
                    sym = h.symbol.upper().strip()
                    curr = h.currency.upper().strip()
                    kind = (
                        instruments[h.instrument_id].instrument_type
                        if h.instrument_id in instruments
                        else InstrumentType.stock.value
                    )
                    close = price_map.get((sym, curr, kind))
                    if close is not None:
                        if isinstance(close, tuple):
                            price_date, price = close
                        else:
                            # Compatibility for injected providers that return only a price.
                            price_date, price = expected_close_date, close
                        await self.holding_price_repo.upsert_price(
                            workspace_id=workspace_id,
                            holding_id=h.id,
                            price_date=price_date,
                            unit_price=price,
                            source="api",
                        )
                        prices_updated[sym] = price
            await self.snapshot_repo.delete_for_date(workspace_id, today)

        return prices_updated

    async def submit_prices(self, workspace_id: int, payload: HoldingPriceBulkCreate) -> None:
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
        by_public = {h.public_id: h for h in holdings}
        prices_to_upsert: list[tuple[int, object]] = []
        for item in payload.prices:
            holding = by_public.get(item.holding_public_id)
            if holding is None or holding.id is None:
                raise NotFoundError(detail=f"Holding with id {item.holding_public_id} not found")
            prices_to_upsert.append((holding.id, item.unit_price))
        await self.holding_price_repo.bulk_upsert_prices(
            workspace_id=workspace_id,
            price_date=payload.price_date,
            prices=prices_to_upsert,
            source="manual",
        )
        if payload.price_date == datetime.now(UTC).date():
            await self.snapshot_repo.delete_for_date(workspace_id, payload.price_date)

    async def create_snapshot(self, workspace_id: int, snapshot_date: date) -> None:
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
        as_of_datetime = datetime.combine(snapshot_date, datetime.max.time(), UTC)
        cash_balances = await self.cash_repo.get_latest_per_account_currency(
            workspace_id, as_of=as_of_datetime
        )
        # Cash balances are legitimately added to any account type (reconciliation
        # snapshots on bank/wallet accounts), but only brokerage cash belongs in
        # the investing performance total -- mirrors the same filter in
        # InvestingSummaryService.get_summary() (spec-050).
        if self.account_repo is None:
            raise ValidationError(detail="Account repository is required for performance snapshots")
        if cash_balances:
            accounts, _ = await self.account_repo.list_workspace_accounts(
                workspace_id, limit=10000, offset=0
            )
            brokerage_ids = {a.id for a in accounts if a.account_type == "brokerage"}
            cash_balances = [c for c in cash_balances if c.account_id in brokerage_ids]
        holdings_value = Decimal("0")
        total_cost = Decimal("0")
        cash_value = Decimal("0")
        used_currencies = sorted({
            *(h.currency.upper() for h in holdings),
            *(c.currency.upper() for c in cash_balances),
        })

        reporting_currency: str | None = None
        if self.finance_setting_repo is not None:
            settings = await self.finance_setting_repo.get_by_workspace(workspace_id)
            if settings and settings.reporting_currency_code:
                reporting_currency = settings.reporting_currency_code.upper()

        if not used_currencies:
            reporting_currency = reporting_currency or "USD"
        elif reporting_currency is None:
            if len(used_currencies) > 1:
                raise ValidationError(
                    detail=(
                        "Reporting currency is required for multi-currency performance snapshots"
                    )
                )
            reporting_currency = used_currencies[0]

        fx_lookup: dict[tuple[str, str], FxRate] = {}
        if any(curr != reporting_currency for curr in used_currencies):
            if self.fx_rate_repo is None:
                raise ValidationError(
                    detail="FX rates are required for multi-currency performance snapshots"
                )
            required_pairs = _build_required_pairs(used_currencies, reporting_currency)
            fx_lookup = await self.fx_rate_repo.get_latest_rates_for_pairs(
                list(required_pairs),
                as_of=effective_display_as_of(
                    datetime.combine(snapshot_date, datetime.max.time(), UTC)
                ),
            )

        holding_ids = [h.id for h in holdings if h.id is not None]
        latest_prices = await self.holding_price_repo.latest_prices_on_or_before_bulk(
            workspace_id, holding_ids, snapshot_date
        )
        for h in holdings:
            if h.id is None:
                continue
            latest_price = latest_prices.get(h.id)
            unit_price = latest_price.unit_price if latest_price is not None else h.avg_cost
            currency = h.currency.upper()
            native_value = h.quantity * unit_price
            native_cost = h.quantity * h.avg_cost
            converted_value = _convert_amount(native_value, currency, reporting_currency, fx_lookup)
            converted_cost = _convert_amount(native_cost, currency, reporting_currency, fx_lookup)
            if converted_value is None or converted_cost is None:
                raise ValidationError(
                    detail=(
                        f"FX rate from {currency} to {reporting_currency} is required "
                        "for performance snapshots"
                    )
                )
            holdings_value += converted_value
            total_cost += converted_cost
        for cash in cash_balances:
            currency = cash.currency.upper()
            converted_value = _convert_amount(cash.balance, currency, reporting_currency, fx_lookup)
            if converted_value is None:
                raise ValidationError(
                    detail=(
                        f"FX rate from {currency} to {reporting_currency} is required "
                        "for performance snapshots"
                    )
                )
            cash_value += converted_value
        total_value = holdings_value + cash_value
        await self.snapshot_repo.upsert(
            PortfolioSnapshot(
                workspace_id=workspace_id,
                snapshot_date=snapshot_date,
                total_value=total_value.quantize(MONEY_QUANT),
                total_cost=total_cost.quantize(MONEY_QUANT),
                holdings_value=holdings_value.quantize(MONEY_QUANT),
                cash_value=cash_value.quantize(MONEY_QUANT),
                currency_code=reporting_currency,
                fx_rates_used=_fx_rates_used(used_currencies, reporting_currency, fx_lookup),
            )
        )

    async def summary(self, workspace_id: int) -> PerformanceSummaryResponse:
        configured_reporting_currency: str | None = None
        if self.finance_setting_repo is not None:
            settings = await self.finance_setting_repo.get_by_workspace(workspace_id)
            if settings and settings.reporting_currency_code:
                configured_reporting_currency = settings.reporting_currency_code.upper()

        today = datetime.now(UTC).date()
        snapshot = await self.snapshot_repo.latest(workspace_id)
        if (
            snapshot is None
            or snapshot.snapshot_date != today
            or (
                configured_reporting_currency is not None
                and snapshot.currency_code != configured_reporting_currency
            )
        ):
            await self.create_snapshot(workspace_id, today)
            snapshot = await self.snapshot_repo.latest(workspace_id)
        if snapshot is None:
            raise ValidationError(detail="Failed to generate portfolio snapshot")
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
        holding_ids = [holding.id for holding in holdings if holding.id is not None]
        latest_prices = await self.holding_price_repo.latest_prices_on_or_before_bulk(
            workspace_id, holding_ids, snapshot.snapshot_date
        )
        if not holdings:
            valuation_status = "empty"
        elif all(
            holding.id in latest_prices
            and latest_prices[holding.id].price_date == snapshot.snapshot_date
            for holding in holdings
            if holding.id is not None
        ):
            valuation_status = "current"
        else:
            valuation_status = "estimated"

        previous = await self.snapshot_repo.latest_before(workspace_id, snapshot.snapshot_date)
        if previous is not None and previous.currency_code == snapshot.currency_code:
            daily_change, daily_change_pct = _value_change(
                snapshot.holdings_value, previous.holdings_value
            )
            previous_snapshot_date = previous.snapshot_date
        else:
            daily_change = None
            daily_change_pct = None
            previous_snapshot_date = None

        reporting_currency = snapshot.currency_code
        as_of_datetime = datetime.combine(snapshot.snapshot_date, datetime.max.time(), UTC)
        if self.account_repo is None:
            raise ValidationError(detail="Account repository is required for live cash calculation")
        live_cash = await _live_cash_total(
            workspace_id=workspace_id,
            reporting_currency=reporting_currency,
            cash_repo=self.cash_repo,
            account_repo=self.account_repo,
            fx_rate_repo=self.fx_rate_repo,
            as_of=as_of_datetime,
        )
        if live_cash is None:
            raise ValidationError(
                detail=f"FX rate from cash currencies to {reporting_currency} is required for performance summary"
            )

        gain, pct = _value_change(snapshot.holdings_value, snapshot.total_cost)
        return PerformanceSummaryResponse(
            total_value=snapshot.holdings_value,
            total_cost=snapshot.total_cost,
            portfolio_value=snapshot.holdings_value,
            invested_value=snapshot.total_cost,
            cash_total=live_cash,
            total_gain_loss=gain,
            total_gain_loss_pct=pct,
            daily_change=daily_change,
            daily_change_pct=daily_change_pct,
            snapshot_date=snapshot.snapshot_date,
            previous_snapshot_date=previous_snapshot_date,
            currency=snapshot.currency_code,
            valuation_status=valuation_status,
            holdings_count=len(holdings),
            fx_rates_used=snapshot.fx_rates_used or {},
        )

    async def get_performance_history(
        self,
        workspace_id: int,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> PerformanceHistoryResponse:
        configured_reporting_currency = "USD"
        if self.finance_setting_repo is not None:
            settings = await self.finance_setting_repo.get_by_workspace(workspace_id)
            if settings and settings.reporting_currency_code:
                configured_reporting_currency = settings.reporting_currency_code.upper()

        snapshots = (
            await self.snapshot_repo.list_range(workspace_id, from_date, to_date)
            if self.snapshot_repo is not None
            else []
        )
        points: list[PerformanceHistoryPoint] = []
        for s in snapshots:
            unrealized = s.holdings_value - s.total_cost
            unrealized_pct = (
                (unrealized / s.total_cost * Decimal("100")).quantize(Decimal("0.01"))
                if s.total_cost > 0
                else None
            )
            points.append(
                PerformanceHistoryPoint(
                    snapshot_date=s.snapshot_date,
                    holdings_value=s.holdings_value,
                    total_cost=s.total_cost,
                    total_value=s.total_value,
                    cash_value=s.cash_value,
                    unrealized_gain_loss=unrealized,
                    unrealized_gain_loss_pct=unrealized_pct,
                )
            )
        currency = snapshots[-1].currency_code if snapshots else configured_reporting_currency
        return PerformanceHistoryResponse(currency=currency, points=points)

    async def get_allocation_breakdown(
        self,
        workspace_id: int,
        as_of: date | None = None,
    ) -> PortfolioAllocationResponse:
        today = as_of or datetime.now(UTC).date()
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
        open_holdings = [h for h in holdings if h.quantity != 0]

        cash_balances = await self.cash_repo.get_latest_per_account_currency(workspace_id)
        if self.account_repo is not None:
            accounts, _ = await self.account_repo.list_workspace_accounts(
                workspace_id, limit=10000, offset=0
            )
            brokerage_ids = {a.id for a in accounts if a.account_type == "brokerage"}
            cash_balances = [c for c in cash_balances if c.account_id in brokerage_ids]

        # Determine reporting currency
        reporting_currency = "USD"
        if self.finance_setting_repo is not None:
            settings = await self.finance_setting_repo.get_by_workspace(workspace_id)
            if settings and settings.reporting_currency_code:
                reporting_currency = settings.reporting_currency_code.upper()
            elif open_holdings:
                reporting_currency = open_holdings[0].currency.upper()
            elif cash_balances:
                reporting_currency = cash_balances[0].currency.upper()

        # FX rates if needed
        all_currencies = {h.currency.upper() for h in open_holdings} | {
            c.currency.upper() for c in cash_balances
        }
        fx_lookup: dict[tuple[str, str], FxRate] = {}
        if self.fx_rate_repo is not None and any(c != reporting_currency for c in all_currencies):
            pairs = _build_required_pairs(all_currencies, reporting_currency)
            as_of_dt = datetime.combine(today, datetime.max.time(), tzinfo=UTC)
            fx_lookup = await self.fx_rate_repo.get_latest_rates_for_pairs(
                list(pairs), effective_display_as_of(as_of_dt)
            )

        # Get latest prices for open holdings
        latest_prices = {}
        if self.holding_price_repo is not None and open_holdings:
            holding_ids = [h.id for h in open_holdings if h.id is not None]
            latest_prices = await self.holding_price_repo.latest_prices_on_or_before_bulk(
                workspace_id=workspace_id, holding_ids=holding_ids, as_of=today
            )

        # Preload Instruments and Companies
        instrument_ids = [h.instrument_id for h in open_holdings if h.instrument_id is not None]
        instruments: dict[int, Instrument] = {}
        companies: dict[int, Company] = {}
        if instrument_ids and self.instrument_repo is not None:
            instruments = await self.instrument_repo.get_by_ids(instrument_ids)
            company_ids = [
                inst.company_id for inst in instruments.values() if inst.company_id is not None
            ]
            if company_ids:
                comp_stmt = select(Company).where(Company.id.in_(company_ids))
                comp_rows = (await self.holding_repo.session.execute(comp_stmt)).scalars().all()
                companies = {c.id: c for c in comp_rows if c.id is not None}

        # Calculate values converted to reporting currency
        total_holdings_val = Decimal("0.00")
        class_buckets: dict[str, dict[str, Any]] = {
            "stock": {"label": "Stocks", "value": Decimal("0.00"), "count": 0},
            "etf": {"label": "ETFs", "value": Decimal("0.00"), "count": 0},
            "mutual_fund": {"label": "Mutual Funds", "value": Decimal("0.00"), "count": 0},
            "cash": {"label": "Cash", "value": Decimal("0.00"), "count": 0},
        }
        sector_totals: dict[str, Decimal] = {}

        for h in open_holdings:
            p = latest_prices.get(h.id)
            raw_val = h.quantity * (p.unit_price if p is not None else h.avg_cost)
            converted = _convert_amount(raw_val, h.currency.upper(), reporting_currency, fx_lookup)
            val = (converted if converted is not None else raw_val).quantize(Decimal("0.01"))
            total_holdings_val += val

            inst = instruments.get(h.instrument_id)
            inst_type = inst.instrument_type if inst else "stock"
            if inst_type not in class_buckets:
                inst_type = "stock"
            class_buckets[inst_type]["value"] += val
            class_buckets[inst_type]["count"] += 1

            # Sector
            comp = companies.get(inst.company_id) if inst and inst.company_id else None
            sector = (
                comp.sector
                if comp and comp.sector
                else ("ETFs & Funds" if inst_type in ("etf", "mutual_fund") else "Unclassified")
            )
            sector_totals[sector] = sector_totals.get(sector, Decimal("0.00")) + val

        # Cash balances
        total_cash_val = Decimal("0.00")
        for c in cash_balances:
            c_converted = _convert_amount(
                c.balance, c.currency.upper(), reporting_currency, fx_lookup
            )
            c_val = (c_converted if c_converted is not None else c.balance).quantize(
                Decimal("0.01")
            )
            total_cash_val += c_val
            class_buckets["cash"]["value"] += c_val
            class_buckets["cash"]["count"] += 1

        total_portfolio_val = total_holdings_val + total_cash_val

        # Asset class allocation list
        asset_classes: list[AssetClassAllocationItem] = []
        for key, bucket in class_buckets.items():
            val = bucket["value"]
            pct = (
                round(float((val / total_portfolio_val) * 100), 2)
                if total_portfolio_val > 0
                else 0.0
            )
            asset_classes.append(
                AssetClassAllocationItem(
                    key=key,
                    label=bucket["label"],
                    value=val,
                    pct=pct,
                    count=bucket["count"],
                )
            )

        # Sector allocation list
        sectors: list[SectorAllocationItem] = []
        for sector_name, s_val in sorted(
            sector_totals.items(), key=lambda item: item[1], reverse=True
        ):
            s_pct = (
                round(float((s_val / total_portfolio_val) * 100), 2)
                if total_portfolio_val > 0
                else 0.0
            )
            sectors.append(
                SectorAllocationItem(
                    sector=sector_name,
                    value=s_val,
                    pct=s_pct,
                )
            )

        return PortfolioAllocationResponse(
            as_of_date=today,
            currency=reporting_currency,
            total_portfolio_value=total_portfolio_val.quantize(Decimal("0.01")),
            holdings_value=total_holdings_val.quantize(Decimal("0.01")),
            cash_value=total_cash_val.quantize(Decimal("0.01")),
            asset_classes=asset_classes,
            sectors=sectors,
        )

    async def get_dividend_history(self, workspace_id: int) -> DividendHistoryResponse:
        configured_reporting_currency = "USD"
        if self.finance_setting_repo is not None:
            settings = await self.finance_setting_repo.get_by_workspace(workspace_id)
            if settings and settings.reporting_currency_code:
                configured_reporting_currency = settings.reporting_currency_code.upper()

        stmt = (
            select(Dividend)
            .where(Dividend.workspace_id == workspace_id)
            .order_by(Dividend.pay_date.asc())
        )
        dividends = (await self.holding_repo.session.execute(stmt)).scalars().all()

        if not dividends:
            return DividendHistoryResponse(
                currency=configured_reporting_currency,
                total_dividends_received=Decimal("0.00"),
                trailing_12m_dividends=Decimal("0.00"),
                monthly_history=[],
            )

        used_currencies = sorted({d.currency.upper() for d in dividends})
        fx_lookup: dict[tuple[str, str], FxRate] = {}
        if any(curr != configured_reporting_currency for curr in used_currencies):
            if self.fx_rate_repo is not None:
                required_pairs = _build_required_pairs(used_currencies, configured_reporting_currency)
                fx_lookup = await self.fx_rate_repo.get_latest_rates_for_pairs(
                    list(required_pairs),
                    as_of=effective_display_as_of(datetime.now(UTC)),
                )

        monthly_map: dict[str, dict[str, Any]] = {}
        today = datetime.now(UTC).date()
        trailing_limit = today - timedelta(days=365)
        total_received = Decimal("0.00")
        trailing_12m = Decimal("0.00")

        for d in dividends:
            d_curr = d.currency.upper()
            gross_conv = _convert_amount(
                d.gross_amount, d_curr, configured_reporting_currency, fx_lookup
            )
            tax_conv = _convert_amount(
                d.tax_withheld, d_curr, configured_reporting_currency, fx_lookup
            )
            net_conv = _convert_amount(
                d.net_amount, d_curr, configured_reporting_currency, fx_lookup
            )

            gross_val = gross_conv if gross_conv is not None else d.gross_amount
            tax_val = tax_conv if tax_conv is not None else d.tax_withheld
            net_val = net_conv if net_conv is not None else d.net_amount

            month_key = d.pay_date.strftime("%Y-%m")
            bucket = monthly_map.setdefault(
                month_key,
                {
                    "gross": Decimal("0.00"),
                    "tax": Decimal("0.00"),
                    "net": Decimal("0.00"),
                    "count": 0,
                },
            )
            bucket["gross"] += gross_val
            bucket["tax"] += tax_val
            bucket["net"] += net_val
            bucket["count"] += 1

            total_received += net_val
            if d.pay_date >= trailing_limit:
                trailing_12m += net_val

        monthly_history = [
            MonthlyDividendPoint(
                month=m,
                gross_amount=b["gross"].quantize(Decimal("0.01")),
                tax_withheld=b["tax"].quantize(Decimal("0.01")),
                net_amount=b["net"].quantize(Decimal("0.01")),
                payment_count=b["count"],
            )
            for m, b in sorted(monthly_map.items(), key=lambda x: x[0])
        ]

        return DividendHistoryResponse(
            currency=configured_reporting_currency,
            total_dividends_received=total_received.quantize(Decimal("0.01")),
            trailing_12m_dividends=trailing_12m.quantize(Decimal("0.01")),
            monthly_history=monthly_history,
        )


async def _live_cash_total(
    workspace_id: int,
    reporting_currency: str | None,
    cash_repo: CashBalanceRepository,
    account_repo: AccountRepository,
    fx_rate_repo: FxRateRepository | None,
    as_of: datetime | None = None,
) -> Decimal | None:
    """Compute live, brokerage-filtered, FX-converted investing cash total.

    Returns Decimal(0) if no brokerage cash exists, or None if conversion is required
    but missing. (INV-1)
    """
    cash_balances = await cash_repo.get_latest_per_account_currency(workspace_id, as_of=as_of)
    if not cash_balances:
        return Decimal("0.00")

    accounts, _ = await account_repo.list_workspace_accounts(workspace_id, limit=10000, offset=0)
    brokerage_ids = {a.id for a in accounts if a.account_type == "brokerage"}
    cash_balances = [c for c in cash_balances if c.account_id in brokerage_ids]

    if not cash_balances:
        return Decimal("0.00")

    # If reporting_currency is None, check if we have single currency
    if reporting_currency is None:
        currencies = {c.currency.upper() for c in cash_balances}
        if len(currencies) == 1:
            reporting_currency = list(currencies)[0]
        else:
            return None

    reporting_currency = reporting_currency.upper()
    unique_currencies = {c.currency.upper() for c in cash_balances}

    # No conversion needed if all currencies match reporting currency
    if all(curr == reporting_currency for curr in unique_currencies):
        return sum(c.balance for c in cash_balances)

    if fx_rate_repo is None:
        return None

    required_pairs = _build_required_pairs(list(unique_currencies), reporting_currency)
    fx_lookup = await fx_rate_repo.get_latest_rates_for_pairs(
        list(required_pairs),
        as_of=effective_display_as_of(as_of),
    )

    converted_cash = Decimal("0")
    for cash in cash_balances:
        curr = cash.currency.upper()
        converted_value = _convert_amount(cash.balance, curr, reporting_currency, fx_lookup)
        if converted_value is None:
            return None
        converted_cash += converted_value

    return converted_cash


class InvestingSummaryService:
    def __init__(
        self,
        holding_repo: HoldingRepository,
        cash_repo: CashBalanceRepository,
        finance_setting_repo: FinanceSettingRepository | None = None,
        fx_rate_repo: FxRateRepository | None = None,
        holding_price_repo: HoldingPriceRepository | None = None,
        snapshot_repo: PortfolioSnapshotRepository | None = None,
        account_repo: AccountRepository | None = None,
    ):
        self.holding_repo = holding_repo
        self.cash_repo = cash_repo
        self.finance_setting_repo = finance_setting_repo
        self.fx_rate_repo = fx_rate_repo
        self.holding_price_repo = holding_price_repo
        self.snapshot_repo = snapshot_repo
        self.account_repo = account_repo

    async def get_summary(self, workspace_id: int) -> InvestingSummaryResponse:
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
        cash_balances = await self.cash_repo.get_latest_per_account_currency(workspace_id)
        # Cash balances are legitimately added to any account type (reconciliation
        # snapshots on bank/wallet accounts), but only brokerage cash belongs in
        # the investing total -- otherwise a wallet account with both ledger
        # activity (spending_total) and a manual cash-balance snapshot gets
        # double-counted in net worth (spec-050).
        if self.account_repo is not None:
            accounts, _ = await self.account_repo.list_workspace_accounts(
                workspace_id, limit=10000, offset=0
            )
            brokerage_ids = {a.id for a in accounts if a.account_type == "brokerage"}
            cash_balances = [c for c in cash_balances if c.account_id in brokerage_ids]

        today = datetime.now(UTC).date()
        latest_prices = {}
        if self.holding_price_repo is not None and holdings:
            holding_ids = [h.id for h in holdings if h.id is not None]
            latest_prices = await self.holding_price_repo.latest_prices_on_or_before_bulk(
                workspace_id=workspace_id, holding_ids=holding_ids, as_of=today
            )

        used_cost_basis_fallback = False
        holding_values = {}
        for holding in holdings:
            price_record = latest_prices.get(holding.id)
            if price_record is not None:
                holding_values[holding.id] = holding.quantity * price_record.unit_price
            else:
                holding_values[holding.id] = holding.quantity * holding.avg_cost
                used_cost_basis_fallback = True

        breakdown: dict[str, Decimal] = {}

        for holding in holdings:
            value = holding_values[holding.id]
            curr = holding.currency.upper()
            breakdown[curr] = breakdown.get(curr, Decimal("0")) + value

        for cash in cash_balances:
            curr = cash.currency.upper()
            breakdown[curr] = breakdown.get(curr, Decimal("0")) + cash.balance

        used_currencies = sorted(breakdown.keys())
        reporting_currency: str | None = None
        if self.finance_setting_repo is not None:
            settings = await self.finance_setting_repo.get_by_workspace(workspace_id)
            if settings and settings.reporting_currency_code:
                reporting_currency = settings.reporting_currency_code.upper()

        # No data in workspace -> trivially valued as zero.
        if not used_currencies:
            return InvestingSummaryResponse(
                portfolio_value=Decimal("0"),
                holdings_count=0,
                cash_total=Decimal("0"),
                currency_breakdown={},
                daily_change=None,
                reporting_currency=reporting_currency,
                valuation_status="empty",
                fx_as_of=None,
            )

        if reporting_currency is None:
            # If there is only one currency, we can report deterministic native totals.
            if len(used_currencies) == 1:
                currency = used_currencies[0]
                portfolio_value = Decimal("0")
                for holding in holdings:
                    portfolio_value += holding_values[holding.id]

                if self.account_repo is None:
                    raise ValidationError(
                        detail="Account repository is required for live cash calculation"
                    )
                live_cash = await _live_cash_total(
                    workspace_id=workspace_id,
                    reporting_currency=currency,
                    cash_repo=self.cash_repo,
                    account_repo=self.account_repo,
                    fx_rate_repo=self.fx_rate_repo,
                    as_of=datetime.combine(today, datetime.max.time(), UTC),
                )
                if live_cash is None:
                    live_cash = Decimal("0")

                # Calculate daily change (holdings-only, INV-2)
                daily_change = None
                if self.snapshot_repo is not None:
                    prev_snapshot = await self.snapshot_repo.latest_before(workspace_id, today)
                    if prev_snapshot is not None:
                        daily_change = portfolio_value - prev_snapshot.holdings_value

                return InvestingSummaryResponse(
                    portfolio_value=portfolio_value,
                    holdings_count=len(holdings),
                    cash_total=live_cash,
                    currency_breakdown=breakdown,
                    daily_change=daily_change,
                    reporting_currency=currency,
                    valuation_status="cost_basis_fallback"
                    if used_cost_basis_fallback
                    else "single_currency_native",
                    fx_as_of=None,
                )

            # Multi-currency portfolio without configured reporting currency.
            return InvestingSummaryResponse(
                portfolio_value=None,
                holdings_count=len(holdings),
                cash_total=None,
                currency_breakdown=breakdown,
                daily_change=None,
                reporting_currency=None,
                valuation_status="multi_currency_unconverted",
                fx_as_of=None,
            )

        if any(curr != reporting_currency for curr in used_currencies):
            if self.fx_rate_repo is None:
                return InvestingSummaryResponse(
                    portfolio_value=None,
                    holdings_count=len(holdings),
                    cash_total=None,
                    currency_breakdown=breakdown,
                    daily_change=None,
                    reporting_currency=reporting_currency,
                    valuation_status="conversion_required",
                    fx_as_of=None,
                )

            valuation_as_of = effective_display_as_of()
            required_pairs = _build_required_pairs(used_currencies, reporting_currency)
            fx_lookup = await self.fx_rate_repo.get_latest_rates_for_pairs(
                list(required_pairs), as_of=valuation_as_of
            )

            converted_portfolio = Decimal("0")
            for holding in holdings:
                native_value = holding_values[holding.id]
                curr = holding.currency.upper()
                converted_value = _convert_amount(native_value, curr, reporting_currency, fx_lookup)
                if converted_value is None:
                    return InvestingSummaryResponse(
                        portfolio_value=None,
                        holdings_count=len(holdings),
                        cash_total=None,
                        currency_breakdown=breakdown,
                        daily_change=None,
                        reporting_currency=reporting_currency,
                        valuation_status="conversion_required",
                        fx_as_of=None,
                    )
                converted_portfolio += converted_value

            if self.account_repo is None:
                raise ValidationError(
                    detail="Account repository is required for live cash calculation"
                )
            live_cash = await _live_cash_total(
                workspace_id=workspace_id,
                reporting_currency=reporting_currency,
                cash_repo=self.cash_repo,
                account_repo=self.account_repo,
                fx_rate_repo=self.fx_rate_repo,
                as_of=datetime.combine(today, datetime.max.time(), UTC),
            )
            if live_cash is None:
                return InvestingSummaryResponse(
                    portfolio_value=None,
                    holdings_count=len(holdings),
                    cash_total=None,
                    currency_breakdown=breakdown,
                    daily_change=None,
                    reporting_currency=reporting_currency,
                    valuation_status="conversion_required",
                    fx_as_of=None,
                )

            # Calculate daily change (holdings-only, INV-2)
            daily_change = None
            if self.snapshot_repo is not None:
                prev_snapshot = await self.snapshot_repo.latest_before(workspace_id, today)
                if prev_snapshot is not None:
                    daily_change = converted_portfolio - prev_snapshot.holdings_value

            return InvestingSummaryResponse(
                portfolio_value=converted_portfolio,
                holdings_count=len(holdings),
                cash_total=live_cash,
                currency_breakdown=breakdown,
                daily_change=daily_change,
                reporting_currency=reporting_currency,
                valuation_status="cost_basis_fallback"
                if used_cost_basis_fallback
                else "converted_available",
                fx_as_of=valuation_as_of,
                fx_rates_used=_fx_rates_used(used_currencies, reporting_currency, fx_lookup),
            )

        portfolio_value = Decimal("0")
        for holding in holdings:
            portfolio_value += holding_values[holding.id]

        if self.account_repo is None:
            raise ValidationError(detail="Account repository is required for live cash calculation")
        live_cash = await _live_cash_total(
            workspace_id=workspace_id,
            reporting_currency=reporting_currency,
            cash_repo=self.cash_repo,
            account_repo=self.account_repo,
            fx_rate_repo=self.fx_rate_repo,
            as_of=datetime.combine(today, datetime.max.time(), UTC),
        )
        if live_cash is None:
            live_cash = Decimal("0")

        # Calculate daily change (holdings-only, INV-2)
        daily_change = None
        if self.snapshot_repo is not None:
            prev_snapshot = await self.snapshot_repo.latest_before(workspace_id, today)
            if prev_snapshot is not None:
                daily_change = portfolio_value - prev_snapshot.holdings_value

        return InvestingSummaryResponse(
            portfolio_value=portfolio_value,
            holdings_count=len(holdings),
            cash_total=live_cash,
            currency_breakdown=breakdown,
            daily_change=daily_change,
            reporting_currency=reporting_currency,
            valuation_status="cost_basis_fallback"
            if used_cost_basis_fallback
            else "converted_available",
            fx_as_of=None,
        )
