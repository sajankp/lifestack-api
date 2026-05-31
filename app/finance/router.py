import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.audit import AuditLogger
from app.core.dependencies import (
    get_audit_logger,
    get_current_user,
    get_current_workspace_id,
    get_finance_account_service,
    get_finance_currency_service,
    get_finance_fx_rate_service,
    get_finance_setting_service,
    get_finance_transfer_service,
)
from app.core.exceptions import NotFoundError
from app.core.pagination import PaginatedResponse, PaginationParams
from app.finance.models import CurrencyDisplayPreference
from app.finance.schemas import (
    AccountCreate,
    AccountResponse,
    AccountUpdate,
    CapitalTransferCreate,
    CapitalTransferResponse,
    CurrencyResponse,
    FxRateResponse,
    UserFinanceSettingResponse,
    UserFinanceSettingUpdate,
    WorkspaceFinanceSettingResponse,
    WorkspaceFinanceSettingUpdate,
)
from app.finance.service import (
    AccountService,
    CapitalTransferService,
    CurrencyService,
    FinanceSettingService,
    FxRateService,
)

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


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: uuid.UUID,
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
):
    await account_service.delete_account(
        workspace_id=workspace_id,
        public_id=account_id,
        actor_id=user["id"],
        audit_logger=audit_logger,
    )


@router.get("/settings", response_model=WorkspaceFinanceSettingResponse)
async def get_workspace_finance_settings(
    setting_service: Annotated[FinanceSettingService, Depends(get_finance_setting_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    row = await setting_service.get_setting(workspace_id)
    if row is None:
        # Keep response stable while settings row may not exist yet.
        return WorkspaceFinanceSettingResponse(
            reporting_currency_code=None,
            currency_display_preference=CurrencyDisplayPreference.symbol,
            updated_at=datetime.now(UTC),
        )
    return WorkspaceFinanceSettingResponse.model_validate(row)


@router.patch("/settings", response_model=WorkspaceFinanceSettingResponse)
async def update_workspace_finance_settings(
    setting_in: WorkspaceFinanceSettingUpdate,
    setting_service: Annotated[FinanceSettingService, Depends(get_finance_setting_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    row = await setting_service.update_workspace_settings(
        workspace_id,
        setting_in.model_dump(exclude_unset=True),
    )
    return WorkspaceFinanceSettingResponse.model_validate(row)


@router.get("/settings/user", response_model=UserFinanceSettingResponse)
async def get_user_finance_settings(
    setting_service: Annotated[FinanceSettingService, Depends(get_finance_setting_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    setting = await setting_service.get_user_settings(workspace_id, user["id"])
    return UserFinanceSettingResponse.model_validate(setting)


@router.patch("/settings/user", response_model=UserFinanceSettingResponse)
async def update_user_finance_settings(
    setting_in: UserFinanceSettingUpdate,
    setting_service: Annotated[FinanceSettingService, Depends(get_finance_setting_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    setting = await setting_service.update_user_settings(
        workspace_id,
        user["id"],
        setting_in.model_dump(exclude_unset=True),
    )
    return UserFinanceSettingResponse.model_validate(setting)


@router.get("/fx-rates", response_model=FxRateResponse)
async def get_fx_rate(
    base: str,
    quote: str,
    fx_service: Annotated[FxRateService, Depends(get_finance_fx_rate_service)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    rate_row = await fx_service.get_latest_pair(base, quote)
    if rate_row is None:
        raise NotFoundError(detail=f"FX rate not found for pair {base.upper()}/{quote.upper()}")
    return FxRateResponse.model_validate(rate_row)


@router.get("/transfers", response_model=PaginatedResponse[CapitalTransferResponse])
async def list_transfers(
    transfer_service: Annotated[CapitalTransferService, Depends(get_finance_transfer_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
):
    transfers, total = await transfer_service.list_transfers(
        workspace_id, pagination.limit, pagination.offset
    )
    return PaginatedResponse(
        items=[CapitalTransferResponse.model_validate(t) for t in transfers],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/transfers/{transfer_id}", response_model=CapitalTransferResponse)
async def get_transfer(
    transfer_id: uuid.UUID,
    transfer_service: Annotated[CapitalTransferService, Depends(get_finance_transfer_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    transfer = await transfer_service.get_transfer(workspace_id, transfer_id)
    return CapitalTransferResponse.model_validate(transfer)


@router.post(
    "/transfers", response_model=CapitalTransferResponse, status_code=status.HTTP_201_CREATED
)
async def create_transfer(
    transfer_in: CapitalTransferCreate,
    transfer_service: Annotated[CapitalTransferService, Depends(get_finance_transfer_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
):
    transfer = await transfer_service.create_transfer(
        workspace_id=workspace_id,
        actor_id=user["id"],
        transfer_in=transfer_in,
        audit_logger=audit_logger,
    )
    return CapitalTransferResponse.model_validate(transfer)
