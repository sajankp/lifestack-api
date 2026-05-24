import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.dependencies import (
    get_current_user,
    get_current_workspace_id,
    get_finance_account_service,
    get_finance_currency_service,
)
from app.core.pagination import PaginatedResponse, PaginationParams
from app.finance.schemas import (
    AccountCreate,
    AccountResponse,
    AccountUpdate,
    CurrencyResponse,
)
from app.finance.service import AccountService, CurrencyService

router = APIRouter(prefix="/finance", tags=["finance"])


@router.get("/currencies", response_model=list[CurrencyResponse])
async def list_workspace_currencies(
    currency_service: Annotated[CurrencyService, Depends(get_finance_currency_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    currencies = await currency_service.list_workspace_currencies(workspace_id)
    return [CurrencyResponse.model_validate(c) for c in currencies]


@router.get("/accounts", response_model=PaginatedResponse[AccountResponse])
async def list_accounts(
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
):
    accounts, total = await account_service.list_accounts(
        workspace_id, pagination.limit, pagination.offset
    )
    return PaginatedResponse(
        items=[AccountResponse.model_validate(a) for a in accounts],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post("/accounts", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account(
    account_in: AccountCreate,
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    account = await account_service.create_account(workspace_id, account_in)
    return AccountResponse.model_validate(account)


@router.patch("/accounts/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: uuid.UUID,
    account_in: AccountUpdate,
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    account = await account_service.update_account(workspace_id, account_id, account_in)
    return AccountResponse.model_validate(account)
