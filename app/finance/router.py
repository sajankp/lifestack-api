import uuid
from datetime import UTC, datetime
from decimal import Decimal
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
    get_finance_fx_rate_repo,
    get_finance_fx_rate_service,
    get_finance_setting_repo,
    get_finance_setting_service,
    get_finance_transfer_service,
    get_investing_cash_balance_repo,
    get_investing_summary_service,
    require_min_role,
)
from app.core.exceptions import NotFoundError
from app.core.pagination import PaginatedResponse, PaginationParams
from app.finance.models import AccountType, CurrencyDisplayPreference
from app.finance.repository import FinanceSettingRepository, FxRateRepository
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
    InvestingAccountBalance,
    NetWorthResponse,
    ReconciliationSummary,
    SpendingAccountBalance,
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
from app.investing.repository import CashBalanceRepository
from app.investing.service import InvestingSummaryService

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
    - ``projected_balance``: income - expenses + transfer_in - transfer_out (all-time)
    - ``snapshot_balance``: the most recent investing cash balance snapshot, or null
    - ``discrepancy``: projected - snapshot (positive = ledger > snapshot, negative = snapshot > ledger)
    - ``transaction_count`` / ``transfer_count``: entry breakdown

    A discrepancy indicates unrecorded transactions or transfers on one side.
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
            updated_at=datetime.now(UTC),
        )
    return WorkspaceFinanceSettingResponse.model_validate(row)


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


def _convert_to_reporting(
    amount: Decimal,
    currency: str,
    reporting_currency: str,
    fx_lookup: dict[tuple[str, str], object],
) -> Decimal | None:
    if currency == reporting_currency:
        return amount
    direct = fx_lookup.get((currency, reporting_currency))
    if direct is not None:
        return amount * direct.rate  # type: ignore[attr-defined]
    inverse = fx_lookup.get((reporting_currency, currency))
    if inverse is not None and inverse.rate:  # type: ignore[attr-defined]
        return amount / inverse.rate  # type: ignore[attr-defined]
    return None


@router.get("/net-worth", response_model=NetWorthResponse)
async def get_net_worth(
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    summary_service: Annotated[InvestingSummaryService, Depends(get_investing_summary_service)],
    cash_balance_repo: Annotated[CashBalanceRepository, Depends(get_investing_cash_balance_repo)],
    setting_repo: Annotated[FinanceSettingRepository, Depends(get_finance_setting_repo)],
    fx_rate_repo: Annotated[FxRateRepository, Depends(get_finance_fx_rate_repo)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    accounts, _ = await account_service.list_accounts(workspace_id, limit=10000, offset=0)

    # Get reporting currency from workspace settings
    ws_settings = await setting_repo.get_by_workspace(workspace_id)
    reporting_currency: str | None = None
    if ws_settings and ws_settings.reporting_currency_code:
        reporting_currency = ws_settings.reporting_currency_code.upper()

    # Resolve per-account spending balances — brokerage accounts are excluded because
    # their cash is already captured in investing_cash_total; including them here
    # would double-count and also mis-display cross-currency inflows (gross vs net issue).
    # Uses a bulk query (3 SQL statements) instead of N per-account round-trips.
    spending_accounts_list = [a for a in accounts if a.account_type != AccountType.brokerage]
    raw_balances: list[dict] = await account_service.get_spending_balances_bulk(
        workspace_id, spending_accounts_list
    )

    # Per-(brokerage account, currency) investing cash for the breakdown table.
    # This itemizes what investing_cash_total already aggregates — no double count.
    brokerage_by_id = {a.id: a for a in accounts if a.account_type == AccountType.brokerage}
    cash_rows = [
        c
        for c in await cash_balance_repo.get_latest_per_account_currency(workspace_id)
        if c.account_id in brokerage_by_id
    ]

    # Build FX lookup covering both spending and investing cash currencies → reporting
    fx_lookup: dict[tuple[str, str], object] = {}
    if reporting_currency:
        currencies = {b["currency_code"].upper() for b in raw_balances} | {
            c.currency.upper() for c in cash_rows
        }
        foreign = {c for c in currencies if c != reporting_currency}
        if foreign:
            pairs = [(c, reporting_currency) for c in foreign] + [
                (reporting_currency, c) for c in foreign
            ]
            fx_lookup = await fx_rate_repo.get_latest_rates_for_pairs(pairs)

    # Assemble spending account list and total
    spending_accounts: list[SpendingAccountBalance] = []
    spending_total = Decimal("0")
    spending_convertible = True
    for data in raw_balances:
        balance: Decimal = data["spending_balance"]
        currency: str = data["currency_code"]
        balance_in_rc: Decimal | None = None
        if reporting_currency:
            balance_in_rc = _convert_to_reporting(balance, currency, reporting_currency, fx_lookup)
            if balance_in_rc is None:
                spending_convertible = False
            else:
                spending_total += balance_in_rc
        spending_accounts.append(
            SpendingAccountBalance(
                account_public_id=data["account_public_id"],
                account_name=data["account_name"],
                account_type=data["account_type"],
                currency_code=currency,
                balance=balance,
                balance_in_reporting_currency=balance_in_rc,
            )
        )

    # Get investing totals (already FX-converted to reporting currency by summary service)
    investing_summary = await summary_service.get_summary(workspace_id)
    investing_cash = investing_summary.cash_total
    holdings_value = investing_summary.portfolio_value
    effective_reporting = reporting_currency or investing_summary.reporting_currency

    investing_total: Decimal | None = None
    if investing_cash is not None and holdings_value is not None:
        investing_total = investing_cash + holdings_value

    # Build the per-account investing cash breakdown
    investing_accounts: list[InvestingAccountBalance] = []
    for cash in cash_rows:
        account = brokerage_by_id[cash.account_id]
        currency_code = cash.currency.upper()
        balance_in_rc = (
            _convert_to_reporting(cash.balance, currency_code, reporting_currency, fx_lookup)
            if reporting_currency
            else None
        )
        investing_accounts.append(
            InvestingAccountBalance(
                account_public_id=account.public_id,
                account_name=account.name,
                currency_code=currency_code,
                balance=cash.balance,
                balance_in_reporting_currency=balance_in_rc,
            )
        )
    investing_accounts.sort(key=lambda a: (a.account_name.lower(), a.currency_code))

    total_net_worth: Decimal | None = None
    if spending_convertible and reporting_currency and investing_total is not None:
        total_net_worth = spending_total + investing_total
    elif (
        spending_convertible
        and reporting_currency
        and not raw_balances
        and investing_total is not None
    ):
        total_net_worth = investing_total

    # Determine valuation status
    has_any_data = bool(raw_balances) or investing_summary.holdings_count > 0
    if not has_any_data:
        valuation_status = "empty"
    elif total_net_worth is not None:
        valuation_status = "ok"
    elif not effective_reporting:
        valuation_status = "no_reporting_currency"
    else:
        valuation_status = "partial"

    return NetWorthResponse(
        reporting_currency=effective_reporting,
        spending_accounts=spending_accounts,
        spending_total=spending_total if (spending_convertible and reporting_currency) else None,
        investing_accounts=investing_accounts,
        investing_cash_total=investing_cash,
        holdings_value=holdings_value,
        investing_total=investing_total,
        total_net_worth=total_net_worth,
        valuation_status=valuation_status,
        fx_as_of=investing_summary.fx_as_of,
    )
