import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.config import settings
from app.core.audit import AuditLogger
from app.core.dependencies import (
    get_audit_logger,
    get_current_user,
    get_current_workspace_id,
    get_finance_account_service,
    get_finance_currency_service,
    get_finance_fx_rate_service,
    get_finance_net_worth_service,
    get_finance_setting_service,
    get_finance_transfer_service,
    require_min_role,
)
from app.core.exceptions import NotFoundError, ValidationError
from app.core.pagination import PaginatedResponse, PaginationParams, build_page
from app.finance.models import CurrencyDisplayPreference
from app.finance.schemas import (
    AccountBalanceResponse,
    AccountCreate,
    AccountResponse,
    AccountUpdate,
    CapitalTransferCreate,
    CapitalTransferResponse,
    CapitalTransferUpdate,
    CurrencyResponse,
    FxRateResponse,
    NetWorthHistoryItem,
    NetWorthResponse,
    ReconciliationSummary,
    UserFinanceSettingResponse,
    UserFinanceSettingUpdate,
    UserFxRateResponse,
    UserNetWorthPointResponse,
    WorkspaceFinanceSettingResponse,
    WorkspaceFinanceSettingUpdate,
)
from app.finance.service import (
    AccountService,
    CapitalTransferService,
    CurrencyService,
    FinanceSettingService,
    FxRateService,
    NetWorthService,
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
    return build_page([AccountResponse.model_validate(a) for a in accounts], total, pagination)


@router.post("/accounts", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account(
    account_in: AccountCreate,
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("admin"))],
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
    _role: Annotated[object, Depends(require_min_role("admin"))],
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
    _role: Annotated[object, Depends(require_min_role("admin"))],
):
    await account_service.delete_account(
        workspace_id=workspace_id,
        public_id=account_id,
        actor_id=user["id"],
        audit_logger=audit_logger,
    )


@router.get("/accounts/{account_id}/balance", response_model=AccountBalanceResponse)
async def get_account_spending_balance(
    account_id: uuid.UUID,
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Retrieve the transfer-inclusive projected balance for a wallet/bank/card account.

    The balance is computed from the workspace spending transaction history
    (income minus expenses) plus capital transfer contributions (inflows minus
    outflows). It is independent of the investing cash balance snapshots.
    """
    data = await account_service.get_spending_balance(workspace_id, account_id)
    return AccountBalanceResponse.model_validate(data)


@router.get("/accounts/{account_id}/reconciliation", response_model=ReconciliationSummary)
async def get_account_reconciliation(
    account_id: uuid.UUID,
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Compare the projected spending ledger balance against the latest cash balance snapshot.

    Returns:
    - ``projected_balance``: income - expenses + transfer_in - transfer_out
      + (sell net - buy net) (all-time)
    - ``snapshot_balance``: the most recent investing cash balance snapshot, or null
    - ``discrepancy``: projected - snapshot (positive = ledger > snapshot, negative = snapshot > ledger)
    - ``transaction_count`` / ``transfer_count`` / ``order_count``: entry breakdown

    A discrepancy indicates unrecorded transactions, transfers or trades on one side.
    A null snapshot means no cash balance has been recorded yet for this account.
    """
    return await account_service.get_reconciliation_summary(workspace_id, account_id)


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
            lookthrough_min_weight_pct=settings.LOOKTHROUGH_MIN_DISPLAY_WEIGHT_PCT,
            default_spending_account_id=None,
            locale="en-US",
            decimal_places=2,
            updated_at=datetime.now(UTC),
        )
    default_account_public_id = await setting_service.resolve_default_account_public_id(
        workspace_id, row
    )
    return WorkspaceFinanceSettingResponse(
        reporting_currency_code=row.reporting_currency_code,
        currency_display_preference=row.currency_display_preference,
        lookthrough_min_weight_pct=row.lookthrough_min_weight_pct,
        default_spending_account_id=default_account_public_id,
        locale=row.locale,
        decimal_places=row.decimal_places,
        updated_at=row.updated_at,
    )


@router.patch("/settings", response_model=WorkspaceFinanceSettingResponse)
async def update_workspace_finance_settings(
    setting_in: WorkspaceFinanceSettingUpdate,
    setting_service: Annotated[FinanceSettingService, Depends(get_finance_setting_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("admin"))],
):
    row = await setting_service.update_workspace_settings(
        workspace_id,
        setting_in.model_dump(exclude_unset=True),
    )
    default_account_public_id = await setting_service.resolve_default_account_public_id(
        workspace_id, row
    )
    return WorkspaceFinanceSettingResponse(
        reporting_currency_code=row.reporting_currency_code,
        currency_display_preference=row.currency_display_preference,
        lookthrough_min_weight_pct=row.lookthrough_min_weight_pct,
        default_spending_account_id=default_account_public_id,
        locale=row.locale,
        decimal_places=row.decimal_places,
        updated_at=row.updated_at,
    )


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
    """
    Retrieve the latest FX rate for a given base and quote currency.

    FX rates are globally scoped system reference data (market data). This endpoint is read-only.
    Updates or rate ingestion are managed exclusively by the system daily cron jobs;
    direct user mutations are not permitted.
    """
    rate_row = await fx_service.get_latest_pair(base, quote)
    if rate_row is None:
        raise NotFoundError(detail=f"FX rate not found for pair {base.upper()}/{quote.upper()}")
    return FxRateResponse.model_validate(rate_row)


@router.get("/fx/history", response_model=PaginatedResponse[UserFxRateResponse])
async def list_fx_history(
    fx_service: Annotated[FxRateService, Depends(get_finance_fx_rate_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
):
    rows, total = await fx_service.list_user_rates(
        workspace_id, pagination.limit, pagination.offset
    )
    items = [
        UserFxRateResponse.model_validate({
            "id": r.id,
            "base_currency_code": r.base_currency_code,
            "quote_currency_code": r.quote_currency_code,
            "rate": r.rate,
            "as_of_date": r.as_of.date(),
            "created_at": r.created_at,
        })
        for r in rows
    ]
    return build_page(items, total, pagination)


@router.delete("/fx/history/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fx_history_row(
    row_id: int,
    fx_service: Annotated[FxRateService, Depends(get_finance_fx_rate_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await fx_service.delete_user_rate(workspace_id, row_id)


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
    return build_page(
        [CapitalTransferResponse.model_validate(t) for t in transfers], total, pagination
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
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    transfer = await transfer_service.create_transfer(
        workspace_id=workspace_id,
        actor_id=user["id"],
        transfer_in=transfer_in,
        audit_logger=audit_logger,
    )
    return CapitalTransferResponse.model_validate(transfer)


@router.patch("/transfers/{transfer_id}", response_model=CapitalTransferResponse)
async def update_transfer(
    transfer_id: uuid.UUID,
    transfer_in: CapitalTransferUpdate,
    transfer_service: Annotated[CapitalTransferService, Depends(get_finance_transfer_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    transfer = await transfer_service.update_transfer(
        workspace_id=workspace_id,
        actor_id=user["id"],
        public_id=transfer_id,
        transfer_in=transfer_in,
    )
    return CapitalTransferResponse.model_validate(transfer)


@router.delete("/transfers/{transfer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_transfer(
    transfer_id: uuid.UUID,
    transfer_service: Annotated[CapitalTransferService, Depends(get_finance_transfer_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await transfer_service.delete_transfer(workspace_id, transfer_id)


@router.get("/net-worth", response_model=NetWorthResponse)
async def get_net_worth(
    net_worth_service: Annotated[NetWorthService, Depends(get_finance_net_worth_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    data = await net_worth_service.get_net_worth(workspace_id)
    return NetWorthResponse.model_validate(data)


@router.get("/net-worth/history", response_model=list[NetWorthHistoryItem])
async def get_net_worth_history(
    net_worth_service: Annotated[NetWorthService, Depends(get_finance_net_worth_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    from_date: date | None = None,
    to_date: date | None = None,
):
    to_dt = to_date or datetime.now(UTC).date()
    from_dt = from_date or (to_dt - timedelta(days=90))

    if from_dt > to_dt:
        raise ValidationError(detail="from_date must not be after to_date")

    if (to_dt - from_dt).days > 365:
        from_dt = to_dt - timedelta(days=365)

    history = await net_worth_service.get_history(workspace_id, from_dt, to_dt)
    return [NetWorthHistoryItem.model_validate(h) for h in history]


@router.get(
    "/net-worth/history/user-points", response_model=PaginatedResponse[UserNetWorthPointResponse]
)
async def list_net_worth_user_points(
    net_worth_service: Annotated[NetWorthService, Depends(get_finance_net_worth_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
):
    rows, total = await net_worth_service.list_user_points(
        workspace_id, pagination.limit, pagination.offset
    )
    items = [UserNetWorthPointResponse.model_validate(r) for r in rows]
    return build_page(items, total, pagination)


@router.delete("/net-worth/history/user-points/{point_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_net_worth_user_point(
    point_id: int,
    net_worth_service: Annotated[NetWorthService, Depends(get_finance_net_worth_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await net_worth_service.delete_user_point(workspace_id, point_id)
