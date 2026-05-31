import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.audit import AuditLogger
from app.core.dependencies import (
    get_audit_logger,
    get_current_user,
    get_current_workspace_id,
    get_investing_analytics_service,
    get_investing_cash_balance_service,
    get_investing_constituent_service,
    get_investing_holding_service,
    get_investing_instrument_service,
    get_investing_performance_service,
    get_investing_summary_service,
    require_min_role,
)
from app.core.pagination import PaginatedResponse, PaginationParams
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

router = APIRouter(prefix="/investing", tags=["investing"])


@router.get("/holdings", response_model=PaginatedResponse[HoldingResponse])
async def list_holdings(
    holding_service: Annotated[HoldingService, Depends(get_investing_holding_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
):
    holdings, total = await holding_service.list_holdings(
        workspace_id, pagination.limit, pagination.offset
    )
    return PaginatedResponse(
        items=[HoldingResponse.model_validate(h) for h in holdings],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post("/holdings", response_model=HoldingResponse, status_code=status.HTTP_201_CREATED)
async def create_holding(
    holding_in: HoldingCreate,
    holding_service: Annotated[HoldingService, Depends(get_investing_holding_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    holding = await holding_service.create_holding(
        user["id"], workspace_id, holding_in, audit_logger=audit_logger
    )
    return HoldingResponse.model_validate(holding)


@router.patch("/holdings/{holding_id}", response_model=HoldingResponse)
async def update_holding(
    holding_id: uuid.UUID,
    holding_in: HoldingUpdate,
    holding_service: Annotated[HoldingService, Depends(get_investing_holding_service)],
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
    return HoldingResponse.model_validate(holding)


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
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
):
    balances, total = await cash_service.list_cash_balances(
        workspace_id, pagination.limit, pagination.offset
    )
    return PaginatedResponse(
        items=[CashBalanceResponse.model_validate(c) for c in balances],
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
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    cash = await cash_service.create_cash_balance(
        user["id"], workspace_id, cash_in, audit_logger=audit_logger
    )
    return CashBalanceResponse.model_validate(cash)


@router.patch("/cash-balances/{cash_balance_id}", response_model=CashBalanceResponse)
async def update_cash_balance(
    cash_balance_id: uuid.UUID,
    cash_in: CashBalanceUpdate,
    cash_service: Annotated[CashBalanceService, Depends(get_investing_cash_balance_service)],
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
    return CashBalanceResponse.model_validate(cash)


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
    return [InstrumentResponse.model_validate(item) for item in instruments]


@router.post("/instruments", response_model=InstrumentResponse, status_code=status.HTTP_201_CREATED)
async def create_instrument(
    payload: InstrumentCreate,
    instrument_service: Annotated[InstrumentService, Depends(get_investing_instrument_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    instrument = await instrument_service.create_instrument(workspace_id, payload)
    return InstrumentResponse.model_validate(instrument)


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


@router.get("/performance/summary", response_model=PerformanceSummaryResponse)
async def get_performance_summary(
    performance_service: Annotated[PerformanceService, Depends(get_investing_performance_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    return await performance_service.summary(workspace_id)
