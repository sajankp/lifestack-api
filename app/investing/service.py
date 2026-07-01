import asyncio
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx

from app.config import settings
from app.core.audit import AuditLogger
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.pagination import DEFAULT_LIMIT
from app.finance.models import Account, FxRate
from app.finance.repository import (
    AccountRepository,
    CurrencyRepository,
    FinanceSettingRepository,
    FxRateRepository,
)
from app.investing.models import (
    CashBalance,
    Company,
    Holding,
    Instrument,
    InstrumentConstituent,
    InstrumentType,
    InvestingOrder,
    LotConsumption,
    OrderLot,
    OrderType,
    PortfolioSnapshot,
)
from app.investing.repository import (
    CashBalanceRepository,
    CompanyRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InstrumentConstituentRepository,
    InstrumentRepository,
    InvestingOrderRepository,
    LotRepository,
    PortfolioSnapshotRepository,
)
from app.investing.schemas import (
    CashBalanceCreate,
    CashBalanceUpdate,
    ExposureAnalyticsResponse,
    ExposureCompanyRow,
    HoldingPriceBulkCreate,
    HoldingUpdate,
    InstrumentConstituentResponse,
    InstrumentConstituentUpsert,
    InstrumentCreate,
    InstrumentUpdate,
    InvestingOrderCreate,
    InvestingOrderUpdate,
    InvestingSummaryResponse,
    OverlapAnalyticsResponse,
    OverlapRow,
    PerformanceSummaryResponse,
)

MONEY_QUANT = Decimal("0.01")


def _value_change(current: Decimal, baseline: Decimal) -> tuple[Decimal, Decimal | None]:
    change = current - baseline
    percentage = change / baseline * Decimal("100") if baseline else None
    return change, percentage


def _build_required_pairs(
    used_currencies: list[str], reporting_currency: str
) -> set[tuple[str, str]]:
    required_pairs: set[tuple[str, str]] = set()
    all_currencies = set(used_currencies) | {reporting_currency}
    for curr in all_currencies:
        if curr == "USD":
            continue
        required_pairs.add((curr, "USD"))
        required_pairs.add(("USD", curr))
    for curr in used_currencies:
        if curr == reporting_currency:
            continue
        required_pairs.add((curr, reporting_currency))
        required_pairs.add((reporting_currency, curr))
    return required_pairs


def _conversion_rate(
    source_currency: str,
    reporting_currency: str,
    fx_lookup: dict[tuple[str, str], FxRate],
) -> Decimal | None:
    if source_currency == reporting_currency:
        return Decimal("1")

    direct = fx_lookup.get((source_currency, reporting_currency))
    if direct is not None:
        return Decimal(str(direct.rate))

    inverse = fx_lookup.get((reporting_currency, source_currency))
    if inverse is not None:
        inverse_rate = Decimal(str(inverse.rate))
        if inverse_rate != 0:
            return Decimal("1") / inverse_rate

    def rate_to_usd(currency: str) -> Decimal | None:
        if currency == "USD":
            return Decimal("1")
        direct_to_usd = fx_lookup.get((currency, "USD"))
        if direct_to_usd is not None:
            return Decimal(str(direct_to_usd.rate))
        usd_to_currency = fx_lookup.get(("USD", currency))
        if usd_to_currency is not None:
            usd_to_currency_rate = Decimal(str(usd_to_currency.rate))
            if usd_to_currency_rate != 0:
                return Decimal("1") / usd_to_currency_rate
        return None

    source_to_usd = rate_to_usd(source_currency)
    reporting_to_usd = rate_to_usd(reporting_currency)
    if source_to_usd is not None and reporting_to_usd is not None and reporting_to_usd != 0:
        return source_to_usd / reporting_to_usd

    return None


def _convert_amount(
    amount: Decimal,
    source_currency: str,
    reporting_currency: str,
    fx_lookup: dict[tuple[str, str], FxRate],
) -> Decimal | None:
    rate = _conversion_rate(source_currency, reporting_currency, fx_lookup)
    if rate is None:
        return None
    return amount * rate


def _fx_rates_used(
    used_currencies: list[str],
    reporting_currency: str,
    fx_lookup: dict[tuple[str, str], FxRate],
) -> dict[str, str]:
    rates: dict[str, str] = {}
    for curr in used_currencies:
        if curr == reporting_currency:
            continue
        rate = _conversion_rate(curr, reporting_currency, fx_lookup)
        if rate is not None:
            rates[curr] = str(rate)
    return rates


def _snapshot_holding(holding: Holding) -> dict:
    return {
        "symbol": holding.symbol,
        "account_id": holding.account_id,
        "quantity": str(holding.quantity),
        "avg_cost": str(holding.avg_cost),
        "currency": holding.currency,
    }


def _snapshot_cash_balance(cash: CashBalance) -> dict:
    return {
        "account_id": cash.account_id,
        "balance": str(cash.balance),
        "currency": cash.currency,
        "as_of": cash.as_of.isoformat() if hasattr(cash.as_of, "isoformat") else str(cash.as_of),
    }


def _snapshot_order(order: InvestingOrder) -> dict:
    return {
        "order_type": order.order_type,
        "symbol": order.symbol,
        "currency": order.currency,
        "quantity": str(order.quantity),
        "price_per_unit": str(order.price_per_unit),
        "gross_amount": str(order.gross_amount),
        "brokerage_fee": str(order.brokerage_fee),
        "tax_amount": str(order.tax_amount),
        "other_fees": str(order.other_fees),
        "net_amount": str(order.net_amount),
        "occurred_at": order.occurred_at.isoformat()
        if hasattr(order.occurred_at, "isoformat")
        else str(order.occurred_at),
        "exchange_name": order.exchange_name,
        "notes": order.notes,
    }


class HoldingService:
    def __init__(
        self,
        repository: HoldingRepository,
        instrument_repo: InstrumentRepository | None = None,
        company_repo: CompanyRepository | None = None,
        account_repo: AccountRepository | None = None,
        currency_repo: CurrencyRepository | None = None,
        holding_price_repo: HoldingPriceRepository | None = None,
        order_repo: InvestingOrderRepository | None = None,
    ):
        self.repository = repository
        self.instrument_repo = instrument_repo
        self.company_repo = company_repo
        self.account_repo = account_repo
        self.currency_repo = currency_repo
        self.holding_price_repo = holding_price_repo
        self.order_repo = order_repo

    async def _resolve_or_create_instrument(
        self,
        workspace_id: int,
        symbol: str,
        instrument_type: InstrumentType,
        *,
        allow_type_change: bool = False,
    ) -> Instrument | None:
        if self.instrument_repo is None:
            return None
        # get_by_symbol already checks workspace override first, then global
        instrument = await self.instrument_repo.get_by_symbol(workspace_id, symbol)
        if instrument is not None:
            if allow_type_change and instrument.instrument_type != instrument_type.value:
                instrument.instrument_type = instrument_type.value
                instrument.updated_at = datetime.now(UTC)
                instrument = await self.instrument_repo.save(instrument)
            return instrument

        # If not found, check via Yahoo Finance
        target_workspace_id = workspace_id
        try:
            async with httpx.AsyncClient() as client:
                price_info = await _fetch_stock_price(client, symbol)
                if price_info is not None:
                    target_workspace_id = None
        except Exception:
            # Fallback to workspace-scoped instrument on external API failure
            pass

        company: Company | None = None
        if self.company_repo is not None and instrument_type == InstrumentType.stock:
            company = await self.company_repo.get_by_name(target_workspace_id, symbol)
            if company is None:
                company = await self.company_repo.create(
                    Company(workspace_id=target_workspace_id, name=symbol, ticker=symbol)
                )

        instrument = await self.instrument_repo.create(
            Instrument(
                workspace_id=target_workspace_id,
                symbol=symbol,
                name=symbol,
                instrument_type=instrument_type.value,
                company_id=company.id if company else None,
            )
        )
        return instrument

    async def _validate_refs(
        self, workspace_id: int, account_id: uuid.UUID, currency: str
    ) -> Account:
        if self.account_repo is None:
            raise ValidationError(detail="Account repository is not configured")
        account = await self.account_repo.get_by_public_id(workspace_id, account_id)
        if not account or not account.is_active:
            raise ValidationError(
                detail=f"Account with ID {account_id} is not found in this workspace"
            )
        if self.currency_repo is not None:
            code = currency.upper()
            currency_row = await self.currency_repo.get_by_code(code)
            if not currency_row or not currency_row.is_active:
                raise ValidationError(detail=f"Unsupported currency code '{code}'")
            await self.currency_repo.ensure_workspace_defaults(workspace_id)
            enabled = await self.currency_repo.is_enabled_for_workspace(workspace_id, code)
            if not enabled:
                raise ValidationError(detail=f"Currency '{code}' is not enabled for this workspace")
        return account

    async def list_holdings(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[Holding], int]:
        return await self.repository.get_all(workspace_id, limit, offset)

    async def get_holding(self, workspace_id: int, public_id: uuid.UUID) -> Holding:
        holding = await self.repository.get_by_public_id(workspace_id, public_id)
        if not holding:
            raise NotFoundError(detail=f"Holding with id {public_id} not found in this workspace")
        return holding

    async def update_holding(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        holding_in: HoldingUpdate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> Holding:
        holding = await self.get_holding(workspace_id, public_id)
        before_snap = _snapshot_holding(holding)

        update_data = holding_in.model_dump(exclude_unset=True)
        if not update_data:
            return holding

        requested_type = update_data.pop("instrument_type", None)
        symbol_changed = "symbol" in update_data and update_data["symbol"] != holding.symbol
        type_changed = requested_type is not None

        if holding.source_type == "order" and (
            "quantity" in update_data or "avg_cost" in update_data
        ):
            raise ValidationError(
                detail=(
                    "Quantity and average cost are computed from orders for this holding "
                    "and cannot be edited directly. Edit the underlying orders instead."
                )
            )

        if symbol_changed or type_changed:
            next_symbol = update_data.get("symbol") or holding.symbol
            if symbol_changed:
                duplicate = await self.repository.get_by_unique_key(
                    workspace_id, next_symbol, holding.account_id
                )
                if duplicate is not None and duplicate.id != holding.id:
                    raise ConflictError(
                        detail="A holding already exists for this symbol/account in this workspace"
                    )

                if holding.source_type == "order":
                    if self.order_repo is None:
                        raise ValidationError(
                            detail=(
                                "Order repository is not configured; cannot rename "
                                "order-derived holding."
                            )
                        )
                    await self.order_repo.rename_symbol(
                        workspace_id, holding.account_id, holding.symbol, next_symbol
                    )

            if (
                requested_type is None
                and holding.instrument_id is not None
                and self.instrument_repo
            ):
                current = (await self.instrument_repo.get_by_ids([holding.instrument_id])).get(
                    holding.instrument_id
                )
                requested_type = (
                    InstrumentType(current.instrument_type) if current else InstrumentType.stock
                )
            requested_type = requested_type or InstrumentType.stock
            instrument = await self._resolve_or_create_instrument(
                workspace_id, next_symbol, requested_type, allow_type_change=True
            )
            holding.instrument_id = instrument.id if instrument else None

        if symbol_changed and holding.id is not None and self.holding_price_repo is not None:
            await self.holding_price_repo.delete_for_holding(workspace_id, holding.id)

        next_currency = holding.currency
        if "currency" in update_data and update_data["currency"] is not None:
            next_currency = update_data["currency"]

        # Validate that the associated account is still active and valid
        if self.account_repo is not None:
            account = await self.account_repo.get_by_id(workspace_id, holding.account_id)
            if not account or not account.is_active:
                raise ValidationError(detail="Associated account is inactive or not found")
        if self.currency_repo is not None:
            code = next_currency.upper()
            currency_row = await self.currency_repo.get_by_code(code)
            if not currency_row or not currency_row.is_active:
                raise ValidationError(detail=f"Unsupported currency code '{code}'")
            enabled = await self.currency_repo.is_enabled_for_workspace(workspace_id, code)
            if not enabled:
                raise ValidationError(detail=f"Currency '{code}' is not enabled for this workspace")

        for key, value in update_data.items():
            setattr(holding, key, value)
        holding.updated_at = datetime.now(UTC)
        holding = await self.repository.save(holding)

        if audit_logger and actor_id is not None:
            after_snap = _snapshot_holding(holding)
            changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="update",
                module="investing",
                entity_type="holding",
                entity_id=holding.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(holding.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )
        return holding

    async def delete_holding(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        holding = await self.get_holding(workspace_id, public_id)
        before_snap = _snapshot_holding(holding)
        await self.repository.delete(holding)

        if audit_logger and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="delete",
                module="investing",
                entity_type="holding",
                entity_id=holding.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(holding.public_id),
                    "before": before_snap,
                    "after": None,
                    "changed_fields": [],
                },
            )


class CashBalanceService:
    def __init__(
        self,
        repository: CashBalanceRepository,
        account_repo: AccountRepository | None = None,
        currency_repo: CurrencyRepository | None = None,
    ):
        self.repository = repository
        self.account_repo = account_repo
        self.currency_repo = currency_repo

    async def _validate_refs(
        self, workspace_id: int, account_id: uuid.UUID, currency: str
    ) -> Account:
        if self.account_repo is None:
            raise ValidationError(detail="Account repository is not configured")
        account = await self.account_repo.get_by_public_id(workspace_id, account_id)
        if not account or not account.is_active:
            raise ValidationError(
                detail=f"Account with ID {account_id} is not found in this workspace"
            )
        if self.currency_repo is not None:
            code = currency.upper()
            currency_row = await self.currency_repo.get_by_code(code)
            if not currency_row or not currency_row.is_active:
                raise ValidationError(detail=f"Unsupported currency code '{code}'")
            await self.currency_repo.ensure_workspace_defaults(workspace_id)
            enabled = await self.currency_repo.is_enabled_for_workspace(workspace_id, code)
            if not enabled:
                raise ValidationError(detail=f"Currency '{code}' is not enabled for this workspace")
        return account

    async def list_cash_balances(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[CashBalance], int]:
        return await self.repository.get_all(workspace_id, limit, offset)

    async def get_cash_balance(self, workspace_id: int, public_id: uuid.UUID) -> CashBalance:
        cash = await self.repository.get_by_public_id(workspace_id, public_id)
        if not cash:
            raise NotFoundError(
                detail=f"Cash balance with id {public_id} not found in this workspace"
            )
        return cash

    async def create_cash_balance(
        self,
        user_id: int,
        workspace_id: int,
        cash_in: CashBalanceCreate,
        audit_logger: AuditLogger | None = None,
    ) -> CashBalance:
        account = await self._validate_refs(workspace_id, cash_in.account_id, cash_in.currency)
        cash = CashBalance(
            workspace_id=workspace_id,
            user_id=user_id,
            account_id=account.id,
            balance=cash_in.balance,
            currency=cash_in.currency,
            as_of=cash_in.as_of,
        )
        cash = await self.repository.create(cash)

        if audit_logger:
            after_snap = _snapshot_cash_balance(cash)
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="create",
                module="investing",
                entity_type="cash_balance",
                entity_id=cash.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(cash.public_id),
                    "before": None,
                    "after": after_snap,
                    "changed_fields": list(after_snap.keys()),
                },
            )
        return cash

    async def update_cash_balance(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        cash_in: CashBalanceUpdate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> CashBalance:
        cash = await self.get_cash_balance(workspace_id, public_id)
        before_snap = _snapshot_cash_balance(cash)

        update_data = cash_in.model_dump(exclude_unset=True)
        if not update_data:
            return cash

        next_currency = cash.currency
        if "currency" in update_data and update_data["currency"] is not None:
            next_currency = update_data["currency"]

        # Validate that the associated account is still active and valid
        if self.account_repo is not None:
            account = await self.account_repo.get_by_id(workspace_id, cash.account_id)
            if not account or not account.is_active:
                raise ValidationError(detail="Associated account is inactive or not found")
        if self.currency_repo is not None:
            code = next_currency.upper()
            currency_row = await self.currency_repo.get_by_code(code)
            if not currency_row or not currency_row.is_active:
                raise ValidationError(detail=f"Unsupported currency code '{code}'")
            enabled = await self.currency_repo.is_enabled_for_workspace(workspace_id, code)
            if not enabled:
                raise ValidationError(detail=f"Currency '{code}' is not enabled for this workspace")

        for key, value in update_data.items():
            setattr(cash, key, value)
        cash.updated_at = datetime.now(UTC)
        cash = await self.repository.save(cash)

        if audit_logger and actor_id is not None:
            after_snap = _snapshot_cash_balance(cash)
            changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="update",
                module="investing",
                entity_type="cash_balance",
                entity_id=cash.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(cash.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )
        return cash

    async def delete_cash_balance(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        cash = await self.get_cash_balance(workspace_id, public_id)
        before_snap = _snapshot_cash_balance(cash)
        await self.repository.delete(cash)

        if audit_logger and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="delete",
                module="investing",
                entity_type="cash_balance",
                entity_id=cash.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(cash.public_id),
                    "before": before_snap,
                    "after": None,
                    "changed_fields": [],
                },
            )


def _previous_weekday(value: date) -> date:
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


async def _fetch_stock_price(
    client: httpx.AsyncClient, symbol: str, currency: str | None = None
) -> tuple[date, Decimal] | None:
    sym = symbol.upper().strip()
    if currency and currency.upper() == "INR" and "." not in sym:
        sym = f"{sym}.NS"

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    try:
        resp = await client.get(
            url,
            headers=headers,
            params={"interval": "1d", "range": "10d", "events": "history"},
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("chart", {}).get("result")
        if result and len(result) > 0:
            chart = result[0]
            timestamps = chart.get("timestamp") or []
            closes = ((chart.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            today = datetime.now(UTC).date()
            completed = [
                (datetime.fromtimestamp(timestamp, UTC).date(), Decimal(str(close)))
                for timestamp, close in zip(timestamps, closes, strict=False)
                if close is not None and datetime.fromtimestamp(timestamp, UTC).date() < today
            ]
            if completed:
                return max(completed, key=lambda item: item[0])
    except Exception:
        pass
    return None


async def _fetch_all_amfi_navs(
    client: httpx.AsyncClient,
) -> dict[str, tuple[date, Decimal]]:
    navs: dict[str, tuple[date, Decimal]] = {}
    try:
        response = await client.get("https://portal.amfiindia.com/spages/NAVAll.txt")
        response.raise_for_status()
        for line in response.text.splitlines():
            fields = line.split(";")
            if len(fields) < 6:
                continue
            code = fields[0].strip()
            if not code.isdigit():
                continue
            try:
                nav = Decimal(fields[4].strip())
                nav_date = datetime.strptime(fields[5].strip(), "%d-%b-%Y").date()
                navs[code] = (nav_date, nav)
            except (ValueError, ArithmeticError):
                continue
    except httpx.HTTPError:
        return {}
    return navs


def _get_amfi_nav(
    scheme_code: str, navs: dict[str, tuple[date, Decimal]]
) -> tuple[date, Decimal] | None:
    code = scheme_code.strip()
    if not code.isdigit():
        return None
    return navs.get(code)


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
    ):
        self.holding_repo = holding_repo
        self.cash_repo = cash_repo
        self.holding_price_repo = holding_price_repo
        self.snapshot_repo = snapshot_repo
        self.finance_setting_repo = finance_setting_repo
        self.fx_rate_repo = fx_rate_repo
        self.instrument_repo = instrument_repo

    async def refresh_workspace_prices(self, workspace_id: int) -> dict[str, Decimal]:
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
        if not holdings:
            return {}

        today = datetime.now(UTC).date()
        expected_close_date = _previous_weekday(today)
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
                await _fetch_all_amfi_navs(client)
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
                        return _get_amfi_nav(sym, amfi_navs)
                    return await _fetch_stock_price(client, sym, curr)

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
                as_of=datetime.combine(snapshot_date, datetime.max.time(), UTC),
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

        gain, pct = _value_change(snapshot.holdings_value, snapshot.total_cost)
        return PerformanceSummaryResponse(
            total_value=snapshot.holdings_value,
            total_cost=snapshot.total_cost,
            portfolio_value=snapshot.holdings_value,
            invested_value=snapshot.total_cost,
            cash_total=snapshot.cash_value,
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


class InstrumentService:
    def __init__(
        self,
        instrument_repo: InstrumentRepository,
        company_repo: CompanyRepository,
    ):
        self.instrument_repo = instrument_repo
        self.company_repo = company_repo

    async def list_instruments(self, workspace_id: int) -> Sequence[Instrument]:
        return await self.instrument_repo.list_workspace(workspace_id)

    async def create_instrument(self, workspace_id: int, payload: InstrumentCreate) -> Instrument:
        target_workspace_id = workspace_id
        try:
            async with httpx.AsyncClient() as client:
                price_info = await _fetch_stock_price(client, payload.symbol)
                if price_info is not None:
                    target_workspace_id = None
        except Exception:
            # Fallback to workspace-scoped instrument on external API failure
            pass

        existing = await self.instrument_repo.get_by_symbol(target_workspace_id, payload.symbol)
        if existing is not None:
            raise ConflictError(detail=f"Instrument '{payload.symbol}' already exists")

        company_id: int | None = None
        if payload.instrument_type == InstrumentType.stock:
            company = await self.company_repo.get_by_name(target_workspace_id, payload.name)
            if company is None:
                company = await self.company_repo.create(
                    Company(
                        workspace_id=target_workspace_id,
                        name=payload.name,
                        ticker=payload.ticker or payload.symbol,
                    )
                )
            company_id = company.id

        return await self.instrument_repo.create(
            Instrument(
                workspace_id=target_workspace_id,
                symbol=payload.symbol,
                name=payload.name,
                instrument_type=payload.instrument_type.value,
                company_id=company_id,
            )
        )

    async def find_or_create_instrument(
        self,
        workspace_id: int,
        symbol: str,
        instrument_type: InstrumentType,
        instrument_name: str | None = None,
    ) -> Instrument | None:
        instrument = await self.instrument_repo.get_by_symbol(workspace_id, symbol)
        if instrument is not None:
            if instrument_name and instrument.name == instrument.symbol:
                instrument.name = instrument_name
                instrument.updated_at = datetime.now(UTC)
                await self.instrument_repo.save(instrument)
            return instrument

        display_name = instrument_name or symbol

        target_workspace_id = workspace_id
        try:
            async with httpx.AsyncClient() as client:
                price_info = await _fetch_stock_price(client, symbol)
                if price_info is not None:
                    target_workspace_id = None
        except Exception:
            pass

        company: Company | None = None
        if instrument_type == InstrumentType.stock:
            company = await self.company_repo.get_by_name(target_workspace_id, display_name)
            if company is None:
                company = await self.company_repo.create(
                    Company(workspace_id=target_workspace_id, name=display_name, ticker=symbol)
                )

        return await self.instrument_repo.create(
            Instrument(
                workspace_id=target_workspace_id,
                symbol=symbol,
                name=display_name,
                instrument_type=instrument_type.value,
                company_id=company.id if company else None,
            )
        )

    async def update_instrument(
        self, workspace_id: int, public_id: uuid.UUID, payload: InstrumentUpdate
    ) -> Instrument:
        instrument = await self.instrument_repo.get_by_public_id(workspace_id, public_id)
        if instrument is None:
            raise NotFoundError(detail=f"Instrument '{public_id}' not found")

        if instrument.workspace_id != workspace_id:
            raise ForbiddenError(detail="Cannot modify a global instrument reference")

        update_data = payload.model_dump(exclude_unset=True)
        if not update_data:
            return instrument

        if payload.name is not None:
            instrument.name = payload.name
        if payload.instrument_type is not None:
            instrument.instrument_type = payload.instrument_type.value
            if payload.instrument_type == InstrumentType.stock and instrument.company_id is None:
                company = await self.company_repo.get_by_name(workspace_id, instrument.name)
                if company is None:
                    company = await self.company_repo.create(
                        Company(
                            workspace_id=workspace_id,
                            name=instrument.name,
                            ticker=instrument.symbol,
                        )
                    )
                instrument.company_id = company.id
            if payload.instrument_type != InstrumentType.stock:
                instrument.company_id = None
        instrument.updated_at = datetime.now(UTC)
        return await self.instrument_repo.save(instrument)


class ConstituentService:
    def __init__(
        self,
        instrument_repo: InstrumentRepository,
        company_repo: CompanyRepository,
        constituent_repo: InstrumentConstituentRepository,
    ):
        self.instrument_repo = instrument_repo
        self.company_repo = company_repo
        self.constituent_repo = constituent_repo

    async def upsert_constituents(
        self,
        workspace_id: int,
        instrument_public_id: uuid.UUID,
        payload: InstrumentConstituentUpsert,
    ) -> Sequence[InstrumentConstituent]:
        instrument = await self.instrument_repo.get_by_public_id(workspace_id, instrument_public_id)
        if instrument is None:
            raise NotFoundError(detail=f"Instrument '{instrument_public_id}' not found")
        if instrument.instrument_type == InstrumentType.stock.value:
            raise ValidationError(detail="Stock instruments cannot have constituent snapshots")

        constituents = list(payload.constituents)
        total_weight = sum(item.weight for item in constituents)
        if payload.renormalise:
            if total_weight <= 0:
                raise ValidationError(detail="Constituent weights must be positive")
            for item in constituents:
                item.weight = (item.weight / total_weight).quantize(Decimal("0.00000001"))
            total_weight = sum(item.weight for item in constituents)

        if not (Decimal("0.99") <= total_weight <= Decimal("1.01")):
            raise ValidationError(
                detail=f"Constituent weights must sum to approximately 1.0 (got {total_weight})"
            )

        await self.constituent_repo.delete_snapshot(
            instrument.id, payload.as_of_date, payload.source
        )  # type: ignore[arg-type]
        rows: list[InstrumentConstituent] = []
        requested_names = [item.company_name for item in constituents]
        companies_by_name = await self.company_repo.get_by_names(workspace_id, requested_names)
        for item in constituents:
            company = companies_by_name.get(item.company_name)
            if company is None:
                company = await self.company_repo.create(
                    Company(
                        workspace_id=workspace_id,
                        name=item.company_name,
                        ticker=item.company_ticker,
                    )
                )
                companies_by_name[item.company_name] = company
            rows.append(
                InstrumentConstituent(
                    instrument_id=instrument.id,  # type: ignore[arg-type]
                    constituent_company_id=company.id,  # type: ignore[arg-type]
                    weight=item.weight,
                    as_of_date=payload.as_of_date,
                    source=payload.source,
                    fetched_at=payload.fetched_at,
                )
            )
        return await self.constituent_repo.create_many(rows)

    async def get_constituents(
        self, workspace_id: int, instrument_public_id: uuid.UUID, as_of: date
    ) -> list[InstrumentConstituentResponse]:
        instrument = await self.instrument_repo.get_by_public_id(workspace_id, instrument_public_id)
        if instrument is None:
            raise NotFoundError(detail=f"Instrument '{instrument_public_id}' not found")
        rows = await self.constituent_repo.list_snapshot(instrument.id, as_of)  # type: ignore[arg-type]
        if not rows:
            rows = await self.constituent_repo.get_latest_on_or_before(instrument.id, as_of)  # type: ignore[arg-type]

        companies_by_id = await self.company_repo.get_by_ids([
            row.constituent_company_id for row in rows
        ])
        out: list[InstrumentConstituentResponse] = []
        for row in rows:
            company = companies_by_id.get(row.constituent_company_id)
            if company is None:
                continue
            out.append(
                InstrumentConstituentResponse(
                    company_id=company.public_id,
                    company_name=company.name,
                    company_ticker=company.ticker,
                    weight=row.weight,
                    as_of_date=row.as_of_date,
                    source=row.source,
                )
            )
        return out


class ExposureAnalyticsService:
    def __init__(
        self,
        holding_repo: HoldingRepository,
        instrument_repo: InstrumentRepository,
        company_repo: CompanyRepository,
        constituent_repo: InstrumentConstituentRepository,
        finance_setting_repo: FinanceSettingRepository | None = None,
        fx_rate_repo: FxRateRepository | None = None,
        staleness_window_days: int = 30,
    ):
        self.holding_repo = holding_repo
        self.instrument_repo = instrument_repo
        self.company_repo = company_repo
        self.constituent_repo = constituent_repo
        self.finance_setting_repo = finance_setting_repo
        self.fx_rate_repo = fx_rate_repo
        self.staleness_window_days = staleness_window_days

    async def exposure(
        self, workspace_id: int, as_of: date, *, apply_display_threshold: bool = True
    ) -> ExposureAnalyticsResponse:
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
        used_currencies = sorted({holding.currency.upper() for holding in holdings})
        reporting_currency: str | None = None
        display_threshold_pct = settings.LOOKTHROUGH_MIN_DISPLAY_WEIGHT_PCT
        if self.finance_setting_repo is not None:
            workspace_settings = await self.finance_setting_repo.get_by_workspace(workspace_id)
            if workspace_settings:
                display_threshold_pct = getattr(
                    workspace_settings,
                    "lookthrough_min_weight_pct",
                    settings.LOOKTHROUGH_MIN_DISPLAY_WEIGHT_PCT,
                )
                if workspace_settings.reporting_currency_code:
                    reporting_currency = workspace_settings.reporting_currency_code.upper()
        if reporting_currency is None and len(used_currencies) == 1:
            reporting_currency = used_currencies[0]
        if reporting_currency is None and len(used_currencies) > 1:
            warning = "Reporting currency is required for multi-currency look-through analytics"
            return ExposureAnalyticsResponse(
                as_of_date=as_of,
                analysis_status="unavailable",
                currency=None,
                snapshot_coverage=Decimal("0"),
                staleness_days=self.staleness_window_days,
                warnings=[warning],
                display_threshold_pct=display_threshold_pct,
                exposure=[],
                total_direct_exposure=None,
                total_lookthrough_exposure=None,
            )

        fx_lookup: dict[tuple[str, str], FxRate] = {}
        fx_as_of: datetime | None = None
        if (
            reporting_currency
            and any(curr != reporting_currency for curr in used_currencies)
            and self.fx_rate_repo is not None
        ):
            required_pairs = _build_required_pairs(used_currencies, reporting_currency)
            fx_lookup = await self.fx_rate_repo.get_latest_rates_for_pairs(
                list(required_pairs),
                datetime.combine(as_of, datetime.max.time(), tzinfo=UTC),
            )
            if fx_lookup:
                fx_as_of = max(rate.as_of for rate in fx_lookup.values())

        direct: dict[int, Decimal] = {}
        lookthrough: dict[int, Decimal] = {}
        warnings: list[str] = []
        decomposable = 0
        decomposed = 0
        instruments_by_id = await self.instrument_repo.get_by_ids([
            h.instrument_id for h in holdings if h.instrument_id is not None
        ])
        symbols = [h.symbol for h in holdings if h.instrument_id is None]
        instruments_by_symbol = await self.instrument_repo.get_by_symbols(workspace_id, symbols)
        pooled_instrument_ids = [
            inst.id
            for inst in list(instruments_by_id.values()) + list(instruments_by_symbol.values())
            if inst.id is not None and inst.instrument_type != InstrumentType.stock.value
        ]
        constituents_by_instrument = await self.constituent_repo.get_latest_on_or_before_many(
            pooled_instrument_ids, as_of
        )

        for h in holdings:
            native_value = h.quantity * h.avg_cost
            value = (
                _convert_amount(
                    native_value,
                    h.currency.upper(),
                    reporting_currency,
                    fx_lookup,
                )
                if reporting_currency
                else native_value
            )
            if value is None:
                warnings.append(
                    f"FX rate from {h.currency.upper()} to {reporting_currency} is required "
                    f"for {h.symbol}"
                )
                continue
            instrument = None
            if h.instrument_id is not None:
                instrument = instruments_by_id.get(h.instrument_id)
            if instrument is None:
                instrument = instruments_by_symbol.get(h.symbol)
            if instrument is None:
                warnings.append(f"Instrument missing for symbol {h.symbol}")
                continue

            if instrument.instrument_type == InstrumentType.stock.value:
                if instrument.company_id is None:
                    warnings.append(
                        f"Stock instrument {instrument.symbol} is not linked to a company"
                    )
                    continue
                direct[instrument.company_id] = (
                    direct.get(instrument.company_id, Decimal("0")) + value
                )
                lookthrough[instrument.company_id] = (
                    lookthrough.get(instrument.company_id, Decimal("0")) + value
                )
                continue

            decomposable += 1
            rows = constituents_by_instrument.get(instrument.id, [])  # type: ignore[arg-type]
            if not rows:
                warnings.append(f"No constituent snapshot for {instrument.symbol}")
                continue
            snapshot_date = rows[0].as_of_date
            if (as_of - snapshot_date).days > self.staleness_window_days:
                warnings.append(f"Stale constituent snapshot for {instrument.symbol}")
                continue
            decomposed += 1
            for row in rows:
                lookthrough[row.constituent_company_id] = lookthrough.get(
                    row.constituent_company_id, Decimal("0")
                ) + (value * row.weight)

        company_ids = sorted(set(direct.keys()) | set(lookthrough.keys()))
        companies_by_id = await self.company_repo.get_by_ids(company_ids)
        rows: list[ExposureCompanyRow] = []
        for cid in company_ids:
            company = companies_by_id.get(cid)
            if company is None:
                continue
            rows.append(
                ExposureCompanyRow(
                    company_id=company.public_id,
                    company_name=company.name,
                    company_ticker=company.ticker,
                    direct_exposure=direct.get(cid, Decimal("0")),
                    lookthrough_exposure=lookthrough.get(cid, Decimal("0")),
                )
            )

        coverage = Decimal("1")
        if decomposable > 0:
            coverage = Decimal(decomposed) / Decimal(decomposable)
        analysis_status = "complete" if not warnings else "partial"
        total_direct = sum((r.direct_exposure for r in rows), Decimal("0"))
        total_lookthrough = sum((r.lookthrough_exposure for r in rows), Decimal("0"))
        visible_rows = rows
        if apply_display_threshold and total_lookthrough > 0:
            minimum_share = display_threshold_pct / Decimal("100")
            visible_rows = [
                row for row in rows if row.lookthrough_exposure / total_lookthrough >= minimum_share
            ]
        return ExposureAnalyticsResponse(
            as_of_date=as_of,
            analysis_status=analysis_status,
            currency=reporting_currency,
            fx_as_of=fx_as_of,
            fx_rates_used={
                key: Decimal(value)
                for key, value in _fx_rates_used(
                    used_currencies, reporting_currency, fx_lookup
                ).items()
            }
            if reporting_currency
            else {},
            snapshot_coverage=coverage,
            staleness_days=self.staleness_window_days,
            warnings=warnings,
            display_threshold_pct=display_threshold_pct,
            hidden_exposure_count=len(rows) - len(visible_rows),
            exposure=visible_rows,
            total_direct_exposure=total_direct,
            total_lookthrough_exposure=total_lookthrough,
        )

    async def overlap(self, workspace_id: int, as_of: date) -> OverlapAnalyticsResponse:
        exposure = await self.exposure(workspace_id, as_of, apply_display_threshold=False)
        sorted_rows = sorted(exposure.exposure, key=lambda r: r.lookthrough_exposure, reverse=True)
        total = sum((r.lookthrough_exposure for r in sorted_rows), Decimal("0"))

        overlaps: list[OverlapRow] = []
        for row in sorted_rows:
            share = Decimal("0")
            if total > 0:
                share = row.lookthrough_exposure / total
            overlaps.append(
                OverlapRow(
                    company_id=row.company_id,
                    company_name=row.company_name,
                    company_ticker=row.company_ticker,
                    overlap_exposure=row.lookthrough_exposure,
                    portfolio_share=share,
                )
            )

        top5 = sum((r.portfolio_share for r in overlaps[:5]), Decimal("0"))
        top10 = sum((r.portfolio_share for r in overlaps[:10]), Decimal("0"))
        duplicate = sum(
            ((row.lookthrough_exposure - row.direct_exposure) for row in exposure.exposure),
            Decimal("0"),
        )
        duplicate = duplicate / total if total > 0 else Decimal("0")

        minimum_share = exposure.display_threshold_pct / Decimal("100")
        visible_overlaps = [row for row in overlaps if row.portfolio_share >= minimum_share]
        return OverlapAnalyticsResponse(
            as_of_date=as_of,
            analysis_status=exposure.analysis_status,
            currency=exposure.currency,
            fx_as_of=exposure.fx_as_of,
            fx_rates_used=exposure.fx_rates_used,
            snapshot_coverage=exposure.snapshot_coverage,
            warnings=exposure.warnings,
            display_threshold_pct=exposure.display_threshold_pct,
            hidden_overlap_count=len(overlaps) - len(visible_overlaps),
            top_5_concentration_pct=top5,
            top_10_concentration_pct=top10,
            duplicate_exposure_index=duplicate,
            overlaps=visible_overlaps,
        )


class InvestingSummaryService:
    def __init__(
        self,
        holding_repo: HoldingRepository,
        cash_repo: CashBalanceRepository,
        finance_setting_repo: FinanceSettingRepository | None = None,
        fx_rate_repo: FxRateRepository | None = None,
        holding_price_repo: HoldingPriceRepository | None = None,
        snapshot_repo: PortfolioSnapshotRepository | None = None,
    ):
        self.holding_repo = holding_repo
        self.cash_repo = cash_repo
        self.finance_setting_repo = finance_setting_repo
        self.fx_rate_repo = fx_rate_repo
        self.holding_price_repo = holding_price_repo
        self.snapshot_repo = snapshot_repo

    async def get_summary(self, workspace_id: int) -> InvestingSummaryResponse:
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
        cash_balances = await self.cash_repo.get_latest_per_account_currency(workspace_id)

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
                cash_total = Decimal("0")
                for holding in holdings:
                    portfolio_value += holding_values[holding.id]
                for cash in cash_balances:
                    cash_total += cash.balance

                # Calculate daily change
                daily_change = None
                if self.snapshot_repo is not None:
                    prev_snapshot = await self.snapshot_repo.latest_before(workspace_id, today)
                    if prev_snapshot is not None:
                        daily_change = portfolio_value - prev_snapshot.total_value

                return InvestingSummaryResponse(
                    portfolio_value=portfolio_value,
                    holdings_count=len(holdings),
                    cash_total=cash_total,
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

            valuation_as_of = datetime.now(UTC)
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

            converted_cash = Decimal("0")
            for cash in cash_balances:
                curr = cash.currency.upper()
                converted_value = _convert_amount(cash.balance, curr, reporting_currency, fx_lookup)
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
                converted_cash += converted_value

            # portfolio_value is holdings-only (cash is reported separately as
            # cash_total and added by net worth) — consistent with the
            # single-currency paths. Including cash here made net worth
            # double-count cash, since the net-worth router adds cash_total to
            # portfolio_value. total_value (holdings + cash) is used only for the
            # daily-change comparison, since snapshots store
            # total_value = holdings_value + cash_value.
            total_value = converted_portfolio + converted_cash
            daily_change = None
            if self.snapshot_repo is not None:
                prev_snapshot = await self.snapshot_repo.latest_before(workspace_id, today)
                if prev_snapshot is not None:
                    daily_change = total_value - prev_snapshot.total_value

            return InvestingSummaryResponse(
                portfolio_value=converted_portfolio,
                holdings_count=len(holdings),
                cash_total=converted_cash,
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
        cash_total = Decimal("0")
        for holding in holdings:
            portfolio_value += holding_values[holding.id]
        for cash in cash_balances:
            cash_total += cash.balance

        # Calculate daily change
        daily_change = None
        if self.snapshot_repo is not None:
            prev_snapshot = await self.snapshot_repo.latest_before(workspace_id, today)
            if prev_snapshot is not None:
                daily_change = portfolio_value - prev_snapshot.total_value

        return InvestingSummaryResponse(
            portfolio_value=portfolio_value,
            holdings_count=len(holdings),
            cash_total=cash_total,
            currency_breakdown=breakdown,
            daily_change=daily_change,
            reporting_currency=reporting_currency,
            valuation_status="cost_basis_fallback"
            if used_cost_basis_fallback
            else "converted_available",
            fx_as_of=None,
        )


AVG_COST_PRECISION = Decimal("0.000001")


def _order_total_fees(order: InvestingOrder) -> Decimal:
    """Sum of all transaction costs recorded on an order.

    The fee columns are non-nullable with a 0 default, but guard against
    ``None`` defensively so a malformed/legacy row can't raise here.
    """
    return (
        (order.brokerage_fee or Decimal("0"))
        + (order.tax_amount or Decimal("0"))
        + (order.other_fees or Decimal("0"))
    )


def _effective_buy_cost_per_unit(order: InvestingOrder) -> Decimal:
    """Per-unit cost basis of a buy, with fees capitalized into the lot.

    ``cost = price_per_unit + total_fees / quantity`` — the broker/tax
    convention of folding buy-side brokerage, tax, and other fees into the
    asset's cost of acquisition (Section 48, Income-tax Act 1961). See
    spec-046. Sell-side fees are handled separately (they reduce realized
    proceeds, not the cost of units sold).
    """
    fees = _order_total_fees(order)
    if fees == 0 or order.quantity == 0:
        return order.price_per_unit
    return (order.price_per_unit + fees / order.quantity).quantize(AVG_COST_PRECISION)


@dataclass
class _OpenLot:
    """In-memory FIFO lot state during a ``_replay_orders`` pass."""

    buy_order_id: int
    original_quantity: Decimal
    remaining: Decimal
    cost_per_unit: Decimal
    acquired_at: datetime


@dataclass
class _LotConsumptionEvent:
    sell_order_id: int
    buy_order_id: int
    quantity_consumed: Decimal
    cost_per_unit: Decimal


@dataclass
class _ReplayResult:
    quantity: Decimal
    avg_cost: Decimal
    lots: list[_OpenLot] = field(default_factory=list)
    consumptions: list[_LotConsumptionEvent] = field(default_factory=list)


class InvestingOrderService:
    def __init__(
        self,
        order_repository: InvestingOrderRepository,
        holding_repository: HoldingRepository,
        cash_balance_repository: CashBalanceRepository,
        account_repository: AccountRepository,
        currency_repository: CurrencyRepository,
        instrument_service: InstrumentService,
        lot_repository: LotRepository,
    ):
        self.order_repository = order_repository
        self.holding_repository = holding_repository
        self.cash_balance_repository = cash_balance_repository
        self.account_repository = account_repository
        self.currency_repository = currency_repository
        self.instrument_service = instrument_service
        self.lot_repository = lot_repository

    async def _validate_brokerage_account(
        self, workspace_id: int, account_public_id: uuid.UUID
    ) -> "Account":
        account = await self.account_repository.get_by_public_id(workspace_id, account_public_id)
        if not account or not account.is_active:
            raise NotFoundError(
                detail=f"Account with id {account_public_id} not found in this workspace"
            )
        if account.account_type != "brokerage":
            raise ValidationError(
                detail=(
                    f"Orders can only be placed against brokerage accounts. "
                    f"Account '{account.name}' is type '{account.account_type}'"
                )
            )
        return account

    async def _get_cash_balance(self, workspace_id: int, account_id: int, currency: str) -> Decimal:
        latest = await self.cash_balance_repository.get_latest_for_account_currency(
            workspace_id, account_id, currency
        )
        return latest.balance if latest is not None else Decimal("0")

    async def _update_cash_balance(
        self,
        workspace_id: int,
        user_id: int,
        account_id: int,
        currency: str,
        delta: Decimal,
        trigger_type: str,
        trigger_ref: uuid.UUID,
    ) -> CashBalance:
        current = await self._get_cash_balance(workspace_id, account_id, currency)
        new_balance = current + delta
        cash = CashBalance(
            workspace_id=workspace_id,
            user_id=user_id,
            account_id=account_id,
            balance=new_balance,
            currency=currency,
            as_of=datetime.now(UTC),
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
        )
        return await self.cash_balance_repository.create(cash)

    def _replay_orders(
        self, orders: Sequence[InvestingOrder], *, record_sells: bool
    ) -> "_ReplayResult":
        """Replay orders chronologically with a FIFO lot cost-basis model.

        A sell is matched against the *oldest* open buy lot(s) first (FIFO),
        per Section 45(2A) of the Income-tax Act, 1961 and CBDT Circular 768
        — see spec-044. Returns the resulting position plus the lot/consumption
        state needed to persist ``OrderLot``/``LotConsumption`` rows. When
        ``record_sells`` is True, each sell order's ``realized_gain_loss`` and
        ``avg_cost_at_sale`` are updated in place against the lots it consumed.
        Raises ``ValidationError`` if a sell would exceed open lots at any
        intermediate point, not just in the final aggregate.
        """
        # `queue` drives FIFO consumption order; `all_lots` retains every lot
        # (including fully-consumed ones) so their final remaining_quantity=0
        # state and consumption history are still persisted.
        queue: deque[_OpenLot] = deque()
        all_lots: dict[int, _OpenLot] = {}
        consumptions: list[_LotConsumptionEvent] = []

        for order in orders:
            if order.order_type == "buy":
                assert order.id is not None
                lot = _OpenLot(
                    buy_order_id=order.id,
                    original_quantity=order.quantity,
                    remaining=order.quantity,
                    cost_per_unit=_effective_buy_cost_per_unit(order),
                    acquired_at=order.occurred_at,
                )
                queue.append(lot)
                all_lots[order.id] = lot
            else:
                assert order.id is not None
                to_consume = order.quantity
                realized = Decimal("0")
                cost_consumed = Decimal("0")
                while to_consume > 0:
                    if not queue:
                        raise ValidationError(
                            detail=(
                                "This order would result in a negative holding "
                                "(a sell would exceed shares held at that point in time)"
                            )
                        )
                    lot = queue[0]
                    take = min(lot.remaining, to_consume)
                    realized += take * (order.price_per_unit - lot.cost_per_unit)
                    cost_consumed += take * lot.cost_per_unit
                    consumptions.append(
                        _LotConsumptionEvent(
                            sell_order_id=order.id,
                            buy_order_id=lot.buy_order_id,
                            quantity_consumed=take,
                            cost_per_unit=lot.cost_per_unit,
                        )
                    )
                    lot.remaining -= take
                    to_consume -= take
                    if lot.remaining == 0:
                        queue.popleft()
                if record_sells:
                    # Sell-side fees reduce realized proceeds (they are not
                    # added to the cost of the units sold). avg_cost_at_sale
                    # stays the fee-inclusive buy cost of the consumed lots.
                    realized -= _order_total_fees(order)
                    order.realized_gain_loss = realized.quantize(MONEY_QUANT)
                    order.avg_cost_at_sale = (cost_consumed / order.quantity).quantize(
                        AVG_COST_PRECISION
                    )

        final_qty = sum((lot.remaining for lot in queue), Decimal("0"))
        final_avg_cost = (
            (
                sum((lot.remaining * lot.cost_per_unit for lot in queue), Decimal("0")) / final_qty
            ).quantize(AVG_COST_PRECISION)
            if final_qty > 0
            else Decimal("0")
        )
        return _ReplayResult(
            quantity=final_qty,
            avg_cost=final_avg_cost,
            lots=list(all_lots.values()),
            consumptions=consumptions,
        )

    async def _recompute_holding_from_orders(
        self, workspace_id: int, user_id: int, symbol: str, account_id: int
    ) -> Holding | None:
        orders = await self.order_repository.list_by_holding(workspace_id, symbol, account_id)
        holding = await self.holding_repository.get_by_unique_key(workspace_id, symbol, account_id)

        if not orders:
            # No orders remain; delete the holding if it exists
            if holding is not None:
                await self.holding_repository.delete(holding)
            return None

        if holding is not None and holding.id is not None:
            await self.lot_repository.delete_for_holding(holding.id)

        result = self._replay_orders(orders, record_sells=True)

        # `orders` were loaded via this same session (list_by_holding), so the
        # in-place realized_gain_loss/avg_cost_at_sale mutations above are
        # already tracked as dirty and will be persisted on the next flush
        # (triggered below by the holding/lot repository calls) — no explicit
        # per-order save() needed.

        if holding is None:
            instrument_id = next((o.instrument_id for o in orders if o.instrument_id), None)
            holding = await self.holding_repository.create(
                Holding(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    symbol=symbol,
                    account_id=account_id,
                    quantity=result.quantity,
                    avg_cost=result.avg_cost,
                    currency=orders[0].currency,
                    source_type="order",
                    instrument_id=instrument_id,
                )
            )
        else:
            holding.quantity = result.quantity
            holding.avg_cost = result.avg_cost
            holding.updated_at = datetime.now(UTC)
            holding = await self.holding_repository.save(holding)

        assert holding.id is not None
        if result.lots:
            created_lots = await self.lot_repository.create_lots([
                OrderLot(
                    workspace_id=workspace_id,
                    holding_id=holding.id,
                    buy_order_id=lot.buy_order_id,
                    original_quantity=lot.original_quantity,
                    remaining_quantity=lot.remaining,
                    cost_per_unit=lot.cost_per_unit,
                    acquired_at=lot.acquired_at,
                )
                for lot in result.lots
            ])
            if result.consumptions:
                lot_id_by_buy_order = {lot.buy_order_id: lot.id for lot in created_lots}
                await self.lot_repository.create_consumptions([
                    LotConsumption(
                        sell_order_id=ev.sell_order_id,
                        lot_id=lot_id_by_buy_order[ev.buy_order_id],
                        quantity_consumed=ev.quantity_consumed,
                        cost_per_unit=ev.cost_per_unit,
                    )
                    for ev in result.consumptions
                ])
        return holding

    async def place_order(
        self,
        workspace_id: int,
        user_id: int,
        order_in: InvestingOrderCreate,
        audit_logger: AuditLogger | None = None,
        source_type: str = "manual",
    ) -> InvestingOrder:
        account = await self._validate_brokerage_account(workspace_id, order_in.account_id)
        assert account.id is not None

        gross_amount = order_in.quantity * order_in.price_per_unit
        total_fees = order_in.brokerage_fee + order_in.tax_amount + order_in.other_fees

        if order_in.order_type == OrderType.buy:
            net_amount = gross_amount + total_fees
        else:
            net_amount = gross_amount - total_fees

        if order_in.order_type == OrderType.buy:
            available_cash = await self._get_cash_balance(
                workspace_id, account.id, order_in.currency
            )
            if available_cash < net_amount:
                raise ValidationError(
                    detail=(
                        f"Insufficient cash balance. "
                        f"Available: {order_in.currency} {available_cash:.2f}, "
                        f"Required: {order_in.currency} {net_amount:.2f}"
                    )
                )

        holding = await self.holding_repository.get_by_unique_key(
            workspace_id, order_in.symbol, account.id
        )

        if order_in.order_type == OrderType.sell and (
            holding is None or holding.quantity < order_in.quantity
        ):
            current_qty = holding.quantity if holding else Decimal("0")
            raise ValidationError(
                detail=(
                    f"Cannot sell {order_in.quantity} shares of {order_in.symbol}. "
                    f"Current holding: {current_qty} shares"
                )
            )

        instrument = await self.instrument_service.find_or_create_instrument(
            workspace_id,
            order_in.symbol,
            order_in.instrument_type,
            order_in.instrument_name,
        )

        order = InvestingOrder(
            workspace_id=workspace_id,
            user_id=user_id,
            account_id=account.id,
            order_type=order_in.order_type.value,
            symbol=order_in.symbol,
            instrument_id=instrument.id if instrument else None,
            quantity=order_in.quantity,
            price_per_unit=order_in.price_per_unit,
            gross_amount=gross_amount.quantize(MONEY_QUANT),
            brokerage_fee=order_in.brokerage_fee,
            tax_amount=order_in.tax_amount,
            other_fees=order_in.other_fees,
            net_amount=net_amount.quantize(MONEY_QUANT),
            currency=order_in.currency,
            exchange_name=order_in.exchange_name,
            occurred_at=order_in.occurred_at,
            notes=order_in.notes,
            source_type=source_type,
        )
        order = await self.order_repository.create(order)

        # Recompute holding quantity, FIFO avg_cost, and (for a sell) this
        # order's own realized_gain_loss/avg_cost_at_sale from the full order
        # history — the single replay path shared with update_order/delete_order.
        # `order` is the same session-tracked instance _recompute_holding_from_orders
        # will load and mutate via the identity map, so no re-fetch is needed.
        await self._recompute_holding_from_orders(
            workspace_id, user_id, order_in.symbol, account.id
        )

        # Update cash balance
        cash_delta = -net_amount if order_in.order_type == OrderType.buy else net_amount
        await self._update_cash_balance(
            workspace_id=workspace_id,
            user_id=user_id,
            account_id=account.id,
            currency=order_in.currency,
            delta=cash_delta,
            trigger_type="order",
            trigger_ref=order.public_id,
        )

        if audit_logger:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action=f"investing_order_{order_in.order_type.value}",
                module="investing",
                entity_type="investing_order",
                entity_id=order.id or 0,
                details={
                    "entity_public_id": str(order.public_id),
                    "before": None,
                    "after": {
                        "order_type": order_in.order_type.value,
                        "symbol": order_in.symbol,
                        "quantity": str(order_in.quantity),
                        "price_per_unit": str(order_in.price_per_unit),
                        "net_amount": str(net_amount),
                        "currency": order_in.currency,
                    },
                    "changed_fields": ["quantity", "avg_cost", "cash_balance"],
                },
            )

        return order

    async def delete_order(
        self,
        workspace_id: int,
        user_id: int,
        order_public_id: uuid.UUID,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        order = await self.order_repository.get_by_public_id(workspace_id, order_public_id)
        if not order:
            raise NotFoundError(
                detail=f"Order with id {order_public_id} not found in this workspace"
            )

        # Replay the remaining orders chronologically to ensure deleting this one
        # never drives the holding negative at any point in time (not just in the
        # final aggregate).
        remaining_orders = [
            o
            for o in await self.order_repository.list_by_holding(
                workspace_id, order.symbol, order.account_id
            )
            if o.public_id != order.public_id
        ]
        self._replay_orders(remaining_orders, record_sells=False)

        # Reverse the cash balance impact
        cash_delta = order.net_amount if order.order_type == "buy" else -order.net_amount
        await self._update_cash_balance(
            workspace_id=workspace_id,
            user_id=user_id,
            account_id=order.account_id,
            currency=order.currency,
            delta=cash_delta,
            trigger_type="order",
            trigger_ref=order.public_id,
        )

        await self.order_repository.delete(order)

        # Recompute holding from remaining orders
        await self._recompute_holding_from_orders(
            workspace_id, user_id, order.symbol, order.account_id
        )

        if audit_logger:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="investing_order_deleted",
                module="investing",
                entity_type="investing_order",
                entity_id=order.id or 0,
                details={
                    "entity_public_id": str(order.public_id),
                    "before": {
                        "order_type": order.order_type,
                        "symbol": order.symbol,
                        "quantity": str(order.quantity),
                    },
                    "after": None,
                    "changed_fields": [],
                },
            )

    async def update_order(
        self,
        workspace_id: int,
        user_id: int,
        order_public_id: uuid.UUID,
        order_update: InvestingOrderUpdate,
        audit_logger: AuditLogger | None = None,
    ) -> InvestingOrder:
        order = await self.order_repository.get_by_public_id(workspace_id, order_public_id)
        if not order:
            raise NotFoundError(
                detail=f"Order with id {order_public_id} not found in this workspace"
            )
        before_snap = _snapshot_order(order)

        # Resolve updated values (None means keep existing)
        new_order_type = (
            order_update.order_type.value
            if order_update.order_type is not None
            else order.order_type
        )
        new_quantity = (
            order_update.quantity if order_update.quantity is not None else order.quantity
        )
        new_price = (
            order_update.price_per_unit
            if order_update.price_per_unit is not None
            else order.price_per_unit
        )
        new_brokerage_fee = (
            order_update.brokerage_fee
            if order_update.brokerage_fee is not None
            else order.brokerage_fee
        )
        new_tax_amount = (
            order_update.tax_amount if order_update.tax_amount is not None else order.tax_amount
        )
        new_other_fees = (
            order_update.other_fees if order_update.other_fees is not None else order.other_fees
        )
        new_occurred_at = (
            order_update.occurred_at if order_update.occurred_at is not None else order.occurred_at
        )

        new_gross = new_quantity * new_price
        new_total_fees = new_brokerage_fee + new_tax_amount + new_other_fees
        new_net = (
            new_gross + new_total_fees if new_order_type == "buy" else new_gross - new_total_fees
        ).quantize(MONEY_QUANT)

        # Validate replay with the updated order substituted in place before touching DB
        all_orders = await self.order_repository.list_by_holding(
            workspace_id, order.symbol, order.account_id
        )
        simulated_orders: list[InvestingOrder] = []
        for o in all_orders:
            if o.id == order.id:
                # Replace with in-memory copy reflecting proposed edits
                sim = InvestingOrder(
                    **{col: getattr(o, col) for col in o.model_fields},
                )
                sim.order_type = new_order_type
                sim.quantity = new_quantity
                sim.price_per_unit = new_price
                sim.occurred_at = new_occurred_at
                simulated_orders.append(sim)
            else:
                simulated_orders.append(o)
        # Re-sort by occurred_at since the date may have changed
        simulated_orders.sort(key=lambda o: (o.occurred_at, o.id or 0))
        self._replay_orders(simulated_orders, record_sells=False)

        # Reverse old cash impact
        old_cash_reversal = order.net_amount if order.order_type == "buy" else -order.net_amount
        await self._update_cash_balance(
            workspace_id=workspace_id,
            user_id=user_id,
            account_id=order.account_id,
            currency=order.currency,
            delta=old_cash_reversal,
            trigger_type="order",
            trigger_ref=order.public_id,
        )

        # For buy orders validate sufficient cash after reversal
        if new_order_type == "buy":
            available_cash = await self._get_cash_balance(
                workspace_id, order.account_id, order.currency
            )
            if available_cash < new_net:
                raise ValidationError(
                    detail=(
                        f"Insufficient cash balance. "
                        f"Available: {order.currency} {available_cash:.2f}, "
                        f"Required: {order.currency} {new_net:.2f}"
                    )
                )

        # Persist updated order fields
        order.order_type = new_order_type
        order.quantity = new_quantity
        order.price_per_unit = new_price
        order.brokerage_fee = new_brokerage_fee
        order.tax_amount = new_tax_amount
        order.other_fees = new_other_fees
        order.gross_amount = new_gross.quantize(MONEY_QUANT)
        order.net_amount = new_net
        order.occurred_at = new_occurred_at
        if order_update.exchange_name is not None:
            order.exchange_name = order_update.exchange_name
        if "notes" in order_update.model_fields_set:
            order.notes = order_update.notes
        order.updated_at = datetime.now(UTC)
        order = await self.order_repository.save(order)

        # Apply new cash impact
        new_cash_delta = -new_net if new_order_type == "buy" else new_net
        await self._update_cash_balance(
            workspace_id=workspace_id,
            user_id=user_id,
            account_id=order.account_id,
            currency=order.currency,
            delta=new_cash_delta,
            trigger_type="order",
            trigger_ref=order.public_id,
        )

        # Recompute holding quantity and avg cost from full order history
        await self._recompute_holding_from_orders(
            workspace_id, user_id, order.symbol, order.account_id
        )

        if audit_logger:
            after_snap = _snapshot_order(order)
            changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="investing_order_updated",
                module="investing",
                entity_type="investing_order",
                entity_id=order.id or 0,
                details={
                    "entity_public_id": str(order.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )

        return order

    async def bulk_import_orders(
        self,
        workspace_id: int,
        user_id: int,
        orders: list[InvestingOrderCreate],
        source_import_id: int | None,
        audit_logger: AuditLogger | None = None,
    ) -> list[InvestingOrder]:
        sorted_orders = sorted(orders, key=lambda o: o.occurred_at)
        created = []
        for order_in in sorted_orders:
            order = await self.place_order(
                workspace_id=workspace_id,
                user_id=user_id,
                order_in=order_in,
                audit_logger=audit_logger,
                source_type="csv_import",
            )
            if source_import_id is not None:
                order.source_import_id = source_import_id
                await self.order_repository.create(order)
            created.append(order)
        return created

    async def list_orders(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        symbol: str | None = None,
        account_id: int | None = None,
        order_type: str | None = None,
        search: str | None = None,
    ) -> tuple[Sequence[InvestingOrder], int]:
        return await self.order_repository.list_by_workspace(
            workspace_id, limit, offset, symbol, account_id, order_type, search
        )

    async def get_order(self, workspace_id: int, public_id: uuid.UUID) -> InvestingOrder:
        order = await self.order_repository.get_by_public_id(workspace_id, public_id)
        if not order:
            raise NotFoundError(detail=f"Order with id {public_id} not found in this workspace")
        return order

    async def list_orders_for_holding(
        self, workspace_id: int, symbol: str, account_id: int
    ) -> Sequence[InvestingOrder]:
        return await self.order_repository.list_by_holding(workspace_id, symbol, account_id)
