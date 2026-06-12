import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx

from app.core.audit import AuditLogger
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
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
    PortfolioSnapshot,
)
from app.investing.repository import (
    CashBalanceRepository,
    CompanyRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InstrumentConstituentRepository,
    InstrumentRepository,
    PortfolioSnapshotRepository,
)
from app.investing.schemas import (
    CashBalanceCreate,
    CashBalanceUpdate,
    ExposureAnalyticsResponse,
    ExposureCompanyRow,
    HoldingCreate,
    HoldingPriceBulkCreate,
    HoldingUpdate,
    InstrumentConstituentResponse,
    InstrumentConstituentUpsert,
    InstrumentCreate,
    InvestingSummaryResponse,
    OverlapAnalyticsResponse,
    OverlapRow,
    PerformanceSummaryResponse,
)

MONEY_QUANT = Decimal("0.01")


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


class HoldingService:
    def __init__(
        self,
        repository: HoldingRepository,
        instrument_repo: InstrumentRepository | None = None,
        company_repo: CompanyRepository | None = None,
        account_repo: AccountRepository | None = None,
        currency_repo: CurrencyRepository | None = None,
    ):
        self.repository = repository
        self.instrument_repo = instrument_repo
        self.company_repo = company_repo
        self.account_repo = account_repo
        self.currency_repo = currency_repo

    async def _resolve_or_create_stock_instrument(
        self, workspace_id: int, symbol: str
    ) -> Instrument | None:
        if self.instrument_repo is None:
            return None
        instrument = await self.instrument_repo.get_by_symbol(workspace_id, symbol)
        if instrument is not None:
            return instrument

        company: Company | None = None
        if self.company_repo is not None:
            company = await self.company_repo.get_by_name(workspace_id, symbol)
            if company is None:
                company = await self.company_repo.create(
                    Company(workspace_id=workspace_id, name=symbol, ticker=symbol)
                )

        instrument = await self.instrument_repo.create(
            Instrument(
                workspace_id=workspace_id,
                symbol=symbol,
                name=symbol,
                instrument_type=InstrumentType.stock.value,
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

    async def create_holding(
        self,
        user_id: int,
        workspace_id: int,
        holding_in: HoldingCreate,
        audit_logger: AuditLogger | None = None,
    ) -> Holding:
        account = await self._validate_refs(
            workspace_id, holding_in.account_id, holding_in.currency
        )
        existing = await self.repository.get_by_unique_key(
            workspace_id, holding_in.symbol, account.id
        )
        if existing:
            raise ConflictError(
                detail=("A holding already exists for this symbol/account in this workspace")
            )

        holding = Holding(
            workspace_id=workspace_id,
            user_id=user_id,
            symbol=holding_in.symbol,
            account_id=account.id,
            quantity=holding_in.quantity,
            avg_cost=holding_in.avg_cost,
            currency=holding_in.currency,
        )
        instrument = await self._resolve_or_create_stock_instrument(workspace_id, holding_in.symbol)
        if instrument is not None:
            holding.instrument_id = instrument.id
        holding = await self.repository.create(holding)

        if audit_logger:
            after_snap = _snapshot_holding(holding)
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="create",
                module="investing",
                entity_type="holding",
                entity_id=holding.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(holding.public_id),
                    "before": None,
                    "after": after_snap,
                    "changed_fields": list(after_snap.keys()),
                },
            )

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


async def _fetch_stock_price(symbol: str) -> Decimal | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper().strip()}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            result = data.get("chart", {}).get("result")
            if result and len(result) > 0:
                meta = result[0].get("meta", {})
                price = meta.get("regularMarketPrice")
                if price is not None:
                    return Decimal(str(price))
        except Exception:
            pass
    return None


class PerformanceService:
    def __init__(
        self,
        holding_repo: HoldingRepository,
        cash_repo: CashBalanceRepository,
        holding_price_repo: HoldingPriceRepository,
        snapshot_repo: PortfolioSnapshotRepository,
        finance_setting_repo: FinanceSettingRepository | None = None,
        fx_rate_repo: FxRateRepository | None = None,
    ):
        self.holding_repo = holding_repo
        self.cash_repo = cash_repo
        self.holding_price_repo = holding_price_repo
        self.snapshot_repo = snapshot_repo
        self.finance_setting_repo = finance_setting_repo
        self.fx_rate_repo = fx_rate_repo

    async def refresh_workspace_prices(self, workspace_id: int) -> dict[str, Decimal]:
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
        if not holdings:
            return {}

        symbols = sorted({h.symbol.upper().strip() for h in holdings})
        tasks = [_fetch_stock_price(sym) for sym in symbols]
        results = await asyncio.gather(*tasks)
        prices = {
            sym: price for sym, price in zip(symbols, results, strict=False) if price is not None
        }

        if prices:
            today = datetime.now(UTC).date()
            for h in holdings:
                if h.id is not None:
                    sym = h.symbol.upper().strip()
                    if sym in prices:
                        await self.holding_price_repo.upsert_price(
                            workspace_id=workspace_id,
                            holding_id=h.id,
                            price_date=today,
                            unit_price=prices[sym],
                            source="api",
                        )
            await self.create_snapshot(workspace_id, today)

        return prices

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

    async def create_snapshot(self, workspace_id: int, snapshot_date: date) -> None:
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
        cash_balances, _ = await self.cash_repo.get_all(workspace_id, limit=10000, offset=0)
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

        snapshot = await self.snapshot_repo.latest(workspace_id)
        if snapshot is None or (
            configured_reporting_currency is not None
            and snapshot.currency_code != configured_reporting_currency
        ):
            today = datetime.now(UTC).date()
            await self.create_snapshot(workspace_id, today)
            snapshot = await self.snapshot_repo.latest(workspace_id)
        if snapshot is None:
            raise ValidationError(detail="Failed to generate portfolio snapshot")
        gain = snapshot.total_value - snapshot.total_cost
        pct = (gain / snapshot.total_cost * Decimal("100")) if snapshot.total_cost else None
        return PerformanceSummaryResponse(
            total_value=snapshot.total_value,
            total_cost=snapshot.total_cost,
            total_gain_loss=gain,
            total_gain_loss_pct=pct,
            snapshot_date=snapshot.snapshot_date,
            currency=snapshot.currency_code,
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
        existing = await self.instrument_repo.get_by_symbol(workspace_id, payload.symbol)
        if existing is not None:
            raise ConflictError(detail=f"Instrument '{payload.symbol}' already exists")

        company_id: int | None = None
        if payload.instrument_type == InstrumentType.stock:
            company = await self.company_repo.get_by_name(workspace_id, payload.name)
            if company is None:
                company = await self.company_repo.create(
                    Company(
                        workspace_id=workspace_id,
                        name=payload.name,
                        ticker=payload.ticker or payload.symbol,
                    )
                )
            company_id = company.id

        return await self.instrument_repo.create(
            Instrument(
                workspace_id=workspace_id,
                symbol=payload.symbol,
                name=payload.name,
                instrument_type=payload.instrument_type.value,
                company_id=company_id,
            )
        )


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

        total_weight = sum(item.weight for item in payload.constituents)
        if not (Decimal("0.99") <= total_weight <= Decimal("1.01")):
            raise ValidationError(
                detail=f"Constituent weights must sum to approximately 1.0 (got {total_weight})"
            )

        await self.constituent_repo.delete_snapshot(
            instrument.id, payload.as_of_date, payload.source
        )  # type: ignore[arg-type]
        rows: list[InstrumentConstituent] = []
        requested_names = [item.company_name for item in payload.constituents]
        companies_by_name = await self.company_repo.get_by_names(workspace_id, requested_names)
        for item in payload.constituents:
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
        staleness_window_days: int = 30,
    ):
        self.holding_repo = holding_repo
        self.instrument_repo = instrument_repo
        self.company_repo = company_repo
        self.constituent_repo = constituent_repo
        self.staleness_window_days = staleness_window_days

    async def exposure(self, workspace_id: int, as_of: date) -> ExposureAnalyticsResponse:
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
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
            value = h.quantity * h.avg_cost
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
        return ExposureAnalyticsResponse(
            as_of_date=as_of,
            analysis_status=analysis_status,
            snapshot_coverage=coverage,
            staleness_days=self.staleness_window_days,
            warnings=warnings,
            exposure=rows,
            total_direct_exposure=sum((r.direct_exposure for r in rows), Decimal("0")),
            total_lookthrough_exposure=sum((r.lookthrough_exposure for r in rows), Decimal("0")),
        )

    async def overlap(self, workspace_id: int, as_of: date) -> OverlapAnalyticsResponse:
        exposure = await self.exposure(workspace_id, as_of)
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

        return OverlapAnalyticsResponse(
            as_of_date=as_of,
            analysis_status=exposure.analysis_status,
            snapshot_coverage=exposure.snapshot_coverage,
            warnings=exposure.warnings,
            top_5_concentration_pct=top5,
            top_10_concentration_pct=top10,
            duplicate_exposure_index=duplicate,
            overlaps=overlaps,
        )


class InvestingSummaryService:
    def __init__(
        self,
        holding_repo: HoldingRepository,
        cash_repo: CashBalanceRepository,
        finance_setting_repo: FinanceSettingRepository | None = None,
        fx_rate_repo: FxRateRepository | None = None,
    ):
        self.holding_repo = holding_repo
        self.cash_repo = cash_repo
        self.finance_setting_repo = finance_setting_repo
        self.fx_rate_repo = fx_rate_repo

    async def get_summary(self, workspace_id: int) -> InvestingSummaryResponse:
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=10000, offset=0)
        cash_balances, _ = await self.cash_repo.get_all(workspace_id, limit=10000, offset=0)

        breakdown: dict[str, Decimal] = {}

        for holding in holdings:
            value = holding.quantity * holding.avg_cost
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
                    portfolio_value += holding.quantity * holding.avg_cost
                for cash in cash_balances:
                    cash_total += cash.balance
                return InvestingSummaryResponse(
                    portfolio_value=portfolio_value,
                    holdings_count=len(holdings),
                    cash_total=cash_total,
                    currency_breakdown=breakdown,
                    daily_change=None,
                    reporting_currency=currency,
                    valuation_status="single_currency_native",
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
                native_value = holding.quantity * holding.avg_cost
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

            return InvestingSummaryResponse(
                portfolio_value=converted_portfolio,
                holdings_count=len(holdings),
                cash_total=converted_cash,
                currency_breakdown=breakdown,
                daily_change=None,
                reporting_currency=reporting_currency,
                valuation_status="converted_available",
                fx_as_of=valuation_as_of,
                fx_rates_used=_fx_rates_used(used_currencies, reporting_currency, fx_lookup),
            )

        portfolio_value = Decimal("0")
        cash_total = Decimal("0")
        for holding in holdings:
            portfolio_value += holding.quantity * holding.avg_cost
        for cash in cash_balances:
            cash_total += cash.balance

        return InvestingSummaryResponse(
            portfolio_value=portfolio_value,
            holdings_count=len(holdings),
            cash_total=cash_total,
            currency_breakdown=breakdown,
            daily_change=None,
            reporting_currency=reporting_currency,
            valuation_status="converted_available",
            fx_as_of=None,
        )
