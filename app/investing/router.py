import uuid
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.audit import AuditLogger
from app.core.dependencies import (
    get_audit_logger,
    get_current_user,
    get_current_workspace_id,
    get_finance_account_service,
    get_investing_analytics_service,
    get_investing_cash_balance_service,
    get_investing_constituent_service,
    get_investing_dividend_service,
    get_investing_holding_service,
    get_investing_holding_verification_repo,
    get_investing_instrument_service,
    get_investing_order_service,
    get_investing_performance_service,
    get_investing_snapshot_repo,
    get_investing_summary_service,
    require_min_role,
)
from app.core.pagination import PaginatedResponse, PaginationParams, build_page
from app.finance.service import AccountService
from app.investing.order_service import InvestingOrderService
from app.investing.performance_service import InvestingSummaryService, PerformanceService
from app.investing.repository import HoldingVerificationRepository, PortfolioSnapshotRepository
from app.investing.schemas import (
    CashBalanceCreate,
    CashBalanceResponse,
    CashBalanceUpdate,
    CorporateActionCreate,
    CorporateActionResponse,
    DividendBulkImportRequest,
    DividendBulkImportResult,
    DividendCreate,
    DividendResponse,
    DividendUpdate,
    ExposureAnalyticsResponse,
    HoldingPriceBulkCreate,
    HoldingResponse,
    HoldingUpdate,
    HoldingVerificationResponse,
    InstrumentConstituentResponse,
    InstrumentConstituentUpsert,
    InstrumentCreate,
    InstrumentResponse,
    InstrumentUpdate,
    InvestingOrderBulkCreate,
    InvestingOrderCreate,
    InvestingOrderResponse,
    InvestingOrderUpdate,
    InvestingSummaryResponse,
    OverlapAnalyticsResponse,
    PerformanceSummaryResponse,
)
from app.investing.service import (
    CashBalanceService,
    ConstituentService,
    DividendService,
    ExposureAnalyticsService,
    HoldingService,
    InstrumentService,
)

router = APIRouter(prefix="/investing", tags=["investing"])


async def _build_account_cache(
    account_service: AccountService, workspace_id: int
) -> dict[int, tuple[uuid.UUID, str]]:
    accounts, _ = await account_service.list_accounts(workspace_id, limit=10000, offset=0)
    return {a.id: (a.public_id, a.name) for a in accounts if a.id is not None}


@router.get("/holdings", response_model=PaginatedResponse[HoldingResponse])
async def list_holdings(
    holding_service: Annotated[HoldingService, Depends(get_investing_holding_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
):
    detailed_items, total = await holding_service.list_holdings_with_details(
        workspace_id, pagination.limit, pagination.offset
    )
    return build_page(detailed_items, total, pagination)


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
    return await holding_service.update_holding_with_details(
        workspace_id,
        holding_id,
        holding_in,
        actor_id=user["id"],
        audit_logger=audit_logger,
    )


@router.delete("/holdings/{holding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_holding(
    holding_id: uuid.UUID,
    holding_service: Annotated[HoldingService, Depends(get_investing_holding_service)],
    snapshot_repo: Annotated[PortfolioSnapshotRepository, Depends(get_investing_snapshot_repo)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await holding_service.delete_holding(
        workspace_id, holding_id, actor_id=user["id"], audit_logger=audit_logger
    )
    await snapshot_repo.delete_for_date(workspace_id, datetime.now(UTC).date())


@router.get("/cash-balances", response_model=PaginatedResponse[CashBalanceResponse])
async def list_cash_balances(
    cash_service: Annotated[CashBalanceService, Depends(get_investing_cash_balance_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    account_id: Annotated[
        uuid.UUID | None,
        Query(description="Filter to a single account's full cash-balance history."),
    ] = None,
):
    account_internal_id: int | None = None
    if account_id is not None:
        account = await account_service.get_account(workspace_id, account_id)
        account_internal_id = account.id

    balances, total = await cash_service.list_cash_balances(
        workspace_id, pagination.limit, pagination.offset, account_id=account_internal_id
    )
    if not balances:
        return build_page([], total, pagination)

    account_cache = await _build_account_cache(account_service, workspace_id)
    items = []
    for c in balances:
        pub_id, name = account_cache.get(c.account_id, (None, "Unknown"))
        data = c.model_dump()
        data["account_id"] = pub_id
        data["account_name"] = name
        items.append(CashBalanceResponse.model_validate(data))
    return build_page(items, total, pagination)


@router.post(
    "/cash-balances", response_model=CashBalanceResponse, status_code=status.HTTP_201_CREATED
)
async def create_cash_balance(
    cash_in: CashBalanceCreate,
    cash_service: Annotated[CashBalanceService, Depends(get_investing_cash_balance_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    snapshot_repo: Annotated[PortfolioSnapshotRepository, Depends(get_investing_snapshot_repo)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    cash = await cash_service.create_cash_balance(
        user["id"], workspace_id, cash_in, audit_logger=audit_logger
    )
    await snapshot_repo.delete_for_date(workspace_id, datetime.now(UTC).date())
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
    snapshot_repo: Annotated[PortfolioSnapshotRepository, Depends(get_investing_snapshot_repo)],
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
    await snapshot_repo.delete_for_date(workspace_id, datetime.now(UTC).date())
    account = await account_service.account_repository.get_by_id(workspace_id, cash.account_id)
    data = cash.model_dump()
    data["account_id"] = account.public_id if account else None
    data["account_name"] = account.name if account else "Unknown"
    return CashBalanceResponse.model_validate(data)


@router.delete("/cash-balances/{cash_balance_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cash_balance(
    cash_balance_id: uuid.UUID,
    cash_service: Annotated[CashBalanceService, Depends(get_investing_cash_balance_service)],
    snapshot_repo: Annotated[PortfolioSnapshotRepository, Depends(get_investing_snapshot_repo)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await cash_service.delete_cash_balance(
        workspace_id, cash_balance_id, actor_id=user["id"], audit_logger=audit_logger
    )
    await snapshot_repo.delete_for_date(workspace_id, datetime.now(UTC).date())


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
    return await instrument_service.list_instruments_with_details(workspace_id)


@router.post("/instruments", response_model=InstrumentResponse, status_code=status.HTTP_201_CREATED)
async def create_instrument(
    payload: InstrumentCreate,
    instrument_service: Annotated[InstrumentService, Depends(get_investing_instrument_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    return await instrument_service.create_instrument_with_details(workspace_id, payload)


@router.patch("/instruments/{instrument_id}", response_model=InstrumentResponse)
async def update_instrument(
    instrument_id: uuid.UUID,
    payload: InstrumentUpdate,
    instrument_service: Annotated[InstrumentService, Depends(get_investing_instrument_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    return await instrument_service.update_instrument_with_details(
        workspace_id, instrument_id, payload
    )


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


def _order_response(
    order,
    account_cache: dict[int, tuple[uuid.UUID, str]],
    instrument_type_cache: dict[int, str] | None = None,
) -> InvestingOrderResponse:
    # account_id is required (non-nullable) on InvestingOrderResponse, so fall back
    # to a nil UUID rather than None to avoid a Pydantic validation 500.
    pub_id, name = account_cache.get(order.account_id, (uuid.UUID(int=0), "Unknown"))
    data = {
        "public_id": order.public_id,
        "account_id": pub_id,
        "account_name": name,
        "order_type": order.order_type,
        "symbol": order.symbol,
        "instrument_type": (
            instrument_type_cache.get(order.instrument_id)
            if instrument_type_cache and order.instrument_id is not None
            else None
        ),
        "quantity": order.quantity,
        "price_per_unit": order.price_per_unit,
        "gross_amount": order.gross_amount,
        "brokerage_fee": order.brokerage_fee,
        "tax_amount": order.tax_amount,
        "other_fees": order.other_fees,
        "net_amount": order.net_amount,
        "currency": order.currency,
        "exchange_name": order.exchange_name,
        "occurred_at": order.occurred_at,
        "notes": order.notes,
        "realized_gain_loss": order.realized_gain_loss,
        "avg_cost_at_sale": order.avg_cost_at_sale,
        "source_type": order.source_type,
        "created_at": order.created_at,
    }
    return InvestingOrderResponse.model_validate(data)


@router.post("/orders", response_model=InvestingOrderResponse, status_code=status.HTTP_201_CREATED)
async def place_order(
    order_in: InvestingOrderCreate,
    order_service: Annotated[InvestingOrderService, Depends(get_investing_order_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    snapshot_repo: Annotated[PortfolioSnapshotRepository, Depends(get_investing_snapshot_repo)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    order = await order_service.place_order(
        workspace_id=workspace_id,
        user_id=user["id"],
        order_in=order_in,
        audit_logger=audit_logger,
    )
    await snapshot_repo.delete_for_date(workspace_id, datetime.now(UTC).date())
    account_cache = await _build_account_cache(account_service, workspace_id)
    return _order_response(order, account_cache)


@router.get("/orders", response_model=PaginatedResponse[InvestingOrderResponse])
async def list_orders(
    order_service: Annotated[InvestingOrderService, Depends(get_investing_order_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    symbol: str | None = None,
    order_type: str | None = None,
    search: str | None = None,
):
    orders, total = await order_service.list_orders(
        workspace_id,
        pagination.limit,
        pagination.offset,
        symbol=symbol,
        order_type=order_type,
        search=search,
    )
    if not orders:
        return build_page([], total, pagination)
    account_cache = await _build_account_cache(account_service, workspace_id)
    items = [_order_response(o, account_cache) for o in orders]
    return build_page(items, total, pagination)


@router.get("/orders/by-holding/{symbol}", response_model=list[InvestingOrderResponse])
async def list_orders_for_holding(
    symbol: str,
    account_id: uuid.UUID,
    order_service: Annotated[InvestingOrderService, Depends(get_investing_order_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    account = await account_service.account_repository.get_by_public_id(workspace_id, account_id)
    if not account or account.id is None:
        return []
    orders = await order_service.list_orders_for_holding(workspace_id, symbol, account.id)
    account_cache = await _build_account_cache(account_service, workspace_id)
    return [_order_response(o, account_cache) for o in orders]


@router.get("/orders/{order_id}", response_model=InvestingOrderResponse)
async def get_order(
    order_id: uuid.UUID,
    order_service: Annotated[InvestingOrderService, Depends(get_investing_order_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    order = await order_service.get_order(workspace_id, order_id)
    account_cache = await _build_account_cache(account_service, workspace_id)
    return _order_response(order, account_cache)


@router.delete("/orders/{order_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_order(
    order_id: uuid.UUID,
    order_service: Annotated[InvestingOrderService, Depends(get_investing_order_service)],
    snapshot_repo: Annotated[PortfolioSnapshotRepository, Depends(get_investing_snapshot_repo)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await order_service.delete_order(
        workspace_id=workspace_id,
        user_id=user["id"],
        order_public_id=order_id,
        audit_logger=audit_logger,
    )
    await snapshot_repo.delete_for_date(workspace_id, datetime.now(UTC).date())


@router.patch("/orders/{order_id}", response_model=InvestingOrderResponse)
async def update_order(
    order_id: uuid.UUID,
    order_in: InvestingOrderUpdate,
    order_service: Annotated[InvestingOrderService, Depends(get_investing_order_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    snapshot_repo: Annotated[PortfolioSnapshotRepository, Depends(get_investing_snapshot_repo)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    order = await order_service.update_order(
        workspace_id=workspace_id,
        user_id=user["id"],
        order_public_id=order_id,
        order_update=order_in,
        audit_logger=audit_logger,
    )
    await snapshot_repo.delete_for_date(workspace_id, datetime.now(UTC).date())
    account_cache = await _build_account_cache(account_service, workspace_id)
    return _order_response(order, account_cache)


@router.post(
    "/orders/bulk", response_model=list[InvestingOrderResponse], status_code=status.HTTP_201_CREATED
)
async def bulk_import_orders(
    payload: InvestingOrderBulkCreate,
    order_service: Annotated[InvestingOrderService, Depends(get_investing_order_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    snapshot_repo: Annotated[PortfolioSnapshotRepository, Depends(get_investing_snapshot_repo)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    orders_in = [
        InvestingOrderCreate(
            account_id=payload.account_id,
            order_type=o.order_type,
            symbol=o.symbol,
            quantity=o.quantity,
            price_per_unit=o.price_per_unit,
            currency=o.currency,
            brokerage_fee=o.brokerage_fee,
            tax_amount=o.tax_amount,
            other_fees=o.other_fees,
            exchange_name=o.exchange_name,
            occurred_at=o.occurred_at,
            notes=o.notes,
        )
        for o in payload.orders
    ]
    created = await order_service.bulk_import_orders(
        workspace_id=workspace_id,
        user_id=user["id"],
        orders=orders_in,
        source_import_id=None,
        audit_logger=audit_logger,
    )
    await snapshot_repo.delete_for_date(workspace_id, datetime.now(UTC).date())
    account_cache = await _build_account_cache(account_service, workspace_id)
    return [_order_response(o, account_cache) for o in created]


def _corporate_action_response(
    action, account_cache: dict[int, tuple[uuid.UUID, str]]
) -> CorporateActionResponse:
    pub_id, name = account_cache.get(action.account_id, (uuid.UUID(int=0), "Unknown"))
    data = {
        "public_id": action.public_id,
        "account_id": pub_id,
        "account_name": name,
        "symbol": action.symbol,
        "action_type": action.action_type,
        "ratio_base": action.ratio_base,
        "ratio_quote": action.ratio_quote,
        "ex_date": action.ex_date,
        "notes": action.notes,
        "created_at": action.created_at,
    }
    return CorporateActionResponse.model_validate(data)


@router.post(
    "/corporate-actions",
    response_model=CorporateActionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_corporate_action(
    action_in: CorporateActionCreate,
    order_service: Annotated[InvestingOrderService, Depends(get_investing_order_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    snapshot_repo: Annotated[PortfolioSnapshotRepository, Depends(get_investing_snapshot_repo)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    action = await order_service.create_corporate_action(
        workspace_id=workspace_id,
        user_id=user["id"],
        action_in=action_in,
        audit_logger=audit_logger,
    )
    await snapshot_repo.delete_for_date(workspace_id, datetime.now(UTC).date())
    account_cache = await _build_account_cache(account_service, workspace_id)
    return _corporate_action_response(action, account_cache)


@router.get("/corporate-actions", response_model=PaginatedResponse[CorporateActionResponse])
async def list_corporate_actions(
    order_service: Annotated[InvestingOrderService, Depends(get_investing_order_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    symbol: str | None = None,
    account_id: uuid.UUID | None = None,
):
    account_internal_id: int | None = None
    if account_id is not None:
        account = await account_service.account_repository.get_by_public_id(
            workspace_id, account_id
        )
        if not account or account.id is None:
            return build_page([], 0, pagination)
        account_internal_id = account.id
    actions, total = await order_service.list_corporate_actions(
        workspace_id,
        pagination.limit,
        pagination.offset,
        symbol=symbol,
        account_id=account_internal_id,
    )
    if not actions:
        return build_page([], total, pagination)
    account_cache = await _build_account_cache(account_service, workspace_id)
    items = [_corporate_action_response(a, account_cache) for a in actions]
    return build_page(items, total, pagination)


@router.delete("/corporate-actions/{action_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_corporate_action(
    action_id: uuid.UUID,
    order_service: Annotated[InvestingOrderService, Depends(get_investing_order_service)],
    snapshot_repo: Annotated[PortfolioSnapshotRepository, Depends(get_investing_snapshot_repo)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await order_service.delete_corporate_action(
        workspace_id=workspace_id,
        user_id=user["id"],
        action_public_id=action_id,
        audit_logger=audit_logger,
    )
    await snapshot_repo.delete_for_date(workspace_id, datetime.now(UTC).date())


def _dividend_response(dividend, account) -> DividendResponse:
    return DividendResponse.model_validate({
        "public_id": dividend.public_id,
        "account_id": account.public_id,
        "account_name": account.name,
        "holding_id": None,
        "symbol": dividend.symbol,
        "income_type": dividend.income_type,
        "gross_amount": dividend.gross_amount,
        "tax_withheld": dividend.tax_withheld,
        "net_amount": dividend.net_amount,
        "currency": dividend.currency,
        "pay_date": dividend.pay_date,
        "external_ref": dividend.external_ref,
        "notes": dividend.notes,
        "created_at": dividend.created_at,
        "updated_at": dividend.updated_at,
    })


@router.post("/dividends", response_model=DividendResponse, status_code=status.HTTP_201_CREATED)
async def create_dividend(
    dividend_in: DividendCreate,
    dividend_service: Annotated[DividendService, Depends(get_investing_dividend_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    dividend, account = await dividend_service.create_dividend(
        workspace_id=workspace_id,
        user_id=user["id"],
        dividend_in=dividend_in,
        audit_logger=audit_logger,
    )
    return _dividend_response(dividend, account)


@router.get("/dividends", response_model=PaginatedResponse[DividendResponse])
async def list_dividends(
    dividend_service: Annotated[DividendService, Depends(get_investing_dividend_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    symbol: str | None = None,
    account_id: uuid.UUID | None = None,
):
    rows, total, accounts = await dividend_service.list_dividends(
        workspace_id, pagination.limit, pagination.offset, account_id=account_id, symbol=symbol
    )
    items = [
        _dividend_response(d, accounts[d.account_id]) for d in rows if d.account_id in accounts
    ]
    return build_page(items, total, pagination)


@router.get("/dividends/{dividend_id}", response_model=DividendResponse)
async def get_dividend(
    dividend_id: uuid.UUID,
    dividend_service: Annotated[DividendService, Depends(get_investing_dividend_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    dividend, account = await dividend_service.get_dividend(workspace_id, dividend_id)
    return _dividend_response(dividend, account)


@router.patch("/dividends/{dividend_id}", response_model=DividendResponse)
async def update_dividend(
    dividend_id: uuid.UUID,
    dividend_in: DividendUpdate,
    dividend_service: Annotated[DividendService, Depends(get_investing_dividend_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    dividend, account = await dividend_service.update_dividend(
        workspace_id=workspace_id,
        public_id=dividend_id,
        dividend_in=dividend_in,
        actor_id=user["id"],
        audit_logger=audit_logger,
    )
    return _dividend_response(dividend, account)


@router.delete("/dividends/{dividend_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dividend(
    dividend_id: uuid.UUID,
    dividend_service: Annotated[DividendService, Depends(get_investing_dividend_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await dividend_service.delete_dividend(
        workspace_id=workspace_id,
        public_id=dividend_id,
        actor_id=user["id"],
        audit_logger=audit_logger,
    )


@router.post("/dividends/bulk", response_model=DividendBulkImportResult)
async def bulk_import_dividends(
    request: DividendBulkImportRequest,
    dividend_service: Annotated[DividendService, Depends(get_investing_dividend_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    return await dividend_service.bulk_import(
        workspace_id=workspace_id,
        user_id=user["id"],
        request=request,
        audit_logger=audit_logger,
    )


def _holding_verification_response(
    verification, account_cache: dict[int, tuple[uuid.UUID, str]]
) -> HoldingVerificationResponse:
    pub_id, name = account_cache.get(verification.account_id, (uuid.UUID(int=0), "Unknown"))
    data = {
        "public_id": verification.public_id,
        "account_id": pub_id,
        "account_name": name,
        "source": verification.source,
        "statement_date": verification.statement_date,
        "match_count": verification.match_count,
        "quantity_drift_count": verification.quantity_drift_count,
        "missing_in_lifestack_count": verification.missing_in_lifestack_count,
        "missing_at_depository_count": verification.missing_at_depository_count,
        "report": verification.report_json,
        "created_at": verification.created_at,
    }
    return HoldingVerificationResponse.model_validate(data)


@router.get("/holding-verifications", response_model=PaginatedResponse[HoldingVerificationResponse])
async def list_holding_verifications(
    verification_repo: Annotated[
        HoldingVerificationRepository, Depends(get_investing_holding_verification_repo)
    ],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    account_id: uuid.UUID | None = None,
):
    """Depository-vs-Lifestack holdings verification history (spec-060).

    Written by Demat CAS import commits; read-only from this endpoint —
    there is no create/update/delete surface here beyond the import flow.
    """
    account_internal_id: int | None = None
    if account_id is not None:
        account = await account_service.account_repository.get_by_public_id(
            workspace_id, account_id
        )
        if not account or account.id is None:
            return build_page([], 0, pagination)
        account_internal_id = account.id
    verifications, total = await verification_repo.list_by_workspace(
        workspace_id,
        pagination.limit,
        pagination.offset,
        account_id=account_internal_id,
    )
    if not verifications:
        return build_page([], total, pagination)
    account_cache = await _build_account_cache(account_service, workspace_id)
    items = [_holding_verification_response(v, account_cache) for v in verifications]
    return build_page(items, total, pagination)
