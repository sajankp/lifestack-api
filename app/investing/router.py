import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.audit import AuditLogger
from app.core.dependencies import (
    get_audit_logger,
    get_current_user,
    get_current_workspace_id,
    get_investing_cash_balance_service,
    get_investing_holding_service,
    get_investing_summary_service,
)
from app.core.pagination import PaginatedResponse, PaginationParams
from app.investing.schemas import (
    CashBalanceCreate,
    CashBalanceResponse,
    CashBalanceUpdate,
    HoldingCreate,
    HoldingResponse,
    HoldingUpdate,
    InvestingSummaryResponse,
)
from app.investing.service import CashBalanceService, HoldingService, InvestingSummaryService

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
