import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status

from app.core.audit import AuditLogger
from app.core.dependencies import (
    get_audit_logger,
    get_current_user,
    get_current_workspace_id,
    get_finance_account_service,
    get_import_repo,
    get_investing_analytics_service,
    get_investing_cash_balance_service,
    get_investing_constituent_service,
    get_investing_holding_price_repo,
    get_investing_holding_service,
    get_investing_instrument_service,
    get_investing_performance_service,
    get_investing_summary_service,
    require_min_role,
)
from app.core.pagination import PaginatedResponse, PaginationParams
from app.finance.service import AccountService
from app.imports.repository import ImportRepository
from app.investing.repository import HoldingPriceRepository
from app.investing.schemas import (
    CashBalanceCreate,
    CashBalanceResponse,
    CashBalanceUpdate,
    ExposureAnalyticsResponse,
    HoldingCreate,
    HoldingPriceBulkCreate,
    HoldingResponse,
    HoldingUpdate,
    InstrumentConstituentResponse,
    InstrumentConstituentUpsert,
    InstrumentCreate,
    InstrumentResponse,
    InstrumentUpdate,
    InvestingSummaryResponse,
    OverlapAnalyticsResponse,
    PerformanceSummaryResponse,
)
from app.investing.service import (
    CashBalanceService,
    ConstituentService,
    ExposureAnalyticsService,
    HoldingService,
    InstrumentService,
    InvestingSummaryService,
    PerformanceService,
)
from app.spending.router import _build_import_batch_cache, _source_metadata_response

router = APIRouter(prefix="/investing", tags=["investing"])


async def _build_account_cache(
    account_service: AccountService, workspace_id: int
) -> dict[int, tuple[uuid.UUID, str]]:
    accounts, _ = await account_service.list_accounts(workspace_id, limit=10000, offset=0)
    return {a.id: (a.public_id, a.name) for a in accounts if a.id is not None}


async def _build_instrument_type_cache(
    holding_service: HoldingService, workspace_id: int, holdings: list | tuple
) -> dict[int, str]:
    if holding_service.instrument_repo is None:
        return {}
    instrument_ids = [h.instrument_id for h in holdings if h.instrument_id is not None]
    instruments = await holding_service.instrument_repo.get_by_ids(instrument_ids)
    return {
        instrument_id: instrument.instrument_type
        for instrument_id, instrument in instruments.items()
    }


async def _instrument_response(
    instrument_service: InstrumentService,
    workspace_id: int,
    instrument,
    company_cache: dict[int, Any] | None = None,
) -> InstrumentResponse:
    data = instrument.model_dump()
    if instrument.company_id is not None:
        company = (
            company_cache.get(instrument.company_id)
            if company_cache is not None
            else await instrument_service.company_repo.get_by_id(instrument.company_id)
        )
        if company is not None and company.workspace_id == workspace_id:
            data["company_id"] = company.public_id
        else:
            data["company_id"] = None
    return InstrumentResponse.model_validate(data)


def _populate_valuation_fields(
    data: dict, quantity: Decimal, avg_cost: Decimal, unit_price: Decimal | None
) -> None:
    current_price = unit_price if unit_price is not None else avg_cost
    current_value = quantity * current_price
    book_value = quantity * avg_cost
    gain_loss = current_value - book_value
    gain_loss_pct = (gain_loss / book_value * Decimal("100")) if book_value else Decimal("0")

    data["current_price"] = current_price
    data["current_value"] = current_value
    data["gain_loss"] = gain_loss
    data["gain_loss_pct"] = gain_loss_pct


@router.get("/holdings", response_model=PaginatedResponse[HoldingResponse])
async def list_holdings(
    holding_service: Annotated[HoldingService, Depends(get_investing_holding_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    import_repo: Annotated[ImportRepository, Depends(get_import_repo)],
    price_repo: Annotated[HoldingPriceRepository, Depends(get_investing_holding_price_repo)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
):
    holdings, total = await holding_service.list_holdings(
        workspace_id, pagination.limit, pagination.offset
    )
    if not holdings:
        return PaginatedResponse(
            items=[],
            total=total,
            limit=pagination.limit,
            offset=pagination.offset,
        )

    account_cache = await _build_account_cache(account_service, workspace_id)
    import_cache = await _build_import_batch_cache(import_repo, workspace_id, holdings)
    instrument_type_cache = await _build_instrument_type_cache(
        holding_service, workspace_id, holdings
    )

    holding_ids = [h.id for h in holdings if h.id is not None]
    today = datetime.now(UTC).date()
    latest_prices = await price_repo.latest_prices_on_or_before_bulk(
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
        data["source_metadata"] = _source_metadata_response(
            h.source_type, h.source_ref, import_cache.get(h.source_import_id)
        )

        price_row = latest_prices.get(h.id) if h.id is not None else None
        unit_price = price_row.unit_price if price_row is not None else None
        _populate_valuation_fields(data, h.quantity, h.avg_cost, unit_price)

        items.append(HoldingResponse.model_validate(data))
    return PaginatedResponse(
        items=items,
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post("/holdings", response_model=HoldingResponse, status_code=status.HTTP_201_CREATED)
async def create_holding(
    holding_in: HoldingCreate,
    holding_service: Annotated[HoldingService, Depends(get_investing_holding_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    price_repo: Annotated[HoldingPriceRepository, Depends(get_investing_holding_price_repo)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    holding = await holding_service.create_holding(
        user["id"], workspace_id, holding_in, audit_logger=audit_logger
    )
    account = await account_service.account_repository.get_by_id(workspace_id, holding.account_id)
    data = holding.model_dump()
    data["instrument_type"] = holding_in.instrument_type.value
    data["account_id"] = account.public_id if account else None
    data["account_name"] = account.name if account else "Unknown"
    data["source_metadata"] = _source_metadata_response(
        holding.source_type, holding.source_ref, None
    )

    today = datetime.now(UTC).date()
    price_row = (
        await price_repo.latest_price_on_or_before(workspace_id, holding.id, today)
        if holding.id is not None
        else None
    )
    unit_price = price_row.unit_price if price_row is not None else None
    _populate_valuation_fields(data, holding.quantity, holding.avg_cost, unit_price)

    return HoldingResponse.model_validate(data)


@router.patch("/holdings/{holding_id}", response_model=HoldingResponse)
async def update_holding(
    holding_id: uuid.UUID,
    holding_in: HoldingUpdate,
    holding_service: Annotated[HoldingService, Depends(get_investing_holding_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    price_repo: Annotated[HoldingPriceRepository, Depends(get_investing_holding_price_repo)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    holding = await holding_service.update_holding(
        workspace_id,
        holding_id,
        holding_in,
        actor_id=user["id"],
        audit_logger=audit_logger,
    )
    account = await account_service.account_repository.get_by_id(workspace_id, holding.account_id)
    data = holding.model_dump()
    instrument_type_cache = await _build_instrument_type_cache(
        holding_service, workspace_id, [holding]
    )
    data["instrument_type"] = (
        instrument_type_cache.get(holding.instrument_id, "stock")
        if holding.instrument_id is not None
        else "stock"
    )
    data["account_id"] = account.public_id if account else None
    data["account_name"] = account.name if account else "Unknown"
    data["source_metadata"] = _source_metadata_response(
        holding.source_type, holding.source_ref, None
    )

    today = datetime.now(UTC).date()
    price_row = (
        await price_repo.latest_price_on_or_before(workspace_id, holding.id, today)
        if holding.id is not None
        else None
    )
    unit_price = price_row.unit_price if price_row is not None else None
    _populate_valuation_fields(data, holding.quantity, holding.avg_cost, unit_price)

    return HoldingResponse.model_validate(data)


@router.delete("/holdings/{holding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_holding(
    holding_id: uuid.UUID,
    holding_service: Annotated[HoldingService, Depends(get_investing_holding_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await holding_service.delete_holding(
        workspace_id, holding_id, actor_id=user["id"], audit_logger=audit_logger
    )


@router.get("/cash-balances", response_model=PaginatedResponse[CashBalanceResponse])
async def list_cash_balances(
    cash_service: Annotated[CashBalanceService, Depends(get_investing_cash_balance_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
):
    balances, total = await cash_service.list_cash_balances(
        workspace_id, pagination.limit, pagination.offset
    )
    if not balances:
        return PaginatedResponse(
            items=[],
            total=total,
            limit=pagination.limit,
            offset=pagination.offset,
        )

    account_cache = await _build_account_cache(account_service, workspace_id)
    items = []
    for c in balances:
        pub_id, name = account_cache.get(c.account_id, (None, "Unknown"))
        data = c.model_dump()
        data["account_id"] = pub_id
        data["account_name"] = name
        items.append(CashBalanceResponse.model_validate(data))
    return PaginatedResponse(
        items=items,
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post(
    "/cash-balances", response_model=CashBalanceResponse, status_code=status.HTTP_201_CREATED
)
async def create_cash_balance(
    cash_in: CashBalanceCreate,
    cash_service: Annotated[CashBalanceService, Depends(get_investing_cash_balance_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    cash = await cash_service.create_cash_balance(
        user["id"], workspace_id, cash_in, audit_logger=audit_logger
    )
    account = await account_service.account_repository.get_by_id(workspace_id, cash.account_id)
    data = cash.model_dump()
    data["account_id"] = account.public_id if account else None
    data["account_name"] = account.name if account else "Unknown"
    return CashBalanceResponse.model_validate(data)


@router.patch("/cash-balances/{cash_balance_id}", response_model=CashBalanceResponse)
async def update_cash_balance(
    cash_balance_id: uuid.UUID,
    cash_in: CashBalanceUpdate,
    cash_service: Annotated[CashBalanceService, Depends(get_investing_cash_balance_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    cash = await cash_service.update_cash_balance(
        workspace_id,
        cash_balance_id,
        cash_in,
        actor_id=user["id"],
        audit_logger=audit_logger,
    )
    account = await account_service.account_repository.get_by_id(workspace_id, cash.account_id)
    data = cash.model_dump()
    data["account_id"] = account.public_id if account else None
    data["account_name"] = account.name if account else "Unknown"
    return CashBalanceResponse.model_validate(data)


@router.delete("/cash-balances/{cash_balance_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cash_balance(
    cash_balance_id: uuid.UUID,
    cash_service: Annotated[CashBalanceService, Depends(get_investing_cash_balance_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await cash_service.delete_cash_balance(
        workspace_id, cash_balance_id, actor_id=user["id"], audit_logger=audit_logger
    )


@router.get("/summary", response_model=InvestingSummaryResponse)
async def get_investing_summary(
    summary_service: Annotated[InvestingSummaryService, Depends(get_investing_summary_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    return await summary_service.get_summary(workspace_id)


@router.get("/instruments", response_model=list[InstrumentResponse])
async def list_instruments(
    instrument_service: Annotated[InstrumentService, Depends(get_investing_instrument_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    instruments = await instrument_service.list_instruments(workspace_id)
    company_ids = [item.company_id for item in instruments if item.company_id is not None]
    company_cache = await instrument_service.company_repo.get_by_ids(company_ids)
    return [
        await _instrument_response(instrument_service, workspace_id, item, company_cache)
        for item in instruments
    ]


@router.post("/instruments", response_model=InstrumentResponse, status_code=status.HTTP_201_CREATED)
async def create_instrument(
    payload: InstrumentCreate,
    instrument_service: Annotated[InstrumentService, Depends(get_investing_instrument_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    instrument = await instrument_service.create_instrument(workspace_id, payload)
    return await _instrument_response(instrument_service, workspace_id, instrument)


@router.patch("/instruments/{instrument_id}", response_model=InstrumentResponse)
async def update_instrument(
    instrument_id: uuid.UUID,
    payload: InstrumentUpdate,
    instrument_service: Annotated[InstrumentService, Depends(get_investing_instrument_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    instrument = await instrument_service.update_instrument(workspace_id, instrument_id, payload)
    return await _instrument_response(instrument_service, workspace_id, instrument)


@router.get(
    "/instruments/{instrument_id}/constituents",
    response_model=list[InstrumentConstituentResponse],
)
async def get_instrument_constituents(
    instrument_id: uuid.UUID,
    as_of: str,
    constituent_service: Annotated[ConstituentService, Depends(get_investing_constituent_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    as_of_date = date.fromisoformat(as_of)
    return await constituent_service.get_constituents(workspace_id, instrument_id, as_of_date)


@router.post(
    "/instruments/{instrument_id}/constituents",
    response_model=list[InstrumentConstituentResponse],
    status_code=status.HTTP_201_CREATED,
)
async def upsert_instrument_constituents(
    instrument_id: uuid.UUID,
    payload: InstrumentConstituentUpsert,
    constituent_service: Annotated[ConstituentService, Depends(get_investing_constituent_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await constituent_service.upsert_constituents(workspace_id, instrument_id, payload)
    return await constituent_service.get_constituents(
        workspace_id=workspace_id, instrument_public_id=instrument_id, as_of=payload.as_of_date
    )


@router.get("/analytics/exposure", response_model=ExposureAnalyticsResponse)
async def get_exposure_analytics(
    as_of: str,
    analytics_service: Annotated[
        ExposureAnalyticsService, Depends(get_investing_analytics_service)
    ],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    as_of_date = date.fromisoformat(as_of)
    return await analytics_service.exposure(workspace_id, as_of_date)


@router.get("/analytics/overlap", response_model=OverlapAnalyticsResponse)
async def get_overlap_analytics(
    as_of: str,
    analytics_service: Annotated[
        ExposureAnalyticsService, Depends(get_investing_analytics_service)
    ],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    as_of_date = date.fromisoformat(as_of)
    return await analytics_service.overlap(workspace_id, as_of_date)


@router.post("/prices", status_code=status.HTTP_201_CREATED)
async def submit_prices(
    payload: HoldingPriceBulkCreate,
    performance_service: Annotated[PerformanceService, Depends(get_investing_performance_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await performance_service.submit_prices(workspace_id, payload)
    return {"ok": True}


@router.post("/prices/refresh", status_code=status.HTTP_200_OK)
async def refresh_prices(
    performance_service: Annotated[PerformanceService, Depends(get_investing_performance_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    prices = await performance_service.refresh_workspace_prices(workspace_id)

    if prices:
        price_details = {sym: str(price) for sym, price in prices.items()}
        await audit_logger.log(
            workspace_id=workspace_id,
            actor_id=user["id"],
            action="holding_prices_submitted",
            module="investing",
            entity_type="holding_price",
            entity_id=0,
            details={
                "entity_public_id": str(uuid.UUID(int=0)),
                "before": None,
                "after": {
                    "prices": price_details,
                    "source": "api",
                },
                "changed_fields": ["prices"],
                "prices": price_details,
                "source": "api",
            },
        )
    return {"updated": list(prices.keys())}


@router.get("/performance/summary", response_model=PerformanceSummaryResponse)
async def get_performance_summary(
    performance_service: Annotated[PerformanceService, Depends(get_investing_performance_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    return await performance_service.summary(workspace_id)
