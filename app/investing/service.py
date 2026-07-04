import csv
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.imports.models import ImportBatch

import httpx

from app.config import settings
from app.core.audit import AuditLogger, snapshot_columns
from app.core.currency import (
    build_required_pairs as _build_required_pairs,
)
from app.core.currency import (
    convert_amount as _convert_amount,
)
from app.core.currency import (
    fx_rates_used as _fx_rates_used,
)
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.pagination import DEFAULT_LIMIT
from app.finance.models import Account, FxRate
from app.finance.repository import (
    AccountRepository,
    CurrencyRepository,
    FinanceSettingRepository,
    FxRateRepository,
)
from app.imports.repository import ImportRepository
from app.investing.models import (
    CashBalance,
    Company,
    Holding,
    Instrument,
    InstrumentConstituent,
    InstrumentType,
)
from app.investing.repository import (
    CashBalanceRepository,
    CompanyRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InstrumentConstituentRepository,
    InstrumentRepository,
    InvestingOrderRepository,
    PortfolioSnapshotRepository,
)
from app.investing.response_helpers import instrument_response, populate_valuation_fields
from app.investing.schemas import (
    CashBalanceCreate,
    CashBalanceUpdate,
    ExposureAnalyticsResponse,
    ExposureCompanyRow,
    HoldingResponse,
    HoldingUpdate,
    InstrumentConstituentResponse,
    InstrumentConstituentUpsert,
    InstrumentCreate,
    InstrumentResponse,
    InstrumentUpdate,
    OverlapAnalyticsResponse,
    OverlapRow,
)
from app.spending.response_helpers import source_metadata_response

MONEY_QUANT = Decimal("0.01")


_HOLDING_AUDIT_FIELDS = (
    "symbol",
    "account_id",
    "quantity",
    "avg_cost",
    "currency",
)

_CASH_BALANCE_AUDIT_FIELDS = (
    "account_id",
    "balance",
    "currency",
    "as_of",
)


def _snapshot_holding(holding: Holding) -> dict:
    data = snapshot_columns(holding, _HOLDING_AUDIT_FIELDS)
    # Convert Decimal fields for JSON serialization
    if data.get("quantity") is not None:
        data["quantity"] = str(data["quantity"])
    if data.get("avg_cost") is not None:
        data["avg_cost"] = str(data["avg_cost"])
    return data


def _snapshot_cash_balance(cash: CashBalance) -> dict:
    data = snapshot_columns(cash, _CASH_BALANCE_AUDIT_FIELDS)
    # Convert Decimal and datetime fields for JSON serialization
    if data.get("balance") is not None:
        data["balance"] = str(data["balance"])
    if data.get("as_of") is not None:
        data["as_of"] = (
            data["as_of"].isoformat() if hasattr(data["as_of"], "isoformat") else str(data["as_of"])
        )
    return data


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

    async def _build_account_cache(self, workspace_id: int) -> dict[int, tuple[uuid.UUID, str]]:
        if self.account_repo is None:
            return {}
        accounts, _ = await self.account_repo.list_workspace_accounts(
            workspace_id, limit=10000, offset=0
        )
        return {a.id: (a.public_id, a.name) for a in accounts if a.id is not None}

    async def _build_instrument_type_cache(
        self, workspace_id: int, holdings: list | tuple
    ) -> dict[int, str]:
        if self.instrument_repo is None:
            return {}
        instrument_ids = [h.instrument_id for h in holdings if h.instrument_id is not None]
        instruments = await self.instrument_repo.get_by_ids(instrument_ids)
        return {
            instrument_id: instrument.instrument_type
            for instrument_id, instrument in instruments.items()
        }

    async def _build_import_batch_cache(
        self, workspace_id: int, holdings: list | tuple
    ) -> dict[int, "ImportBatch"]:
        import_batch_ids = {h.source_import_id for h in holdings if h.source_import_id is not None}
        if not import_batch_ids:
            return {}
        import_repo = ImportRepository(self.repository.session)
        return await import_repo.get_by_ids(workspace_id, import_batch_ids)

    async def list_holdings_with_details(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[list[HoldingResponse], int]:
        holdings, total = await self.list_holdings(workspace_id, limit, offset)
        if not holdings:
            return [], total

        account_cache = await self._build_account_cache(workspace_id)
        import_cache = await self._build_import_batch_cache(workspace_id, holdings)
        instrument_type_cache = await self._build_instrument_type_cache(workspace_id, holdings)

        holding_ids = [h.id for h in holdings if h.id is not None]
        today = datetime.now(UTC).date()
        latest_prices = {}
        if self.holding_price_repo is not None:
            latest_prices = await self.holding_price_repo.latest_prices_on_or_before_bulk(
                workspace_id, holding_ids, today
            )

        items = []
        for h in holdings:
            pub_id, name = account_cache.get(h.account_id, (None, "Unknown"))
            data = h.model_dump()
            data["instrument_type"] = (
                instrument_type_cache.get(h.instrument_id, "stock")
                if h.instrument_id is not None
                else "stock"
            )
            data["account_id"] = pub_id
            data["account_name"] = name
            data["source_metadata"] = source_metadata_response(
                h.source_type, h.source_ref, import_cache.get(h.source_import_id)
            )

            price_row = latest_prices.get(h.id) if h.id is not None else None
            unit_price = price_row.unit_price if price_row is not None else None
            populate_valuation_fields(data, h.quantity, h.avg_cost, unit_price)

            items.append(HoldingResponse.model_validate(data))
        return items, total

    async def update_holding_with_details(
        self,
        workspace_id: int,
        holding_id: uuid.UUID,
        holding_in: HoldingUpdate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> HoldingResponse:
        holding = await self.update_holding(
            workspace_id,
            holding_id,
            holding_in,
            actor_id=actor_id,
            audit_logger=audit_logger,
        )
        snapshot_repo = PortfolioSnapshotRepository(self.repository.session)
        await snapshot_repo.delete_for_date(workspace_id, datetime.now(UTC).date())

        account = None
        if self.account_repo is not None:
            account = await self.account_repo.get_by_id(workspace_id, holding.account_id)

        data = holding.model_dump()
        instrument_type_cache = await self._build_instrument_type_cache(workspace_id, [holding])
        data["instrument_type"] = (
            instrument_type_cache.get(holding.instrument_id, "stock")
            if holding.instrument_id is not None
            else "stock"
        )
        data["account_id"] = account.public_id if account else None
        data["account_name"] = account.name if account else "Unknown"
        data["source_metadata"] = source_metadata_response(
            holding.source_type, holding.source_ref, None
        )

        today = datetime.now(UTC).date()
        price_row = None
        if self.holding_price_repo is not None:
            price_row = await self.holding_price_repo.latest_price_on_or_before(
                workspace_id, holding.id, today
            )
        unit_price = price_row.unit_price if price_row is not None else None
        populate_valuation_fields(data, holding.quantity, holding.avg_cost, unit_price)

        return HoldingResponse.model_validate(data)

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
        code = currency.upper()
        if self.currency_repo is not None:
            currency_row = await self.currency_repo.get_by_code(code)
            if not currency_row or not currency_row.is_active:
                raise ValidationError(detail=f"Unsupported currency code '{code}'")
            await self.currency_repo.ensure_workspace_defaults(workspace_id)
            enabled = await self.currency_repo.is_enabled_for_workspace(workspace_id, code)
            if not enabled:
                raise ValidationError(detail=f"Currency '{code}' is not enabled for this workspace")
        # One account, one currency (spec-050): a cash balance's currency must match
        # the account's default_currency_code, whatever account type it is (this
        # also covers reconciliation snapshots on bank/wallet accounts).
        if code != account.default_currency_code.upper():
            raise ValidationError(
                detail=(
                    f"Currency '{code}' does not match account '{account.name}' "
                    f"({account.default_currency_code})"
                )
            )
        return account

    async def list_cash_balances(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        account_id: int | None = None,
    ) -> tuple[Sequence[CashBalance], int]:
        return await self.repository.get_all(workspace_id, limit, offset, account_id=account_id)

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
        account = None
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
        # One account, one currency (spec-050). Only enforced when currency is
        # actually being changed, so patching an unrelated field (balance,
        # as_of) on a pre-existing row never fails on this check.
        if (
            "currency" in update_data
            and account is not None
            and next_currency.upper() != account.default_currency_code.upper()
        ):
            raise ValidationError(
                detail=(
                    f"Currency '{next_currency.upper()}' does not match account "
                    f"'{account.name}' ({account.default_currency_code})"
                )
            )

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


async def _fetch_nse_bhavcopy(
    client: httpx.AsyncClient, trade_date: date
) -> dict[str, tuple[date, Decimal]]:
    """Official NSE end-of-day security-wise price CSV (spec-057).

    NSE has rotated the bhavcopy URL/format before and requires session
    cookies from a prior request to the main site before archive requests
    reliably succeed; this returns {} on any failure (feed outage, trading
    holiday, unparseable format) so callers degrade to the existing
    Yahoo-backed fallback rather than blocking the whole refresh cycle —
    same failure contract as ``_fetch_all_amfi_navs``.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        ),
        "Accept": "text/csv,application/csv,*/*",
    }
    navs: dict[str, tuple[date, Decimal]] = {}
    try:
        await client.get("https://www.nseindia.com", headers=headers, follow_redirects=True)
        url = (
            "https://archives.nseindia.com/products/content/"
            f"sec_bhavdata_full_{trade_date.strftime('%d%m%Y')}.csv"
        )
        resp = await client.get(url, headers=headers, follow_redirects=True)
        resp.raise_for_status()
        for raw_row in csv.DictReader(resp.text.splitlines()):
            row = {
                (k or "").strip().upper(): (v.strip() if isinstance(v, str) else v)
                for k, v in raw_row.items()
            }
            if row.get("SERIES") != "EQ":
                continue
            symbol = row.get("SYMBOL", "")
            close_raw = row.get("CLOSE_PRICE") or row.get("CLOSE")
            if not symbol or not close_raw:
                continue
            try:
                close = Decimal(close_raw)
            except InvalidOperation:
                continue
            navs[symbol.upper()] = (trade_date, close)
    except Exception:
        return {}
    return navs


class InstrumentService:
    def __init__(
        self,
        instrument_repo: InstrumentRepository,
        company_repo: CompanyRepository,
    ):
        self.instrument_repo = instrument_repo
        self.company_repo = company_repo

    async def get_instrument(self, workspace_id: int, public_id: uuid.UUID) -> Instrument:
        instrument = await self.instrument_repo.get_by_public_id(workspace_id, public_id)
        if instrument is None:
            raise NotFoundError(detail=f"Instrument '{public_id}' not found")
        return instrument

    async def list_instruments_with_details(self, workspace_id: int) -> list[InstrumentResponse]:
        instruments = await self.list_instruments(workspace_id)
        company_ids = [item.company_id for item in instruments if item.company_id is not None]
        companies = await self.company_repo.get_by_ids(company_ids)
        return [
            await instrument_response(self, workspace_id, item, companies) for item in instruments
        ]

    async def create_instrument_with_details(
        self, workspace_id: int, payload: InstrumentCreate
    ) -> InstrumentResponse:
        instrument = await self.create_instrument(workspace_id, payload)
        return await instrument_response(self, workspace_id, instrument)

    async def get_instrument_with_details(
        self, workspace_id: int, public_id: uuid.UUID
    ) -> InstrumentResponse:
        instrument = await self.get_instrument(workspace_id, public_id)
        return await instrument_response(self, workspace_id, instrument)

    async def update_instrument_with_details(
        self, workspace_id: int, public_id: uuid.UUID, payload: InstrumentUpdate
    ) -> InstrumentResponse:
        instrument = await self.update_instrument(workspace_id, public_id, payload)
        return await instrument_response(self, workspace_id, instrument)

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
        symbol = symbol.strip().upper()
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
