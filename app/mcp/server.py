"""Authenticated MCP tools and resources for Lifestack."""

import decimal
import json
import re
import uuid
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import structlog
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from pydantic import ValidationError as PydanticValidationError

from app.auth.models import User
from app.auth.repository import UserRepository
from app.capture.spending_batch import local_day_range_utc
from app.capture.tools import AgentTools, _safe_timezone
from app.config import settings
from app.core.audit import AuditLogger, AuditLogRepository, redact_details
from app.core.currency import (
    build_required_pairs,
    conversion_rate,
    convert_amount,
    effective_display_as_of,
    fx_rates_used,
)
from app.core.database import postgres
from app.core.exceptions import APIError, ValidationError
from app.core.exceptions import ValidationError as AppValidationError
from app.finance.lrs_service import LrsService
from app.finance.models import AccountType
from app.finance.repository import (
    AccountRepository,
    CapitalTransferRepository,
    CurrencyRepository,
    FinanceSettingRepository,
    FxRateRepository,
    NetWorthSnapshotRepository,
)
from app.finance.service import AccountService, FxRateService, NetWorthService
from app.health.repository import (
    MedicationEventRepository,
    MedicationRepository,
    WeightEntryRepository,
)
from app.health.service import HealthService
from app.investing.company_sector_service import CompanySectorService, SectorRowError
from app.investing.compliance_service import HoldingComplianceService
from app.investing.order_service import InvestingOrderService
from app.investing.performance_report import (
    VALID_PERIODS,
    build_performance_report,
    period_start,
)
from app.investing.performance_service import InvestingSummaryService, PerformanceService
from app.investing.reconciliation import (
    HoldingReconciliationService,
    ReconciliationInputError,
    parse_external_list,
    parse_tolerances,
)
from app.investing.repository import (
    CashBalanceRepository,
    CompanyRepository,
    CorporateActionRepository,
    DividendRepository,
    HoldingComplianceRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InstrumentConstituentRepository,
    InstrumentRepository,
    InvestingOrderRepository,
    LotRepository,
    PortfolioSnapshotRepository,
    ReferenceSecurityRepository,
)
from app.investing.return_metrics_service import ReturnMetricsService
from app.investing.schemas import (
    CompanySectorRow,
    DividendCreate,
    DividendResponse,
    HoldingComplianceUpsert,
    InstrumentConstituentUpsert,
    InstrumentType,
    InvestingOrderCreate,
    InvestingOrderUpdate,
    OrderType,
)
from app.investing.service import (
    ConstituentService,
    DividendService,
    ExposureAnalyticsService,
    HoldingService,
    InstrumentService,
    PortfolioReportService,
)
from app.investing.tax.exchanges import resolve_exchange
from app.investing.tax_service import TaxService, normalize_currency_code, parse_fx_entries
from app.investing.tax_whatif_service import (
    TaxWhatIfService,
    WhatIfError,
    parse_decimal,
    parse_sale_date,
    resolve_zone,
)
from app.investing.tax_whatif_views import (
    harvest_response,
    lot_listing_response,
    simulation_response,
)
from app.investing.theme_repository import (
    HoldingThemeTagRepository,
    LatestConstituentSnapshotRepository,
)
from app.investing.theme_schemas import (
    GroupBy,
    HoldingThemeTagsInput,
    ThemeTagInput,
)
from app.investing.theme_service import HoldingThemeTagService, ThemeExposureService
from app.mcp.auth import LifestackTokenVerifier
from app.mcp.security import authorize_user, authorize_workspace, validate_page
from app.platform.repository import MembershipRepository, WorkspaceRepository
from app.spending.repository import (
    BudgetRepository,
    CategoryGroupRepository,
    CategoryRepository,
    TagRepository,
)
from app.spending.service import BudgetService
from app.todo.repository import TodoRepository
from app.todo.schemas import TodoCreate, TodoResponse
from app.todo.service import TodoService

_HOLDINGS_QUANTITY_STATES = {"all", "nonzero", "zero"}
_HOLDINGS_SORT_FIELDS = {
    "symbol",
    "quantity",
    "current_value",
    "book_value",
    "gain_loss",
    "gain_loss_pct",
    "created_at",
    "updated_at",
}
_HOLDINGS_VALUE_SORT_FIELDS = {
    "current_value",
    "book_value",
    "gain_loss",
    "gain_loss_pct",
}
_HOLDINGS_INSTRUMENT_TYPES = {"stock", "etf", "mutual_fund"}


async def _run_capture_tool(
    workspace_id: int,
    required_scope: str,
    tool_name: str,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Run a voice-compatible tool behind MCP's workspace and scope checks."""
    async with postgres.async_session_maker() as session:
        user_id = await authorize_workspace(
            session,
            workspace_id,
            required_scope=required_scope,
            tool=tool_name,
        )
        user = await session.get(User, user_id)
        agent_tools = AgentTools(
            session=session,
            user_id=user_id,
            workspace_id=workspace_id,
            user_timezone=(user.timezone if user and user.timezone else "UTC"),
            source_channel="mcp_agent",
        )
        result = await getattr(agent_tools, tool_name)(**kwargs)
        if result.get("status") == "success":
            await session.commit()
        else:
            await session.rollback()
        return result


logger = structlog.get_logger(__name__)


def _holding_service(session: Any) -> HoldingService:
    """Build the investing holding service with the same repositories as REST."""
    return HoldingService(
        HoldingRepository(session),
        InstrumentRepository(session),
        CompanyRepository(session),
        AccountRepository(session),
        CurrencyRepository(session),
        HoldingPriceRepository(session),
        InvestingOrderRepository(session),
    )


def _constituent_service(session: Any) -> ConstituentService:
    return ConstituentService(
        InstrumentRepository(session),
        CompanyRepository(session),
        InstrumentConstituentRepository(session),
    )


def _analytics_service(session: Any) -> ExposureAnalyticsService:
    return ExposureAnalyticsService(
        HoldingRepository(session),
        InstrumentRepository(session),
        CompanyRepository(session),
        InstrumentConstituentRepository(session),
        HoldingPriceRepository(session),
        FinanceSettingRepository(session),
        FxRateRepository(session),
    )


def _company_sector_service(session: Any) -> CompanySectorService:
    return CompanySectorService(
        HoldingRepository(session),
        InstrumentRepository(session),
        CompanyRepository(session),
    )


def _compliance_service(session: Any) -> HoldingComplianceService:
    return HoldingComplianceService(
        HoldingRepository(session),
        HoldingComplianceRepository(session),
        DividendRepository(session),
    )


def _theme_exposure_service(session: Any) -> ThemeExposureService:
    return ThemeExposureService(
        HoldingRepository(session),
        InstrumentRepository(session),
        CompanyRepository(session),
        LatestConstituentSnapshotRepository(session),
        HoldingPriceRepository(session),
        FinanceSettingRepository(session),
        FxRateRepository(session),
        HoldingThemeTagRepository(session),
    )


def _theme_tag_service(session: Any) -> HoldingThemeTagService:
    return HoldingThemeTagService(
        HoldingThemeTagRepository(session),
    )


def _dividend_service(session: Any) -> DividendService:
    return DividendService(
        DividendRepository(session),
        CashBalanceRepository(session),
        AccountRepository(session),
        HoldingRepository(session),
        CurrencyRepository(session),
    )


async def _possible_duplicate_orders(
    session: Any, workspace_id: int, user_id: int, payload: InvestingOrderCreate
) -> list[Any]:
    """Orders that look like ``payload``: same account + symbol + side + qty + price
    on the same workspace-local calendar day. Used for the ``possible_duplicate``
    warning (orders without a ``source_ref``); never to skip a write silently.
    """
    account = await AccountRepository(session).get_by_public_id(workspace_id, payload.account_id)
    if account is None or account.id is None:
        return []
    user = await session.get(User, user_id)
    tz = _safe_timezone(user.timezone if user and user.timezone else "UTC")
    occurred_at = payload.occurred_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    local_day = occurred_at.astimezone(tz).date()
    day_start, day_end = local_day_range_utc(local_day, local_day, tz)
    return list(
        await InvestingOrderRepository(session).find_possible_duplicate_orders(
            workspace_id,
            account_id=account.id,
            symbol=payload.symbol,
            order_type=payload.order_type.value,
            quantity=payload.quantity,
            price_per_unit=payload.price_per_unit,
            from_date=day_start,
            to_date=day_end,
        )
    )


async def _source_ref_replay_mismatches(
    session: Any, workspace_id: int, existing: Any, payload: InvestingOrderCreate
) -> list[str]:
    """Fields on which a ``source_ref`` replay differs from the order it matches.

    A retry must be byte-for-byte the same order. If the caller reuses a
    ``source_ref`` for a *different* order, reporting success would make it
    believe the new order exists while nothing was recorded.
    """
    account = await AccountRepository(session).get_by_id(workspace_id, existing.account_id)
    occurred_at = payload.occurred_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    comparisons = {
        "account_id": account is not None and account.public_id == payload.account_id,
        "order_type": existing.order_type == payload.order_type.value,
        "symbol": existing.symbol == payload.symbol,
        "quantity": existing.quantity == payload.quantity,
        "price_per_unit": existing.price_per_unit == payload.price_per_unit,
        "currency": existing.currency == payload.currency,
        "occurred_at": existing.occurred_at == occurred_at,
    }
    return [name for name, same in comparisons.items() if not same]


def _source_ref_conflict_message(source_ref: str, fields: list[str]) -> str:
    return (
        f"source_ref '{source_ref}' already belongs to a different order "
        f"(differs in: {', '.join(fields)}). Nothing was recorded. Use a new "
        "source_ref for a new order, or resend the original details to retry it."
    )


def _order_preview_fields(payload: InvestingOrderCreate, gross: Decimal, net: Decimal) -> dict:
    return {
        "entity_type": "investing_order",
        "account_id": str(payload.account_id),
        "order_type": payload.order_type.value,
        "symbol": payload.symbol,
        "quantity": str(payload.quantity),
        "price_per_unit": str(payload.price_per_unit),
        "gross_amount": str(gross),
        "brokerage_fee": str(payload.brokerage_fee),
        "tax_amount": str(payload.tax_amount),
        "other_fees": str(payload.other_fees),
        "net_amount": str(net),
        "currency": payload.currency,
        "occurred_at": payload.occurred_at.isoformat(),
        "source_ref": payload.source_ref,
    }


def _order_service(session: Any) -> InvestingOrderService:
    return InvestingOrderService(
        InvestingOrderRepository(session),
        HoldingRepository(session),
        CashBalanceRepository(session),
        AccountRepository(session),
        CurrencyRepository(session),
        InstrumentService(InstrumentRepository(session), CompanyRepository(session)),
        LotRepository(session),
        CorporateActionRepository(session),
    )


MAX_PORTFOLIO_REPORT_CHARS = 2_000_000
MAX_LRS_HEADROOM_INR = Decimal("1e16")  # numeric(18,2) holds < 10^16


def _portfolio_report_service(session: Any) -> PortfolioReportService:
    return PortfolioReportService(session=session)


def _tax_service(session: Any) -> TaxService:
    return TaxService(
        session=session,
        order_repository=InvestingOrderRepository(session),
        reference_repository=ReferenceSecurityRepository(session),
        account_repository=AccountRepository(session),
        instrument_repository=InstrumentRepository(session),
        fx_rate_repository=FxRateRepository(session),
    )


def _tax_whatif_service(session: Any) -> TaxWhatIfService:
    return TaxWhatIfService(
        _tax_service(session),
        HoldingRepository(session),
        LotRepository(session),
        HoldingPriceRepository(session),
    )


async def _user_timezone_name(session: Any, user_id: int) -> str | None:
    user = await session.get(User, user_id)
    return user.timezone if user is not None and user.timezone else None


def _what_if_error(exc: WhatIfError) -> dict[str, Any]:
    return {"status": "error", "code": exc.code, "message": exc.message, **exc.details}


def _reconciliation_service(session: Any) -> HoldingReconciliationService:
    return HoldingReconciliationService(
        HoldingRepository(session),
        InstrumentRepository(session),
        CompanyRepository(session),
        AccountRepository(session),
    )


def _return_metrics_service(session: Any) -> ReturnMetricsService:
    return ReturnMetricsService(
        InvestingOrderRepository(session),
        HoldingRepository(session),
        HoldingPriceRepository(session),
        DividendRepository(session),
        AccountRepository(session),
        NetWorthSnapshotRepository(session),
        FxRateService(FxRateRepository(session), CurrencyRepository(session)),
        FinanceSettingRepository(session),
    )


def _performance_service(session: Any) -> PerformanceService:
    return PerformanceService(
        HoldingRepository(session),
        CashBalanceRepository(session),
        HoldingPriceRepository(session),
        PortfolioSnapshotRepository(session),
        FinanceSettingRepository(session),
        FxRateRepository(session),
        InstrumentRepository(session),
        AccountRepository(session),
        transfer_repo=CapitalTransferRepository(session),
    )


def _health_service(session: Any) -> HealthService:
    return HealthService(
        MedicationRepository(session),
        MedicationEventRepository(session),
        WeightEntryRepository(session),
    )


def _parse_iso_datetime(value: str | None, label: str) -> datetime | None:
    """ISO-8601 -> aware UTC datetime (naive input is read as UTC)."""
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 date or datetime.") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _order_response(order: Any, account: Any, instrument: Any | None = None) -> dict[str, Any]:
    return {
        "public_id": str(order.public_id),
        "account_id": str(account.public_id),
        "account_name": account.name,
        "order_type": str(order.order_type),
        "symbol": order.symbol,
        "instrument_type": instrument.instrument_type if instrument else None,
        "quantity": str(order.quantity),
        "price_per_unit": str(order.price_per_unit),
        "gross_amount": str(order.gross_amount),
        "brokerage_fee": str(order.brokerage_fee),
        "tax_amount": str(order.tax_amount),
        "other_fees": str(order.other_fees),
        "net_amount": str(order.net_amount),
        "currency": order.currency,
        "exchange_name": order.exchange_name,
        "occurred_at": order.occurred_at.isoformat(),
        "notes": order.notes,
        "realized_gain_loss": str(order.realized_gain_loss)
        if order.realized_gain_loss is not None
        else None,
        "avg_cost_at_sale": str(order.avg_cost_at_sale)
        if order.avg_cost_at_sale is not None
        else None,
        "source_type": order.source_type,
        "source_ref": order.source_ref,
        "created_at": order.created_at.isoformat(),
    }


def _decimal_string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _add_holding_reporting_valuation(
    data: dict[str, Any],
    holding: Any,
    price_row: Any,
    reporting_currency: str | None,
    fx_lookup: dict[tuple[str, str], Any],
    fx_as_of: datetime | None,
) -> None:
    """Add explicit reporting-currency values without changing REST fields."""
    quantity = Decimal(str(holding.quantity))
    avg_cost = Decimal(str(holding.avg_cost))
    native_currency = holding.currency.upper()
    book_value = quantity * avg_cost
    market_value = quantity * Decimal(str(price_row.unit_price)) if price_row else None

    data["price_available"] = price_row is not None
    data["price_as_of"] = price_row.price_date.isoformat() if price_row else None
    data["price_source"] = price_row.source if price_row else None
    data["reporting_currency"] = reporting_currency
    data["fx_as_of"] = fx_as_of.isoformat() if fx_as_of else None

    if quantity == 0:
        data.update({
            "reporting_current_value": "0",
            "reporting_book_value": "0",
            "reporting_gain_loss": "0",
            "reporting_gain_loss_pct": "0",
            "valuation_status": "zero_quantity",
        })
        return

    if reporting_currency is None:
        data.update({
            "reporting_current_value": None,
            "reporting_book_value": None,
            "reporting_gain_loss": None,
            "reporting_gain_loss_pct": None,
            "valuation_status": "reporting_currency_required",
        })
        return

    converted_book = convert_amount(book_value, native_currency, reporting_currency, fx_lookup)
    converted_market = (
        convert_amount(market_value, native_currency, reporting_currency, fx_lookup)
        if market_value is not None
        else None
    )
    if converted_book is None:
        status = "missing_fx"
    elif converted_market is None:
        status = "missing_price"
    else:
        status = "current"

    converted_gain_loss = (
        converted_market - converted_book
        if converted_market is not None and converted_book is not None
        else None
    )
    gain_loss_pct = (
        converted_gain_loss / converted_book * Decimal("100")
        if converted_gain_loss is not None and converted_book
        else (Decimal("0") if converted_gain_loss == 0 else None)
    )
    data.update({
        "reporting_current_value": _decimal_string(converted_market),
        "reporting_book_value": _decimal_string(converted_book),
        "reporting_gain_loss": _decimal_string(converted_gain_loss),
        "reporting_gain_loss_pct": _decimal_string(gain_loss_pct),
        "valuation_status": status,
    })


def _sort_holding_items(
    items: list[dict[str, Any]],
    sort_by: str,
    sort_direction: str,
    use_reporting_values: bool,
) -> list[dict[str, Any]]:
    reporting_fields = {
        "current_value": "reporting_current_value",
        "book_value": "reporting_book_value",
        "gain_loss": "reporting_gain_loss",
        "gain_loss_pct": "reporting_gain_loss_pct",
    }
    field = reporting_fields.get(sort_by, sort_by) if use_reporting_values else sort_by

    def value(item: dict[str, Any]) -> Decimal | str | None:
        raw = item.get(field)
        if raw is None:
            return None
        if sort_by in _HOLDINGS_VALUE_SORT_FIELDS:
            return Decimal(str(raw))
        return str(raw)

    present = [item for item in items if value(item) is not None]
    missing = [item for item in items if value(item) is None]
    present.sort(key=lambda item: str(item["public_id"]))
    present.sort(key=lambda item: value(item), reverse=sort_direction == "desc")  # type: ignore[arg-type]
    missing.sort(key=lambda item: str(item["public_id"]))
    return present + missing


def _dividend_response(dividend: Any, account: Any) -> dict[str, Any]:
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
    }).model_dump(mode="json")


async def _load_workspace_reference_data(
    session: Any, workspace_id: int, user_id: int
) -> dict[str, Any]:
    """Load the bounded, read-only vocabulary shared by MCP and voice clients."""
    user = await session.get(User, user_id)
    categories, _ = await CategoryRepository(session).get_all(workspace_id, limit=50, offset=0)
    spending_tags, _ = await TagRepository(session).get_all(workspace_id, limit=50, offset=0)
    accounts, _ = await AccountRepository(session).list_workspace_accounts(
        workspace_id, limit=20, offset=0
    )
    setting = await FinanceSettingRepository(session).get_by_workspace(workspace_id)
    medications = await MedicationRepository(session).get_active(workspace_id)
    default_account_id = setting.default_spending_account_id if setting else None
    return {
        "workspace_id": workspace_id,
        "timezone": user.timezone if user and user.timezone else "UTC",
        "categories": [
            {"public_id": str(category.public_id), "name": category.name} for category in categories
        ],
        "accounts": [
            {
                "public_id": str(account.public_id),
                "name": account.name,
                # Stored as a plain string column, so it is not always an Enum instance.
                "account_type": AccountType(account.account_type).value,
                "currency_code": account.default_currency_code,
                "is_default_spending_account": account.id == default_account_id,
            }
            for account in accounts
            if account.is_active and account.account_type != AccountType.brokerage
        ],
        "tags": [{"public_id": str(tag.public_id), "name": tag.name} for tag in spending_tags],
        "medications": [
            {"public_id": str(medication.public_id), "name": medication.name}
            for medication in medications[:30]
        ],
    }


async def _load_workspace_choices(session: Any, user_id: int) -> list[dict[str, Any]]:
    """List only active workspaces the authenticated MCP user can use."""
    workspaces = await WorkspaceRepository(session).list_user_workspaces(user_id)
    memberships = await MembershipRepository(session).list_user_memberships(user_id)
    roles = {
        membership.workspace_id: (
            membership.role.value if hasattr(membership.role, "value") else str(membership.role)
        )
        for membership in memberships
    }

    token = get_access_token()
    selected_workspace_id: int | None = None
    if token is not None:
        raw_selected = (token.claims or {}).get("default_workspace_id")
        try:
            selected_workspace_id = int(raw_selected) if raw_selected is not None else None
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            selected_workspace_id = None

    active_workspaces = [workspace for workspace in workspaces if workspace.is_active]
    if selected_workspace_id is None and len(active_workspaces) == 1:
        selected_workspace_id = active_workspaces[0].id

    return [
        {
            "workspace_id": workspace.id,
            "public_id": str(workspace.public_id),
            "name": workspace.name,
            "description": workspace.description,
            "role": roles.get(workspace.id),
            "is_current": workspace.id == selected_workspace_id,
        }
        for workspace in active_workspaces
    ]


def create_mcp_server() -> FastMCP:
    mcp = FastMCP("Lifestack", auth=LifestackTokenVerifier())

    @mcp.tool
    async def create_todo(
        workspace_id: int, title: str, description: str | None = None, due_date: str | None = None
    ) -> dict[str, Any]:
        """Create a todo in a workspace the authenticated user belongs to."""
        async with postgres.async_session_maker() as session, session.begin():
            user_id = await authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="create_todo"
            )
            due = datetime.fromisoformat(due_date) if due_date else None
            todo = await TodoService(TodoRepository(session)).create_todo(
                user_id,
                workspace_id,
                TodoCreate(title=title, description=description, due_date=due),
            )
            return TodoResponse.model_validate(todo).model_dump()

    @mcp.tool
    async def list_todos(
        workspace_id: int, completed: bool | None = None, limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        """List todos for an authorized workspace."""
        validate_page(limit, offset)
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_todos"
            )
            todos, total = await TodoService(TodoRepository(session)).list_todos(
                workspace_id, completed, limit, offset
            )
            return {
                "items": [TodoResponse.model_validate(t).model_dump() for t in todos],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    @mcp.tool
    async def get_todo_summary(workspace_id: int) -> dict[str, Any]:
        """Get todo counts and due items for an authorized workspace."""
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_todo_summary"
            )
            service = TodoService(TodoRepository(session))
            now = datetime.now(UTC)
            pending, completed = await service.get_summary_counts(workspace_id, now)
            overdue = await service.get_overdue_items(workspace_id, now, limit=5)
            next_due = await service.get_next_due_items(workspace_id, now, limit=5)
            return {
                "workspace_id": workspace_id,
                "pending": pending,
                "completed": completed,
                "overdue_count": len(overdue),
                "overdue_items": [TodoResponse.model_validate(t).model_dump() for t in overdue],
                "next_due_items": [TodoResponse.model_validate(t).model_dump() for t in next_due],
            }

    @mcp.tool
    async def get_net_worth(workspace_id: int) -> dict[str, Any]:
        """Get the current net worth for an authorized workspace."""
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_net_worth"
            )
            account_repo = AccountRepository(session)
            setting_repo = FinanceSettingRepository(session)
            cash_repo = CashBalanceRepository(session)
            holding_repo = HoldingRepository(session)
            fx_rate_repo = FxRateRepository(session)
            summary = InvestingSummaryService(
                holding_repo,
                cash_repo,
                setting_repo,
                fx_rate_repo,
                HoldingPriceRepository(session),
                PortfolioSnapshotRepository(session),
                account_repo,
            )
            result = await NetWorthService(
                session,
                AccountService(account_repo, CurrencyRepository(session), setting_repo),
                summary,
                cash_repo,
                setting_repo,
                fx_rate_repo,
                NetWorthSnapshotRepository(session),
            ).get_net_worth(workspace_id)
            return result

    @mcp.tool
    async def get_spending_budgets(workspace_id: int) -> dict[str, Any]:
        """Get budgets and spending details for an authorized workspace."""
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_spending_budgets"
            )
            service = BudgetService(
                BudgetRepository(session),
                CategoryRepository(session),
                CategoryGroupRepository(session),
            )
            budgets, total = await service.list_budgets_with_details(workspace_id, limit=100)
            return {
                "workspace_id": workspace_id,
                "budgets": [b.model_dump() for b in budgets],
                "total": total,
            }

    @mcp.tool
    async def get_workspace_reference_data(workspace_id: int) -> dict[str, Any]:
        """Get bounded workspace vocabulary for safe follow-up tool calls.

        This is read-only and excludes brokerage accounts. It gives MCP clients
        the same categories, spending accounts, tags, active medications, and
        persisted timezone that the voice surface uses for disambiguation.
        """
        async with postgres.async_session_maker() as session:
            user_id = await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="get_workspace_reference_data",
            )
            return await _load_workspace_reference_data(session, workspace_id, user_id)

    @mcp.tool
    async def list_workspaces() -> dict[str, Any]:
        """List active workspaces available to the authenticated MCP user.

        Call this before workspace-scoped tools when the workspace is not already
        supplied by the host application. Never guess a workspace identifier.
        """
        user_id = authorize_user(required_scope="mcp:read", tool="list_workspaces")
        async with postgres.async_session_maker() as session:
            workspaces = await _load_workspace_choices(session, user_id)
            return {"workspaces": workspaces}

    @mcp.resource(
        "lifestack://me/workspaces",
        name="my-workspaces",
        description="Active workspaces available to the authenticated MCP user.",
        mime_type="application/json",
    )
    async def my_workspaces() -> str:
        """Expose workspace discovery as a passive authenticated resource."""
        user_id = authorize_user(required_scope="mcp:read", tool="my_workspaces")
        async with postgres.async_session_maker() as session:
            workspaces = await _load_workspace_choices(session, user_id)
            return json.dumps({"workspaces": workspaces}, separators=(",", ":"))

    @mcp.resource(
        "lifestack://workspaces/{workspace_id}/reference-data",
        name="workspace-reference-data",
        description=(
            "Bounded read-only workspace vocabulary for categories, spending accounts, "
            "tags, active medications, and timezone."
        ),
        mime_type="application/json",
    )
    async def workspace_reference_data(workspace_id: int) -> str:
        """Return authenticated workspace context as a JSON MCP resource."""
        async with postgres.async_session_maker() as session:
            user_id = await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="workspace_reference_data",
            )
            data = await _load_workspace_reference_data(session, workspace_id, user_id)
            return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @mcp.tool
    async def list_investment_holdings(
        workspace_id: int,
        limit: int = 25,
        offset: int = 0,
        quantity_state: str = "nonzero",
        symbol: str | None = None,
        account_id: str | None = None,
        currency: str | None = None,
        instrument_type: str | None = None,
        sort_by: str = "current_value",
        sort_direction: str = "desc",
        valuation_currency: str | None = None,
    ) -> dict[str, Any]:
        """List investment holdings with filters, sorting, and reporting valuation.

        Native valuation fields remain in each holding's own currency. Reporting
        values use ``valuation_currency`` when supplied, otherwise the workspace
        reporting currency, or the sole holding currency when unambiguous.
        """
        validate_page(limit, offset)
        if quantity_state not in _HOLDINGS_QUANTITY_STATES:
            return {
                "status": "error",
                "message": "quantity_state must be one of: all, nonzero, zero.",
            }
        if sort_by not in _HOLDINGS_SORT_FIELDS:
            return {
                "status": "error",
                "message": (
                    "sort_by must be one of: symbol, quantity, current_value, book_value, "
                    "gain_loss, gain_loss_pct, created_at, updated_at."
                ),
            }
        if sort_direction not in {"asc", "desc"}:
            return {"status": "error", "message": "sort_direction must be asc or desc."}
        if instrument_type and instrument_type not in _HOLDINGS_INSTRUMENT_TYPES:
            return {
                "status": "error",
                "message": "instrument_type must be one of: stock, etf, mutual_fund.",
            }
        if currency:
            currency = currency.strip().upper()
        if symbol:
            symbol = symbol.strip().upper()
        if valuation_currency:
            valuation_currency = valuation_currency.strip().upper()
            if len(valuation_currency) > 10 or not valuation_currency:
                return {"status": "error", "message": "Invalid valuation_currency."}
        try:
            account_public_id = uuid.UUID(account_id) if account_id else None
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid account_id."}

        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="list_investment_holdings",
            )

            account_filter_id = None
            if account_public_id is not None:
                account = await AccountRepository(session).get_by_public_id(
                    workspace_id, account_public_id
                )
                if account is None or account.id is None:
                    return {"status": "error", "message": "Account not found in this workspace."}
                account_filter_id = account.id

            service = _holding_service(session)
            requires_value_sort = sort_by in _HOLDINGS_VALUE_SORT_FIELDS
            query_limit = None if requires_value_sort else limit
            query_offset = 0 if requires_value_sort else offset
            query_sort_by = "symbol" if requires_value_sort else sort_by
            query_sort_direction = "asc" if requires_value_sort else sort_direction
            raw_holdings, total = await HoldingRepository(session).get_all(
                workspace_id,
                limit=query_limit,
                offset=query_offset,
                quantity_state=quantity_state,
                symbol=symbol,
                account_id=account_filter_id,
                currency=currency,
                instrument_type=instrument_type,
                sort_by=query_sort_by,
                sort_direction=query_sort_direction,
            )
            detailed_items, _ = await service.list_holdings_with_details(
                workspace_id,
                limit=query_limit,
                offset=query_offset,
                quantity_state=quantity_state,
                symbol=symbol,
                account_id=account_filter_id,
                currency=currency,
                instrument_type=instrument_type,
                sort_by=query_sort_by,
                sort_direction=query_sort_direction,
            )
            instruments = await InstrumentRepository(session).get_by_ids([
                holding.instrument_id
                for holding in raw_holdings
                if holding.instrument_id is not None
            ])
            companies = await CompanyRepository(session).get_by_ids([
                instrument.company_id
                for instrument in instruments.values()
                if instrument.company_id is not None
            ])
            raw_by_public_id = {holding.public_id: holding for holding in raw_holdings}
            price_rows = await HoldingPriceRepository(session).latest_prices_on_or_before_bulk(
                workspace_id,
                [holding.id for holding in raw_holdings if holding.id is not None],
                datetime.now(UTC).date(),
            )

            finance_settings = await FinanceSettingRepository(session).get_by_workspace(
                workspace_id
            )
            reporting_currency = valuation_currency or (
                finance_settings.reporting_currency_code.upper()
                if finance_settings and finance_settings.reporting_currency_code
                else None
            )
            if valuation_currency:
                currency_row = await CurrencyRepository(session).get_by_code(valuation_currency)
                if currency_row is None or not currency_row.is_active:
                    return {
                        "status": "error",
                        "message": f"Unsupported valuation_currency '{valuation_currency}'.",
                    }

            used_currencies = sorted({
                holding.currency.upper() for holding in raw_holdings if holding.quantity != 0
            })
            if reporting_currency is None and len(used_currencies) == 1:
                reporting_currency = used_currencies[0]
            fx_lookup: dict[tuple[str, str], Any] = {}
            fx_as_of = None
            if reporting_currency and any(
                currency_code != reporting_currency for currency_code in used_currencies
            ):
                fx_as_of = effective_display_as_of()
                fx_lookup = await FxRateRepository(session).get_latest_rates_for_pairs(
                    list(build_required_pairs(used_currencies, reporting_currency)),
                    as_of=fx_as_of,
                )

            items = []
            for item in detailed_items:
                data = item.model_dump(mode="json")
                raw = raw_by_public_id.get(item.public_id)
                instrument = instruments.get(raw.instrument_id) if raw else None
                data.update({
                    "instrument_public_id": str(instrument.public_id) if instrument else None,
                    "instrument_name": instrument.name if instrument else None,
                    "isin": instrument.isin if instrument else None,
                    "exchange": instrument.exchange if instrument else None,
                })
                if instrument is not None and instrument.company_id is not None:
                    company = companies.get(instrument.company_id)
                    if company is not None:
                        data["company_sector"] = company.sector
                        data["company_industry"] = company.industry
                if raw is not None:
                    _add_holding_reporting_valuation(
                        data,
                        raw,
                        price_rows.get(raw.id) if raw.id is not None else None,
                        reporting_currency,
                        fx_lookup,
                        fx_as_of,
                    )
                items.append(data)

            if requires_value_sort:
                items = _sort_holding_items(
                    items,
                    sort_by,
                    sort_direction,
                    use_reporting_values=reporting_currency is not None,
                )
            else:
                # The repository already applied this ordering. Keep the
                # explicit slice here only for the value-sort path's shared
                # response construction.
                items = items[:limit]
            page_total = total
            if requires_value_sort:
                items = items[offset : offset + limit]
            return {
                "status": "success",
                "workspace_id": workspace_id,
                "items": items,
                "total": page_total,
                "limit": limit,
                "offset": offset,
                "quantity_state": quantity_state,
                "sort_by": sort_by,
                "sort_direction": sort_direction,
                "valuation_currency": reporting_currency,
                "fx_as_of": fx_as_of.isoformat() if fx_as_of else None,
                "fx_rates_used": (
                    fx_rates_used(used_currencies, reporting_currency, fx_lookup)
                    if reporting_currency
                    else {}
                ),
            }

    @mcp.tool
    async def get_investment_constituents(
        workspace_id: int,
        instrument_public_id: str,
        as_of_date: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Read the latest sourced ETF or mutual-fund constituent snapshot.

        Returns the snapshot on or before ``as_of_date`` (latest snapshot by
        ``(as_of_date, fetched_at)`` counts when several exist), optionally
        filtered by ``source``. Each row carries the constituent company's
        ``sector`` and ``industry`` where known.
        """
        try:
            instrument_id = uuid.UUID(instrument_public_id)
            as_of = date.fromisoformat(as_of_date) if as_of_date else datetime.now(UTC).date()
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid instrument_public_id or as_of_date."}

        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="get_investment_constituents",
            )
            items = await _constituent_service(session).get_constituents(
                workspace_id, instrument_id, as_of, source=source
            )
            return {
                "status": "success",
                "workspace_id": workspace_id,
                "instrument_public_id": instrument_public_id,
                "as_of_date": as_of.isoformat(),
                "source": source,
                "items": [item.model_dump(mode="json") for item in items],
                "total": len(items),
            }

    @mcp.tool
    async def write_investment_constituent_snapshot(
        workspace_id: int,
        instrument_public_id: str,
        as_of_date: str,
        source: str,
        fetched_at: str,
        constituents: list[dict[str, Any]],
        renormalise: bool = False,
        overwrite_existing: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Replace one sourced ETF or mutual-fund constituent snapshot.

        Requires the dedicated mcp:research grant and explicit confirmation.
        Each constituent must include a ticker or ISIN and a decimal weight,
        and may optionally carry ``sector`` and ``industry`` (free text, up to
        100 characters each; GICS sector names are recommended). A provided
        sector/industry fills an empty value on the constituent company; an
        already-set value is overwritten only when ``overwrite_existing`` is
        true.
        """
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    "This will replace the complete constituent snapshot for "
                    f"{instrument_public_id} on {as_of_date} from {source}. Confirm to continue."
                ),
                "instrument_public_id": instrument_public_id,
                "as_of_date": as_of_date,
                "source": source,
                "constituent_count": len(constituents),
            }
        if not 1 <= len(constituents) <= 500:
            return {
                "status": "error",
                "message": "constituents must contain between 1 and 500 rows.",
            }

        try:
            instrument_id = uuid.UUID(instrument_public_id)
            payload = InstrumentConstituentUpsert.model_validate({
                "as_of_date": as_of_date,
                "source": source,
                "fetched_at": fetched_at,
                "constituents": constituents,
                "renormalise": renormalise,
                "overwrite_existing": overwrite_existing,
            })
        except (TypeError, ValueError, PydanticValidationError) as exc:
            return {"status": "error", "message": f"Invalid constituent snapshot: {exc}"}

        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:research",
                tool="write_investment_constituent_snapshot",
            )
            try:
                service = _constituent_service(session)
                await service.upsert_constituents(workspace_id, instrument_id, payload)
                items = await service.get_constituents(
                    workspace_id,
                    instrument_id,
                    payload.as_of_date,
                    source=payload.source,
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                "entity_type": "investment_constituent_snapshot",
                "instrument_public_id": instrument_public_id,
                "as_of_date": payload.as_of_date.isoformat(),
                "source": payload.source,
                "fetched_at": payload.fetched_at.isoformat(),
                "items": [item.model_dump(mode="json") for item in items],
                "total": len(items),
                "summary": f"Saved {len(items)} constituent rows from {payload.source}.",
            }

    @mcp.tool
    async def delete_investment_constituent_snapshot(
        workspace_id: int,
        instrument_public_id: str,
        as_of_date: str,
        source: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Delete one complete sourced constituent snapshot after confirmation."""
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    "This will delete the complete constituent snapshot for "
                    f"{instrument_public_id} on {as_of_date} from {source}. Confirm to continue."
                ),
            }
        try:
            instrument_id = uuid.UUID(instrument_public_id)
            snapshot_date = date.fromisoformat(as_of_date)
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid instrument_public_id or as_of_date."}

        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:research",
                tool="delete_investment_constituent_snapshot",
            )
            try:
                deleted = await _constituent_service(session).delete_snapshot(
                    workspace_id, instrument_id, snapshot_date, source
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                "entity_type": "investment_constituent_snapshot",
                "instrument_public_id": instrument_public_id,
                "as_of_date": as_of_date,
                "source": source,
                "deleted_rows": deleted,
                "summary": f"Deleted the {source} constituent snapshot.",
            }

    @mcp.tool
    async def set_company_sector(
        workspace_id: int,
        rows: list[dict[str, Any]],
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Set the sector (and optional industry and country) on companies, in bulk.

        ``rows`` holds 1-200 entries. Each row identifies its company by
        exactly one of:

        - ``holding_public_id`` - a holding in this workspace; the company
          behind that holding's instrument is updated.
        - ``isin`` - a company matched by ISIN, workspace-scoped or global.
        - ``ticker`` plus ``exchange`` - a company via an instrument listed
          on that exchange, workspace-scoped or global.

        ``sector`` is required on every row (free text, up to 100 characters).
        Use GICS names: Information Technology, Health Care, Financials,
        Industrials, Energy, Consumer Staples, Consumer Discretionary,
        Materials, Utilities, Communication Services, Real Estate. Case and
        extra whitespace are normalised (``health  care`` -> ``Health Care``)
        so variants never split an allocation bucket. ``industry`` (up to 100
        characters) and ``country_code`` are optional and only written when
        provided. ETFs and mutual funds have no company; their sectors come
        from constituent snapshots.

        A global company is never edited in place: it is forked into a
        workspace-scoped copy and this workspace's instruments and holdings
        are re-pointed to it (idempotent on re-run). Without ``confirmed`` the
        call returns ``needs_confirmation`` and writes nothing. With it, each
        row is applied atomically; a row that cannot be applied is reported in
        ``errors`` while the other rows still apply, and an unexpected failure
        rolls back the whole call. Returns per-row ``before``/``after`` values.
        Requires the mcp:research scope.
        """
        if not 1 <= len(rows) <= 200:
            return {
                "status": "error",
                "message": "rows must contain between 1 and 200 entries.",
            }
        parsed: list[CompanySectorRow] = []
        for idx, raw in enumerate(rows):
            try:
                parsed.append(CompanySectorRow.model_validate(raw))
            except PydanticValidationError as exc:
                detail = "; ".join(
                    str(err["msg"]).removeprefix("Value error, ") for err in exc.errors()
                )
                return {"status": "error", "message": f"row {idx}: {detail}"}

        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:research",
                tool="set_company_sector",
            )
            if not confirmed:
                return {
                    "status": "needs_confirmation",
                    "needs_confirmation": True,
                    "message": (
                        f"This will set the sector on {len(parsed)} company(ies) in "
                        f"workspace {workspace_id}. Confirm to continue."
                    ),
                    "row_count": len(parsed),
                    "rows": [
                        {
                            "row_index": idx,
                            "requested_sector": row.sector,
                            "requested_industry": row.industry,
                            "requested_country_code": row.country_code,
                        }
                        for idx, row in enumerate(parsed)
                    ],
                }
            try:
                outcome = await _company_sector_service(session).set_sectors(workspace_id, parsed)
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("set_company_sector failed; no changes were saved")
                return {
                    "status": "error",
                    "message": "set_company_sector failed unexpectedly; no changes were saved.",
                }
        updated, errors = outcome["updated"], outcome["errors"]
        return {
            "status": "success",
            "entity_type": "company_sector",
            "workspace_id": workspace_id,
            "updated": updated,
            "errors": errors,
            "summary": (
                f"Set sector on {len(updated)} company(ies)"
                + (f"; {len(errors)} row(s) failed" if errors else "")
                + "."
            ),
        }

    @mcp.tool
    async def set_company_details(
        workspace_id: int,
        company_public_id: str,
        name: str | None = None,
        isin: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Rename a company and/or set its ISIN, workspace-scoped.

        Edits one company visible in this workspace (global or a workspace
        copy). A global company is forked into a workspace copy first, so the
        global record is never mutated in place. At least one of ``name`` or
        ``isin`` must be provided. An ISIN or name already used by another
        visible company is rejected.

        Without ``confirmed`` the call returns ``needs_confirmation`` and
        writes nothing. With it, the change is applied in a transaction and
        recorded in the audit trail (before/after plus changed fields).
        Requires the mcp:write scope.
        """
        if name is None and isin is None:
            return {
                "status": "error",
                "message": "provide at least one of name or isin",
            }
        try:
            public_id = uuid.UUID(company_public_id)
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid company_public_id."}

        async with postgres.async_session_maker() as session:
            user_id = await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:write",
                tool="set_company_details",
            )
            if not confirmed:
                return {
                    "status": "needs_confirmation",
                    "needs_confirmation": True,
                    "message": (
                        f"This will rename and/or set the ISIN of company "
                        f"{company_public_id} in workspace {workspace_id}. "
                        "Confirm to continue."
                    ),
                    "company_public_id": company_public_id,
                    "requested_name": name,
                    "requested_isin": isin,
                }
            try:
                outcome = await _company_sector_service(session).set_company_details(
                    workspace_id,
                    public_id,
                    name=name,
                    isin=isin,
                )
                await AuditLogger(session).log(
                    workspace_id=workspace_id,
                    actor_id=user_id,
                    action="update",
                    module="investing",
                    entity_type="company",
                    entity_id=outcome["company_id"],
                    details={
                        "entity_public_id": str(public_id),
                        "before": outcome["before"],
                        "after": outcome["after"],
                        "changed_fields": [
                            k
                            for k in ("name", "isin")
                            if outcome["before"][k] != outcome["after"][k]
                        ],
                    },
                )
                await session.commit()
            except (APIError, ValueError, SectorRowError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
        return {
            "status": "success",
            "entity_type": "company",
            "entity_public_id": company_public_id,
            "workspace_id": workspace_id,
            "forked_from_global": outcome["forked_from_global"],
            "changed": outcome["changed"],
            "before": outcome["before"],
            "after": outcome["after"],
            "summary": (
                f"Updated company {company_public_id}"
                + (", forked from the global record" if outcome["forked_from_global"] else "")
                + (
                    "; changed: "
                    + ", ".join(
                        k for k in ("name", "isin") if outcome["before"][k] != outcome["after"][k]
                    )
                    if outcome["changed"]
                    else ""
                )
                + "."
            ),
        }

    @mcp.tool
    async def get_investment_sector_allocation(
        workspace_id: int,
        as_of_date: str | None = None,
        lookthrough: bool = True,
    ) -> dict[str, Any]:
        """Sector allocation of the portfolio, with optional ETF/MF look-through.

        With look-through (default) each ETF/MF holding is split across the
        sectors of the companies in its latest constituent snapshot (exactly
        one snapshot per instrument, newest by ``(as_of_date, fetched_at)``);
        direct stocks use their own company's sector; ``OTHER-<FUND>``
        placeholder holdings and unmapped companies go to ``Other / not
        itemised`` and are never rescaled. Returns value and % per sector
        (summing to 100% including that bucket) plus coverage per fund.
        """
        try:
            as_of = date.fromisoformat(as_of_date) if as_of_date else datetime.now(UTC).date()
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid as_of_date. Use YYYY-MM-DD."}

        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="get_investment_sector_allocation",
            )
            result = await _analytics_service(session).sector_allocation(
                workspace_id, as_of, lookthrough=lookthrough
            )
            return {
                "status": "success",
                "workspace_id": workspace_id,
                "as_of_date": result["as_of_date"],
                "currency": result["currency"],
                "lookthrough": result["lookthrough"],
                "total_portfolio_value": result["total_portfolio_value"],
                "holdings_value": result["holdings_value"],
                "sectors": result["sectors"],
                "fund_coverage": result["fund_coverage"],
                "warnings": result["warnings"],
                "fx_as_of": result["fx_as_of"],
                "fx_rates_used": result["fx_rates_used"],
            }

    @mcp.tool
    async def list_investment_dividends(
        workspace_id: int,
        symbol: str | None = None,
        account_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List bounded dividend, interest, and coupon events."""
        validate_page(limit, offset)
        try:
            account_public_id = uuid.UUID(account_id) if account_id else None
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid account_id."}

        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="list_investment_dividends",
            )
            rows, total, accounts = await _dividend_service(session).list_dividends(
                workspace_id,
                limit,
                offset,
                account_id=account_public_id,
                symbol=symbol,
            )
            items = [
                _dividend_response(row, accounts[row.account_id])
                for row in rows
                if row.account_id in accounts
            ]
            return {
                "status": "success",
                "workspace_id": workspace_id,
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    @mcp.tool
    async def list_investment_orders(
        workspace_id: int,
        account_id: str | None = None,
        symbol: str | None = None,
        order_type: str | None = None,
        search: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List recorded stock, ETF, and mutual-fund orders."""
        validate_page(limit, offset)
        if order_type is not None and order_type not in {"buy", "sell"}:
            return {"status": "error", "message": "order_type must be 'buy' or 'sell'."}
        try:
            account_public_id = uuid.UUID(account_id) if account_id else None
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid account_id."}

        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_investment_orders"
            )
            account_repo = AccountRepository(session)
            account_internal_id = None
            if account_public_id is not None:
                account = await account_repo.get_by_public_id(workspace_id, account_public_id)
                if account is None or account.id is None:
                    return {"status": "error", "message": "Investment account not found."}
                account_internal_id = account.id
            order_repo = InvestingOrderRepository(session)
            orders, total = await order_repo.list_by_workspace(
                workspace_id,
                limit,
                offset,
                symbol=symbol,
                account_id=account_internal_id,
                order_type=order_type,
                search=search,
            )
            accounts = await account_repo.get_by_ids(
                workspace_id, {order.account_id for order in orders}
            )
            instruments = await InstrumentRepository(session).get_by_ids([
                order.instrument_id for order in orders if order.instrument_id is not None
            ])
            items = [
                _order_response(
                    order, accounts[order.account_id], instruments.get(order.instrument_id)
                )
                for order in orders
                if order.account_id in accounts
            ]
            return {
                "status": "success",
                "workspace_id": workspace_id,
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    async def _preview_order(
        workspace_id: int,
        payload: InvestingOrderCreate,
        gross: Decimal,
        net: Decimal,
        allow_duplicate: bool = False,
    ) -> dict[str, Any]:
        """``create_investment_order`` dry run: validate + dedupe, persist nothing."""
        async with postgres.async_session_maker() as session:
            user_id = await authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="create_investment_order"
            )
            status, match_reason, existing_id = "would_create", None, None
            errors: list[str] = []
            order_repo = InvestingOrderRepository(session)
            if payload.source_ref:
                existing = await order_repo.get_by_source_ref(workspace_id, payload.source_ref)
                if existing is not None:
                    status, match_reason, existing_id = (
                        "duplicate",
                        "source_ref",
                        str(existing.public_id),
                    )
                    mismatches = await _source_ref_replay_mismatches(
                        session, workspace_id, existing, payload
                    )
                    if mismatches:
                        status = "invalid"
                        errors = [_source_ref_conflict_message(payload.source_ref, mismatches)]
            elif not allow_duplicate:
                similar = await _possible_duplicate_orders(session, workspace_id, user_id, payload)
                if similar:
                    status, match_reason, existing_id = (
                        "possible_duplicate",
                        "possible_duplicate",
                        str(similar[0].public_id),
                    )
            if status == "would_create":
                # Run the real business validation (account, currency, cash, holdings)
                # inside a SAVEPOINT that is always rolled back -- nothing persists.
                nested = await session.begin_nested()
                try:
                    await _order_service(session).place_order(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        order_in=payload,
                        audit_logger=AuditLogger(session),
                        source_type="mcp_agent",
                    )
                except (APIError, ValueError) as exc:
                    status = "invalid"
                    errors = [str(getattr(exc, "detail", None) or exc)]
                finally:
                    await nested.rollback()
            await session.rollback()
            counts = {"create": 0, "duplicate": 0, "possible_duplicate": 0, "invalid": 0}
            counts["create" if status == "would_create" else status] += 1
            return {
                "status": "success",
                "dry_run": True,
                "counts": counts,
                "items": [
                    {
                        "index": 0,
                        "status": status,
                        "match_reason": match_reason,
                        "existing_id": existing_id,
                        "errors": errors,
                        "warnings": [],
                    }
                ],
                **_order_preview_fields(payload, gross, net),
            }

    @mcp.tool
    async def create_investment_order(
        workspace_id: int,
        account_id: str,
        order_type: str,
        symbol: str,
        quantity: str,
        price_per_unit: str,
        currency: str,
        occurred_at: str,
        instrument_type: str = "stock",
        instrument_name: str | None = None,
        brokerage_fee: str = "0",
        tax_amount: str = "0",
        other_fees: str = "0",
        exchange_name: str | None = None,
        notes: str | None = None,
        source_ref: str | None = None,
        confirmed: bool = False,
        dry_run: bool = False,
        allow_duplicate: bool = False,
    ) -> dict[str, Any]:
        """Record a historical buy or sell order after explicit confirmation.

        This records the trade in Lifestack and updates holdings and brokerage
        cash; it does not submit a live order to a broker.

        Idempotency: a repeated ``source_ref`` returns the existing order and
        writes nothing. Without a ``source_ref``, an order with the same
        account + symbol + side + quantity + price on the same local day is
        reported as ``possible_duplicate`` (with the existing order's ID) and is
        NOT recorded -- fees and rounding differ too much to skip it silently;
        set ``allow_duplicate=true`` to record a genuine repeat.

        ``dry_run=true`` runs every validation (including cash and holding
        checks) and the duplicate checks, writes nothing, needs no confirmation,
        and returns ``{counts, items:[{index, status, match_reason, existing_id,
        errors}]}`` with the calculated amounts.
        """
        try:
            payload = InvestingOrderCreate(
                account_id=uuid.UUID(account_id),
                order_type=OrderType(order_type.strip().lower()),
                symbol=symbol,
                instrument_type=InstrumentType(instrument_type.strip().lower()),
                instrument_name=instrument_name,
                quantity=quantity,
                price_per_unit=price_per_unit,
                currency=currency,
                brokerage_fee=brokerage_fee,
                tax_amount=tax_amount,
                other_fees=other_fees,
                exchange_name=exchange_name,
                occurred_at=occurred_at,
                notes=notes,
                source_ref=source_ref,
            )
        except (TypeError, ValueError, PydanticValidationError) as exc:
            return {"status": "error", "message": f"Invalid investment order: {exc}"}

        gross_amount = payload.quantity * payload.price_per_unit
        fees = payload.brokerage_fee + payload.tax_amount + payload.other_fees
        net_amount = (
            gross_amount + fees if payload.order_type == OrderType.buy else gross_amount - fees
        )
        if net_amount <= 0:
            return {"status": "error", "message": "Fees must leave a positive net amount."}

        if dry_run:
            return await _preview_order(
                workspace_id, payload, gross_amount, net_amount, allow_duplicate
            )

        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    f"Record {payload.order_type.value} order for {payload.quantity} {payload.symbol} "
                    f"at {payload.price_per_unit} {payload.currency} per unit; net amount "
                    f"{net_amount} {payload.currency} on {payload.occurred_at.isoformat()}?"
                ),
                "account_id": str(payload.account_id),
                "order_type": payload.order_type.value,
                "symbol": payload.symbol,
                "instrument_type": payload.instrument_type.value,
                "quantity": str(payload.quantity),
                "price_per_unit": str(payload.price_per_unit),
                "gross_amount": str(gross_amount),
                "brokerage_fee": str(payload.brokerage_fee),
                "tax_amount": str(payload.tax_amount),
                "other_fees": str(payload.other_fees),
                "net_amount": str(net_amount),
                "currency": payload.currency,
                "occurred_at": payload.occurred_at.isoformat(),
                "source_ref": payload.source_ref,
            }

        async with postgres.async_session_maker() as session:
            user_id = await authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="create_investment_order"
            )
            order_repo = InvestingOrderRepository(session)
            # Check-then-insert below: hold a per-workspace lock until commit so a
            # concurrent identical request waits and then sees this order.
            await order_repo.lock_idempotent_writes(workspace_id)
            if payload.source_ref:
                existing = await order_repo.get_by_source_ref(workspace_id, payload.source_ref)
                if existing is not None:
                    mismatches = await _source_ref_replay_mismatches(
                        session, workspace_id, existing, payload
                    )
                    if mismatches:
                        return {
                            "status": "error",
                            "match_reason": "source_ref",
                            "existing_id": str(existing.public_id),
                            "message": _source_ref_conflict_message(payload.source_ref, mismatches),
                        }
                    account = await AccountRepository(session).get_by_id(
                        workspace_id, existing.account_id
                    )
                    return {
                        "status": "success",
                        "idempotent": True,
                        "match_reason": "source_ref",
                        "existing_id": str(existing.public_id),
                        "entity_type": "investing_order",
                        "entity_public_id": str(existing.public_id),
                        "item": _order_response(existing, account),
                    }
            elif not allow_duplicate:
                similar = await _possible_duplicate_orders(session, workspace_id, user_id, payload)
                if similar:
                    return {
                        "status": "possible_duplicate",
                        "match_reason": "possible_duplicate",
                        "existing_id": str(similar[0].public_id),
                        "possible_duplicate_count": len(similar),
                        "message": (
                            "An order with the same account, symbol, side, quantity and price "
                            "already exists on this day. Nothing was recorded. If this is a "
                            "genuine second order, retry with allow_duplicate=true; otherwise "
                            "pass a source_ref to make retries idempotent."
                        ),
                    }
            try:
                order = await _order_service(session).place_order(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    order_in=payload,
                    audit_logger=AuditLogger(session),
                    source_type="mcp_agent",
                )
                await PortfolioSnapshotRepository(session).delete_for_date(
                    workspace_id, datetime.now(UTC).date()
                )
                account = await AccountRepository(session).get_by_id(workspace_id, order.account_id)
                instrument = (
                    await InstrumentRepository(session).get_by_ids([order.instrument_id])
                    if order.instrument_id is not None
                    else {}
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            item = _order_response(order, account, instrument.get(order.instrument_id))
            return {
                "status": "success",
                "entity_type": "investing_order",
                "entity_public_id": item["public_id"],
                "item": item,
                "summary": f"Recorded {payload.order_type.value} order for {payload.symbol}.",
            }

    @mcp.tool
    async def update_investment_order(
        workspace_id: int,
        public_id: str,
        order_type: str | None = None,
        quantity: str | None = None,
        price_per_unit: str | None = None,
        brokerage_fee: str | None = None,
        tax_amount: str | None = None,
        other_fees: str | None = None,
        exchange_name: str | None = None,
        occurred_at: str | None = None,
        notes: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Update a recorded investment order after explicit confirmation."""
        try:
            order_public_id = uuid.UUID(public_id)
            payload = InvestingOrderUpdate(
                order_type=OrderType(order_type.strip().lower()) if order_type else None,
                quantity=quantity,
                price_per_unit=price_per_unit,
                brokerage_fee=brokerage_fee,
                tax_amount=tax_amount,
                other_fees=other_fees,
                exchange_name=exchange_name,
                occurred_at=occurred_at,
                notes=notes,
            )
        except (TypeError, ValueError, PydanticValidationError) as exc:
            return {"status": "error", "message": f"Invalid investment order update: {exc}"}
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": f"Update investment order {public_id}? Confirm to continue.",
                "public_id": public_id,
                "changes": payload.model_dump(exclude_unset=True, mode="json"),
            }
        async with postgres.async_session_maker() as session:
            user_id = await authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="update_investment_order"
            )
            try:
                order = await _order_service(session).update_order(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    order_public_id=order_public_id,
                    order_update=payload,
                    audit_logger=AuditLogger(session),
                )
                await PortfolioSnapshotRepository(session).delete_for_date(
                    workspace_id, datetime.now(UTC).date()
                )
                account = await AccountRepository(session).get_by_id(workspace_id, order.account_id)
                instrument = (
                    await InstrumentRepository(session).get_by_ids([order.instrument_id])
                    if order.instrument_id is not None
                    else {}
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            item = _order_response(order, account, instrument.get(order.instrument_id))
            return {
                "status": "success",
                "entity_type": "investing_order",
                "entity_public_id": item["public_id"],
                "item": item,
            }

    @mcp.tool
    async def delete_investment_order(
        workspace_id: int, public_id: str, confirmed: bool = False
    ) -> dict[str, Any]:
        """Delete a recorded investment order after explicit confirmation."""
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    f"Delete investment order {public_id}? This reverses its holding and "
                    "cash impact. Confirm to continue."
                ),
                "public_id": public_id,
            }
        try:
            order_public_id = uuid.UUID(public_id)
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid public_id."}
        async with postgres.async_session_maker() as session:
            user_id = await authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="delete_investment_order"
            )
            try:
                await _order_service(session).delete_order(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    order_public_id=order_public_id,
                    audit_logger=AuditLogger(session),
                )
                await PortfolioSnapshotRepository(session).delete_for_date(
                    workspace_id, datetime.now(UTC).date()
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
        return {
            "status": "success",
            "entity_type": "investing_order",
            "entity_public_id": public_id,
            "summary": f"Deleted investment order {public_id}.",
        }

    @mcp.tool
    async def create_investment_dividend(
        workspace_id: int,
        account_id: str,
        gross_amount: str,
        currency: str,
        pay_date: str,
        symbol: str | None = None,
        income_type: str = "dividend",
        tax_withheld: str = "0",
        external_ref: str | None = None,
        notes: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Create a confirmed dividend or investment-income event.

        This credits the linked brokerage cash balance through the existing
        DividendService and never accepts bank or wallet accounts.
        """
        norm_type = income_type.strip().lower() if income_type else "dividend"
        if norm_type not in ("dividend", "interest", "coupon"):
            return {
                "status": "error",
                "message": "income_type must be one of 'dividend', 'interest', 'coupon'.",
            }

        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    f"Record {norm_type} income of {gross_amount} {currency} for "
                    f"{symbol or 'the brokerage account'} on {pay_date}? Confirm to continue."
                ),
                "account_id": account_id,
                "symbol": symbol,
                "income_type": norm_type,
                "gross_amount": gross_amount,
                "tax_withheld": tax_withheld,
                "currency": currency,
                "pay_date": pay_date,
            }

        try:
            payload = DividendCreate(
                account_id=uuid.UUID(account_id),
                symbol=symbol,
                income_type=norm_type,
                gross_amount=gross_amount,
                tax_withheld=tax_withheld,
                currency=currency,
                pay_date=pay_date,
                external_ref=external_ref,
                notes=notes,
            )
        except (TypeError, ValueError, PydanticValidationError) as exc:
            return {"status": "error", "message": f"Invalid dividend: {exc}"}

        async with postgres.async_session_maker() as session:
            user_id = await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:write",
                tool="create_investment_dividend",
            )
            try:
                dividend, account = await _dividend_service(session).create_dividend(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    dividend_in=payload,
                    audit_logger=AuditLogger(session),
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            item = _dividend_response(dividend, account)
            return {
                "status": "success",
                "entity_type": "investment_dividend",
                "entity_public_id": item["public_id"],
                "item": item,
                "summary": f"Recorded {dividend.income_type} income of {dividend.net_amount} {dividend.currency}.",
            }

    @mcp.tool
    async def delete_investment_dividend(
        workspace_id: int,
        public_id: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Delete a dividend/investment-income event by public ID.

        This removes the dividend record and its associated cash balance credit.
        Requires explicit confirmation.
        """
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": f"Delete dividend {public_id}? This will also remove its cash balance credit. Confirm to continue.",
                "public_id": public_id,
            }

        try:
            div_public_id = uuid.UUID(public_id)
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid public_id."}

        async with postgres.async_session_maker() as session:
            user_id = await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:write",
                tool="delete_investment_dividend",
            )
            try:
                await _dividend_service(session).delete_dividend(
                    workspace_id=workspace_id,
                    public_id=div_public_id,
                    actor_id=user_id,
                    audit_logger=AuditLogger(session),
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                "entity_type": "investment_dividend",
                "entity_public_id": public_id,
                "summary": f"Deleted dividend {public_id}.",
            }

    @mcp.tool
    async def find_spending_transactions(
        workspace_id: int,
        from_day: str | None = None,
        to_day: str | None = None,
        category_name: str | None = None,
        amount: str | None = None,
        search: str | None = None,
        account_name: str | None = None,
        transaction_type: str | None = "expense",
        limit: int = 10,
        from_date: str | None = None,
        to_date: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Find transaction candidates in an authorized workspace.

        ``from_date``/``to_date`` (YYYY-MM-DD) are inclusive workspace-local calendar
        days (``from_day``/``to_day`` are aliases). Returns ``total_count`` and
        ``next_cursor``; pass it back as ``cursor`` for the next page.
        """
        return await _run_capture_tool(
            workspace_id,
            "mcp:read",
            "find_spending_transactions",
            {
                "from_date": from_date,
                "to_date": to_date,
                "cursor": cursor,
                "from_day": from_day,
                "to_day": to_day,
                "category_name": category_name,
                "amount": amount,
                "search": search,
                "account_name": account_name,
                "transaction_type": transaction_type,
                "limit": limit,
            },
        )

    @mcp.tool
    async def update_spending_transaction(
        workspace_id: int,
        public_id: str,
        amount: str | None = None,
        category_name: str | None = None,
        description: str | None = None,
        account_name: str | None = None,
        occurred_at: str | None = None,
        tags: list[str] | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Update one spending transaction after explicit confirmation."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:write",
            "update_spending_transaction",
            {
                "public_id": public_id,
                "amount": amount,
                "category_name": category_name,
                "description": description,
                "account_name": account_name,
                "occurred_at": occurred_at,
                "tags": tags,
                "confirmed": confirmed,
            },
        )

    @mcp.tool
    async def delete_spending_transaction(
        workspace_id: int, public_id: str, confirmed: bool = False
    ) -> dict[str, Any]:
        """Delete one spending transaction after explicit confirmation."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:write",
            "delete_spending_transaction",
            {"public_id": public_id, "confirmed": confirmed},
        )

    @mcp.tool
    async def list_transfers(
        workspace_id: int,
        day: str | None = None,
        from_day: str | None = None,
        to_day: str | None = None,
        account_name: str | None = None,
        amount: str | None = None,
        search: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """List capital transfers for a day or date range in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:read",
            "list_transfers",
            {
                "day": day,
                "from_day": from_day,
                "to_day": to_day,
                "account_name": account_name,
                "amount": amount,
                "search": search,
                "limit": limit,
            },
        )

    @mcp.tool
    async def find_transfers(
        workspace_id: int,
        from_day: str | None = None,
        to_day: str | None = None,
        account_name: str | None = None,
        amount: str | None = None,
        search: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Find bounded transfer candidates for a correction or deletion."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:read",
            "find_transfers",
            {
                "from_day": from_day,
                "to_day": to_day,
                "account_name": account_name,
                "amount": amount,
                "search": search,
                "limit": limit,
            },
        )

    @mcp.tool
    async def create_transfer(
        workspace_id: int,
        from_account_name: str,
        to_account_name: str,
        amount: str,
        fx_rate: str | None = None,
        fees: str | None = None,
        fx_fee_amount: str | None = None,
        platform_fee_amount: str | None = None,
        tax_amount: str | None = None,
        net_amount: str | None = None,
        occurred_at: str | None = None,
        notes: str | None = None,
        source_ref: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Create a capital transfer between accounts in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:write",
            "create_transfer",
            {
                "from_account_name": from_account_name,
                "to_account_name": to_account_name,
                "amount": amount,
                "fx_rate": fx_rate,
                "fees": fees,
                "fx_fee_amount": fx_fee_amount,
                "platform_fee_amount": platform_fee_amount,
                "tax_amount": tax_amount,
                "net_amount": net_amount,
                "occurred_at": occurred_at,
                "notes": notes,
                "source_ref": source_ref,
                "confirmed": confirmed,
            },
        )

    @mcp.tool
    async def update_transfer(
        workspace_id: int,
        public_id: str,
        from_account_name: str | None = None,
        to_account_name: str | None = None,
        amount: str | None = None,
        fx_rate: str | None = None,
        fees: str | None = None,
        fx_fee_amount: str | None = None,
        platform_fee_amount: str | None = None,
        tax_amount: str | None = None,
        net_amount: str | None = None,
        occurred_at: str | None = None,
        notes: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Update an existing capital transfer in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:write",
            "update_transfer",
            {
                "public_id": public_id,
                "from_account_name": from_account_name,
                "to_account_name": to_account_name,
                "amount": amount,
                "fx_rate": fx_rate,
                "fees": fees,
                "fx_fee_amount": fx_fee_amount,
                "platform_fee_amount": platform_fee_amount,
                "tax_amount": tax_amount,
                "net_amount": net_amount,
                "occurred_at": occurred_at,
                "notes": notes,
                "confirmed": confirmed,
            },
        )

    @mcp.tool
    async def delete_transfer(
        workspace_id: int, public_id: str, confirmed: bool = False
    ) -> dict[str, Any]:
        """Delete an existing capital transfer in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:write",
            "delete_transfer",
            {"public_id": public_id, "confirmed": confirmed},
        )

    @mcp.tool
    async def create_todo_task(
        workspace_id: int,
        title: str,
        due_date: str | None = None,
        priority: str = "medium",
    ) -> dict[str, Any]:
        """Create a voice-compatible todo in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:write",
            "create_todo_task",
            {"title": title, "due_date": due_date, "priority": priority},
        )

    @mcp.tool
    async def create_recurring_todo(
        workspace_id: int,
        title: str,
        frequency: str = "weekly",
        interval: int = 1,
        due_time: str | None = None,
        timezone: str | None = None,
        end_date: str | None = None,
        monthly_mode: str = "day_of_month",
        by_weekday: int | None = None,
        by_ordinal: int | None = None,
        priority: str = "medium",
    ) -> dict[str, Any]:
        """Create a voice-compatible recurring todo in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:write",
            "create_recurring_todo",
            {
                "title": title,
                "frequency": frequency,
                "interval": interval,
                "due_time": due_time,
                "timezone": timezone,
                "end_date": end_date,
                "monthly_mode": monthly_mode,
                "by_weekday": by_weekday,
                "by_ordinal": by_ordinal,
                "priority": priority,
            },
        )

    @mcp.tool
    async def get_todo(workspace_id: int, public_id: str) -> dict[str, Any]:
        """Retrieve one todo in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id, "mcp:read", "get_todo", {"public_id": public_id}
        )

    @mcp.tool
    async def update_todo(
        workspace_id: int,
        public_id: str,
        title: str | None = None,
        description: str | None = None,
        due_date: str | None = None,
        priority: str | None = None,
        completed: bool | None = None,
    ) -> dict[str, Any]:
        """Update one todo in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:write",
            "update_todo",
            {
                "public_id": public_id,
                "title": title,
                "description": description,
                "due_date": due_date,
                "priority": priority,
                "completed": completed,
            },
        )

    @mcp.tool
    async def delete_todo(workspace_id: int, public_id: str) -> dict[str, Any]:
        """Delete one todo in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id, "mcp:write", "delete_todo", {"public_id": public_id}
        )

    @mcp.tool
    async def list_next_due_items(workspace_id: int, limit: int = 5) -> dict[str, Any]:
        """List the next due todos in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id, "mcp:read", "list_next_due_items", {"limit": limit}
        )

    @mcp.tool
    async def log_spending_transaction(
        workspace_id: int,
        amount: str,
        category_name: str,
        description: str | None = None,
        account_name: str | None = None,
        occurred_at: str | None = None,
        tags: list[str] | None = None,
        allow_duplicate: bool = False,
        transaction_type: str = "expense",
        source_ref: str | None = None,
        dry_run: bool = False,
        skip_duplicates: bool = False,
    ) -> dict[str, Any]:
        """Create a transaction (expense or income) in an authorized workspace.

        A repeated ``source_ref`` returns the existing transaction (idempotent).
        ``dry_run=true`` writes nothing and returns ``{counts, items:[{index,
        status, match_reason, existing_id, errors}]}``. ``skip_duplicates=true``
        reports a same-day/amount/account/description match as a duplicate
        instead of refusing.
        """
        return await _run_capture_tool(
            workspace_id,
            "mcp:write",
            "log_spending_transaction",
            {
                "dry_run": dry_run,
                "skip_duplicates": skip_duplicates,
                "amount": amount,
                "category_name": category_name,
                "description": description,
                "account_name": account_name,
                "occurred_at": occurred_at,
                "tags": tags,
                "allow_duplicate": allow_duplicate,
                "transaction_type": transaction_type,
                "source_ref": source_ref,
            },
        )

    @mcp.tool
    async def log_spending_transactions_batch(
        workspace_id: int,
        items: list[dict[str, Any]],
        atomic: bool = False,
        dry_run: bool = False,
        skip_duplicates: bool = False,
    ) -> dict[str, Any]:
        """Create up to 50 transactions idempotently in an authorized workspace.

        Each item: {amount, category_name, description?, account_name?, occurred_at?,
        tags?, transaction_type?, source_ref?, allow_duplicate?}. An exact
        ``source_ref`` match returns the existing record. Items without one are matched
        on local day + amount + account + normalised description, multiplicity-aware
        (the ledger already holding 1 of 2 identical purchases creates 1, skips 1):
        ``duplicate`` with ``skip_duplicates=true``, else ``possible_duplicate`` (not
        written). Default is per-item partial success; ``atomic=true`` is all-or-nothing.
        ``dry_run=true`` writes nothing and returns the same structure with
        ``would_create``. Returns ``{counts, items:[{index, status, match_reason,
        existing_id, errors, warnings}]}``.
        """
        return await _run_capture_tool(
            workspace_id,
            "mcp:write",
            "log_spending_transactions_batch",
            {
                "items": items,
                "atomic": atomic,
                "dry_run": dry_run,
                "skip_duplicates": skip_duplicates,
            },
        )

    @mcp.tool
    async def list_spending_transactions(
        workspace_id: int,
        day: str | None = None,
        category_name: str | None = None,
        amount: str | None = None,
        search: str | None = None,
        account_name: str | None = None,
        transaction_type: str | None = "expense",
        limit: int = 10,
        from_date: str | None = None,
        to_date: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List transactions for a local day or inclusive date range in an authorized workspace.

        ``from_date``/``to_date`` (YYYY-MM-DD) are inclusive workspace-local calendar
        days. Returns ``total_count`` and ``next_cursor``; pass it back as ``cursor``
        for the next page.
        """
        return await _run_capture_tool(
            workspace_id,
            "mcp:read",
            "list_spending_transactions",
            {
                "from_date": from_date,
                "to_date": to_date,
                "cursor": cursor,
                "day": day,
                "category_name": category_name,
                "amount": amount,
                "search": search,
                "account_name": account_name,
                "transaction_type": transaction_type,
                "limit": limit,
            },
        )

    @mcp.tool
    async def log_weight(
        workspace_id: int, weight_kg: str, note: str | None = None
    ) -> dict[str, Any]:
        """Log a body-weight measurement in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:write",
            "log_weight",
            {"weight_kg": weight_kg, "note": note},
        )

    @mcp.tool
    async def log_medication_event(
        workspace_id: int,
        name: str,
        status: str,
        dose_time: str | None = None,
    ) -> dict[str, Any]:
        """Record a medication event in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:write",
            "log_medication_event",
            {"name": name, "status": status, "dose_time": dose_time},
        )

    @mcp.tool
    async def reconcile_holdings(
        workspace_id: int,
        external_list: list[dict[str, Any]],
        account_id: str | None = None,
        quantity_tolerance: str = "0.0001",
        cost_tolerance_pct: str = "0.5",
    ) -> dict[str, Any]:
        """Diff an external holdings list (INDmoney/Paasa/broker) against LifeStack.

        ``external_list`` entries (max 500): ``symbol`` and/or ``isin``,
        ``quantity`` (required), optional ``avg_cost``, ``currency``,
        ``account_id`` (LifeStack account UUID), ``source``, ``external_ref``.
        Symbols are normalised (``NSE:RELIANCE``, ``RELIANCE.NS`` and
        ``RELIANCE-EQ`` match ``RELIANCE``) and ISIN wins over symbol when both
        sides have one. Returns per-position status -- matched, quantity_drift,
        cost_basis_drift, missing_in_lifestack, missing_at_depository -- with an
        actionable suggestion for each non-match (record a missed order, review
        fees on mis-fee'd orders, remove a stale holding). Pass ``account_id``
        when the external list covers a single account. ``quantity_tolerance``
        is an absolute share count (float noise); ``cost_tolerance_pct`` is the
        allowed relative difference in average cost. Read-only.
        """
        try:
            external = parse_external_list(external_list)
            quantity_tol, cost_tol = parse_tolerances(quantity_tolerance, cost_tolerance_pct)
            account_public_id = uuid.UUID(account_id) if account_id else None
        except ReconciliationInputError as exc:
            return {"status": "error", "message": str(exc)}
        except (TypeError, ValueError):
            return {"status": "error", "message": "Invalid account_id."}
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="reconcile_holdings"
            )
            try:
                result = await _reconciliation_service(session).reconcile(
                    workspace_id,
                    external,
                    account_public_id=account_public_id,
                    quantity_tolerance=quantity_tol,
                    cost_tolerance_pct=cost_tol,
                )
            except ReconciliationInputError as exc:
                return {"status": "error", "message": str(exc)}
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "account_id": account_id,
            "quantity_tolerance": str(quantity_tol),
            "cost_tolerance_pct": str(cost_tol),
            **result,
        }

    @mcp.tool
    async def list_recent_changes(
        workspace_id: int,
        limit: int = 25,
        offset: int = 0,
        module: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        entity_public_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Paginated, filterable read of the append-only audit log (newest first).

        Each row has the actor's name, the entity, and the redacted
        ``details.before`` / ``details.after`` / ``details.changed_fields`` --
        enough to verify a write or to reconstruct an undo. Secrets (passwords,
        tokens, API keys, hashes, ...) are redacted. ``limit`` is capped at 100.
        ``since``/``until`` are ISO-8601 (naive values are read as UTC).
        Read-only.
        """
        validate_page(limit, offset)
        try:
            since_dt = _parse_iso_datetime(since, "since")
            until_dt = _parse_iso_datetime(until, "until")
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        if since_dt and until_dt and since_dt > until_dt:
            return {"status": "error", "message": "since must not be after until."}
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_recent_changes"
            )
            rows, total = await AuditLogRepository(session).list_by_workspace(
                workspace_id,
                module=module,
                action=action,
                entity_type=entity_type,
                entity_public_id=entity_public_id,
                since=since_dt,
                until=until_dt,
                limit=limit,
                offset=offset,
            )
            actors = await UserRepository(session).get_summaries_by_ids([
                r.actor_id for r in rows if r.actor_id is not None
            ])
        items = []
        for r in rows:
            details = redact_details(r.details or {})
            actor = actors.get(r.actor_id)
            items.append({
                "public_id": str(r.public_id),
                "timestamp": r.timestamp.isoformat(),
                "module": r.module,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_public_id": details.get("entity_public_id"),
                "actor_public_id": str(actor[0]) if actor else None,
                "actor_name": actor[1] if actor else None,
                "details": details,
            })
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }

    @mcp.tool
    async def get_performance(
        workspace_id: int,
        account_id: str | None = None,
        period: str = "all",
        reporting_currency: str = "INR",
    ) -> dict[str, Any]:
        """XIRR (money-weighted) and time-weighted return, converted to INR.

        XIRR is reported overall, per brokerage account and per holding, since
        inception, as a fraction (``0.1234`` = 12.34%) plus ``xirr_pct``; a
        position whose flows cannot yield one gets ``xirr: null`` and
        ``xirr_status: "not_computable"`` (never an error). Time-weighted return
        is chain-linked from portfolio snapshots over ``period`` (all, 1m, 3m,
        6m, 1y, 3y, 5y, ytd) with external contributions/withdrawals neutralised.
        Amounts are given natively and converted to ``reporting_currency``
        (default INR) at the latest FX; a currency with no rate yields null
        converted values and an explicit warning, never zero. Read-only.
        """
        period = period.strip().lower()
        if period not in VALID_PERIODS:
            return {
                "status": "error",
                "message": f"period must be one of: {', '.join(VALID_PERIODS)}.",
            }
        reporting_currency = reporting_currency.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", reporting_currency):
            return {
                "status": "error",
                "message": "reporting_currency must be a 3-letter currency code.",
            }
        try:
            account_public_id = uuid.UUID(account_id) if account_id else None
        except ValueError:
            return {"status": "error", "message": "Invalid account_id."}
        today = datetime.now(UTC).date()
        from_date = period_start(period, today)
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_performance"
            )
            if account_public_id is not None:
                account = await AccountRepository(session).get_by_public_id(
                    workspace_id, account_public_id
                )
                if account is None:
                    return {"status": "error", "message": "Account not found in this workspace."}
            metrics, positions = await _return_metrics_service(session).get_performance_metrics(
                workspace_id
            )
            twr = await _performance_service(session).get_time_weighted_return(
                workspace_id, from_date, today
            )
            used = {r.currency.upper() for r in metrics.by_account}
            used |= {p["currency"].upper() for p in positions}
            if twr.get("currency"):
                used.add(twr["currency"].upper())
            used.discard(reporting_currency)
            fx_as_of = effective_display_as_of()
            fx_lookup = (
                await FxRateRepository(session).get_latest_rates_for_pairs(
                    list(build_required_pairs(sorted(used), reporting_currency)), as_of=fx_as_of
                )
                if used
                else {}
            )
        report = build_performance_report(
            metrics=metrics,
            positions=positions,
            twr=twr,
            rate_for=lambda ccy: conversion_rate(ccy, reporting_currency, fx_lookup),
            reporting_currency=reporting_currency,
            account_public_id=account_public_id,
        )
        rates_used = {
            ccy: str(rate)
            for ccy in sorted(used)
            if (rate := conversion_rate(ccy, reporting_currency, fx_lookup)) is not None
        }
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "account_id": account_id,
            "period": period,
            "from_date": from_date.isoformat() if from_date else None,
            "to_date": today.isoformat(),
            "reporting_currency": reporting_currency,
            "fx_as_of": fx_as_of.isoformat() if used else None,
            "fx_rates_used": rates_used,
            **report,
        }

    @mcp.tool
    async def get_weight_history(
        workspace_id: int,
        start: str | None = None,
        end: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Paginated body-weight history, newest first (read counterpart of log_weight).

        ``start``/``end`` are ISO-8601 bounds on ``measured_at`` (naive values
        are read as UTC). ``limit`` is capped at 100. Weights are strings in kg.
        """
        validate_page(limit, offset)
        try:
            start_dt = _parse_iso_datetime(start, "start")
            end_dt = _parse_iso_datetime(end, "end")
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        if start_dt and end_dt and start_dt > end_dt:
            return {"status": "error", "message": "start must not be after end."}
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_weight_history"
            )
            items, total = await _health_service(session).list_weight_entries(
                workspace_id, start_dt, end_dt, limit, offset
            )
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "items": [item.model_dump(mode="json") for item in items],
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < int(total),
        }

    @mcp.tool
    async def list_medication_events(
        workspace_id: int,
        medication_id: str | None = None,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Paginated medication dose-event history, newest first (read counterpart
        of log_medication_event). Filter by ``medication_id`` (UUID) and/or
        ``status`` (``taken`` | ``skipped``). ``limit`` is capped at 100.
        """
        validate_page(limit, offset)
        status_filter = status.strip().lower() if status else None
        if status_filter is not None and status_filter not in {"taken", "skipped"}:
            return {"status": "error", "message": "status must be 'taken' or 'skipped'."}
        try:
            medication_public_id = uuid.UUID(medication_id) if medication_id else None
        except ValueError:
            return {"status": "error", "message": "Invalid medication_id."}
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_medication_events"
            )
            medication_repo = MedicationRepository(session)
            medication_internal_id: int | None = None
            if medication_public_id is not None:
                medication = await medication_repo.get_by_public_id(
                    workspace_id, medication_public_id
                )
                if medication is None:
                    return {"status": "error", "message": "Medication not found in this workspace."}
                medication_internal_id = medication.id
            events, total = await MedicationEventRepository(session).list_by_workspace(
                workspace_id,
                medication_id=medication_internal_id,
                status=status_filter,
                limit=limit,
                offset=offset,
            )
            medications = await medication_repo.get_by_ids(
                workspace_id, [e.medication_id for e in events]
            )
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "medication_id": medication_id,
            "status_filter": status_filter,
            "items": [
                {
                    "public_id": str(e.public_id),
                    "medication_public_id": str(medications[e.medication_id].public_id)
                    if e.medication_id in medications
                    else None,
                    "medication_name": medications[e.medication_id].name
                    if e.medication_id in medications
                    else None,
                    "scheduled_for": e.scheduled_for.isoformat(),
                    "status": e.status,
                    "logged_at": e.logged_at.isoformat(),
                    "taken_at": e.taken_at.isoformat() if e.taken_at else None,
                    "note": e.note,
                    "source_type": e.source_type,
                }
                for e in events
            ],
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(events) < int(total),
        }

    @mcp.tool
    async def get_investing_summary(workspace_id: int) -> dict[str, Any]:
        """Get a read-only investing summary for an authorized workspace."""
        return await _run_capture_tool(workspace_id, "mcp:read", "get_investing_summary", {})

    @mcp.tool
    async def get_account_balances(workspace_id: int) -> dict[str, Any]:
        """Get read-only spending-account balances for an authorized workspace."""
        return await _run_capture_tool(workspace_id, "mcp:read", "get_account_balances", {})

    @mcp.tool
    async def get_tax_summary(
        workspace_id: int,
        fy: str | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """Get the capital gains tax estimate for a workspace and financial year.

        Returns the LTCG and STCG buckets with their tax, the per-lot
        detail, the FX rates used (with their source), and the assumptions
        behind the number. This is an estimate, not a filing-ready
        liability.

        `fy` is an Indian financial year label such as "2026-27" and
        defaults to the current one.
        """
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_tax_summary"
            )
            estimate = await _tax_service(session).compute(
                workspace_id, fy=fy, account_id=account_id
            )
        if estimate.status == "reconciliation_failed":
            return {
                "status": "reconciliation_failed",
                "message": estimate.message,
                "reconciliation_failures": estimate.reconciliation_failures,
            }
        if estimate.status == "missing_fx_rate":
            return {
                "status": "missing_fx_rate",
                "message": estimate.message,
                "missing_fx_rates": estimate.missing_fx_rates,
            }
        return {
            "status": "success",
            "fy_label": estimate.fy_label,
            "currency": estimate.currency,
            "buckets": {key: bucket.as_dict() for key, bucket in estimate.buckets.items()},
            "total_gain_inr": str(estimate.total_gain_inr),
            "total_tax_inr": str(estimate.total_tax_inr),
            "effective_rate_pct": str(estimate.effective_rate_pct),
            "slab": (
                {
                    "rate_pct": str(estimate.slab.rate_pct),
                    "source": estimate.slab.source,
                    "tax_regime": estimate.slab.tax_regime,
                    "surcharge_pct": str(estimate.slab.surcharge_pct),
                    "cess_pct": str(estimate.slab.cess_pct),
                    "rebate_amount": str(estimate.slab.rebate_amount),
                    "total_income_considered": str(estimate.slab.total_income_considered),
                }
                if estimate.slab is not None
                else None
            ),
            "fx_rates": [f.as_dict() for f in estimate.fx_rates],
            "positions": [
                {
                    "symbol": lot.symbol,
                    "account_name": lot.account_name,
                    "instrument_type": lot.instrument_type,
                    "currency": lot.currency,
                    "quantity": str(lot.quantity),
                    "cost_per_unit": str(lot.cost_per_unit),
                    "acquired_at": lot.acquired_at.isoformat(),
                    "sold_at": lot.sold_at.isoformat(),
                    "long_term_mark_date": lot.long_term_mark_date.isoformat(),
                    "holding_months": lot.holding_months,
                    "is_long_term": lot.is_long_term,
                    "regime": str(lot.regime),
                    "classified_by": lot.classified_by,
                    "classification_detail": lot.classification_detail,
                    "gain_native": str(lot.gain_native),
                    "gain_inr": str(lot.gain_inr),
                }
                for lot in estimate.lots
            ],
            "assumptions": estimate.assumptions,
            "disclaimers": estimate.disclaimers,
        }

    @mcp.tool
    async def simulate_sale(
        workspace_id: int,
        holding: str,
        qty: str,
        date: str,
        price: str,
        sell_fees: str = "0",
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """Read-only what-if: the capital-gains tax of selling `qty` units of a holding.

        Nothing is sold or changed. FIFO open lots are consumed oldest first;
        each lot is classified long-term/short-term on calendar days (more
        than 12 months for Indian-listed, 24 for foreign). The tax is
        INCREMENTAL: the estimated tax for the sale's financial year with the
        sale minus without it, on top of the gains already realised that FY
        (the same data get_tax_summary uses), so the Rs 1.25 lakh Sec 112A
        exemption is only what is left. Returns per-lot gains, the Rule 115
        FX rate and its source, a status (success, insufficient_quantity,
        missing_fx_rate, reconciliation_failed) and the assumptions used.

        `holding` is a holding public_id (from list_investment_holdings), or
        a symbol; a symbol held in several accounts is rejected with the
        candidates unless `account_id` (account public_id) narrows it.
        `date` is the sale day, YYYY-MM-DD in your timezone. `qty`, `price`
        and `sell_fees` are decimal strings (price and fees in the holding's
        currency).
        """
        try:
            qty_value = parse_decimal("qty", qty, allow_zero=False, max_places=8)
            price_value = parse_decimal("price", price, allow_zero=True, max_places=8)
            fees_value = parse_decimal("sell_fees", sell_fees, allow_zero=True, max_places=4)
        except WhatIfError as exc:
            return _what_if_error(exc)
        async with postgres.async_session_maker() as session:
            user_id = await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="simulate_sale"
            )
            timezone_name = await _user_timezone_name(session, user_id)
            try:
                sale_dt = parse_sale_date(date, resolve_zone(timezone_name))
                outcome = await _tax_whatif_service(session).simulate_sale(
                    workspace_id,
                    holding_ref=holding,
                    account_id=account_id,
                    qty=qty_value,
                    sale_date=sale_dt,
                    sale_price=price_value,
                    sell_fees=fees_value,
                    timezone_name=timezone_name,
                )
            except WhatIfError as exc:
                return _what_if_error(exc)
            return simulation_response(outcome)

    @mcp.tool
    async def list_tax_lots(
        workspace_id: int,
        holding: str,
        account_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Read-only: a holding's open FIFO tax lots with their long-term countdown.

        Per lot: acquired date, quantity, cost per unit (fee-inclusive),
        whether it is long-term today, and `days_to_long_term` (calendar
        days until the first day a sale is long-term; 0 = already). The
        threshold is 12 months for Indian-listed holdings and 24 for foreign
        ones. Unrealised P&L is included when a stored price exists.

        `holding` is a holding public_id or a symbol (use `account_id` to
        disambiguate a symbol held in several accounts). Lots are paged:
        `limit` 1-500, `offset` >= 0.
        """
        if not 1 <= limit <= 500 or offset < 0:
            return {
                "status": "error",
                "code": "invalid_page",
                "message": "limit must be 1-500 and offset non-negative.",
            }
        async with postgres.async_session_maker() as session:
            user_id = await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_tax_lots"
            )
            try:
                listing = await _tax_whatif_service(session).list_lots(
                    workspace_id,
                    holding_ref=holding,
                    account_id=account_id,
                    timezone_name=await _user_timezone_name(session, user_id),
                )
            except WhatIfError as exc:
                return _what_if_error(exc)
        response = lot_listing_response(listing)
        total = len(response["lots"])
        response["lot_count"] = total
        response["lots"] = response["lots"][offset : offset + limit]
        response["offset"] = offset
        response["has_more"] = offset + limit < total
        return response

    @mcp.tool
    async def harvest_candidates(
        workspace_id: int,
        stcg_rate_percent: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read-only: unrealised-loss lots worth realising for tax, best first.

        Scans every non-zero holding's open lots against its latest stored
        price, ranks loss lots by the tax the loss would save (converted to
        INR so mixed currencies compare), and reports each lot's price date
        and whether that price is stale. Holdings with no stored price or no
        open lots are listed under `skipped_holdings`, never guessed. A
        foreign short-term loss follows your slab, so it is ranked last
        unless you pass `stcg_rate_percent` (e.g. "30").

        `limit` 1-200 caps the returned candidates (candidate_count is the
        total).
        """
        if not 1 <= limit <= 200:
            return {
                "status": "error",
                "code": "invalid_page",
                "message": "limit must be between 1 and 200.",
            }
        stcg_value = None
        if stcg_rate_percent is not None:
            try:
                stcg_value = parse_decimal("stcg_rate_percent", stcg_rate_percent, allow_zero=True)
            except WhatIfError as exc:
                return _what_if_error(exc)
            if stcg_value > 100:
                return {
                    "status": "error",
                    "code": "invalid_number",
                    "message": "stcg_rate_percent must be between 0 and 100.",
                }
        async with postgres.async_session_maker() as session:
            user_id = await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="harvest_candidates"
            )
            report = await _tax_whatif_service(session).harvest(
                workspace_id,
                stcg_rate_percent=stcg_value,
                timezone_name=await _user_timezone_name(session, user_id),
            )
        return harvest_response(report, limit)

    @mcp.tool
    async def create_portfolio_report(
        workspace_id: int,
        as_of: str,
        run_id: str,
        scalars_json: dict[str, Any] | None = None,
        foundation_json: dict[str, Any] | None = None,
        actions_md: str | None = None,
        theme_weights: dict[str, Any] | None = None,
        harvest_candidates: list[Any] | None = None,
        lrs_headroom_inr: str | None = None,
        tax_buckets: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one portfolio report run in the append-only history (cron use).

        Idempotent on ``(workspace_id, as_of, run_id)``: re-sending the same run
        returns the existing row's ``public_id`` and never overwrites it; a new
        ``run_id`` on the same day adds a new row. ``as_of`` is an ISO date;
        ``lrs_headroom_inr`` a decimal string. The denormalised columns
        (``theme_weights``, ``harvest_candidates``, ``lrs_headroom_inr``,
        ``tax_buckets``) are stored as given so dashboards need not parse
        ``foundation_json``.
        """
        try:
            as_of_date = date.fromisoformat(as_of)
        except ValueError:
            return {"status": "error", "message": "as_of must be an ISO date (YYYY-MM-DD)."}
        if not 1 <= len(run_id) <= 128:
            return {"status": "error", "message": "run_id must be 1-128 characters."}
        headroom: Decimal | None = None
        if lrs_headroom_inr is not None:
            try:
                headroom = Decimal(lrs_headroom_inr)
            except InvalidOperation:
                return {"status": "error", "message": "lrs_headroom_inr must be a decimal string."}
            if not headroom.is_finite() or abs(headroom) >= MAX_LRS_HEADROOM_INR:
                return {
                    "status": "error",
                    "message": "lrs_headroom_inr must be a finite amount below 10^16.",
                }
            headroom = headroom.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        payload_size = len(
            json.dumps(
                [
                    scalars_json,
                    foundation_json,
                    actions_md,
                    theme_weights,
                    harvest_candidates,
                    tax_buckets,
                ],
                default=str,
            )
        )
        if payload_size > MAX_PORTFOLIO_REPORT_CHARS:
            return {
                "status": "error",
                "message": f"report payload exceeds {MAX_PORTFOLIO_REPORT_CHARS} characters.",
            }
        async with postgres.async_session_maker() as session, session.begin():
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="create_portfolio_report"
            )
            recorded = await _portfolio_report_service(session).record_portfolio_report(
                workspace_id=workspace_id,
                as_of=as_of_date,
                run_id=run_id,
                scalars_json=scalars_json,
                foundation_json=foundation_json,
                actions_md=actions_md,
                theme_weights=theme_weights,
                harvest_candidates=harvest_candidates,
                lrs_headroom_inr=headroom,
                tax_buckets=tax_buckets,
            )
            report = recorded.report
            result: dict[str, Any] = {
                "status": "success",
                "created": recorded.created,
                "content_matches": recorded.content_matches,
                "public_id": str(report.public_id),
                "as_of": report.as_of.isoformat(),
                "run_id": report.run_id,
                "created_at": report.created_at.isoformat() if report.created_at else None,
            }
            if not recorded.content_matches:
                result["warning"] = (
                    "A report for this run_id already exists with different content; nothing "
                    "was changed (reports are append-only). Send a corrected report under a "
                    "new run_id."
                )
            return result

    @mcp.tool
    async def list_portfolio_reports(
        workspace_id: int,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 20,
        cursor: int | None = None,
        include_payload: bool = False,
    ) -> dict[str, Any]:
        """List portfolio report history for a workspace, newest ``as_of`` first.

        Filters on ``as_of`` (inclusive). Cursor-paginated with a total count so
        callers can verify no page was cut off. Items carry the denormalised
        columns; set ``include_payload=true`` to also get ``scalars_json``,
        ``foundation_json`` and ``actions_md`` (large -- narrow the date range and
        lower ``limit`` first).
        """
        if limit < 1 or limit > 100:
            return {"status": "error", "message": "limit must be between 1 and 100."}
        if cursor is not None and cursor < 0:
            return {"status": "error", "message": "cursor must be a non-negative integer."}
        try:
            from_d = date.fromisoformat(from_date) if from_date else None
            to_d = date.fromisoformat(to_date) if to_date else None
        except ValueError:
            return {"status": "error", "message": "from_date/to_date must be ISO dates."}
        offset = cursor or 0
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_portfolio_reports"
            )
            items, total = await _portfolio_report_service(session).repo.list_by_workspace_range(
                workspace_id, from_date=from_d, to_date=to_d, limit=limit, offset=offset
            )
        next_cursor = offset + len(items) if offset + len(items) < total else None
        return {
            "status": "success",
            "total": total,
            "next_cursor": next_cursor,
            "items": [
                {
                    "public_id": str(item.public_id),
                    "as_of": item.as_of.isoformat() if item.as_of else None,
                    "run_id": item.run_id,
                    "theme_weights": item.theme_weights,
                    "harvest_candidates": item.harvest_candidates,
                    "lrs_headroom_inr": (
                        str(item.lrs_headroom_inr) if item.lrs_headroom_inr is not None else None
                    ),
                    "tax_buckets": item.tax_buckets,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                    **(
                        {
                            "scalars_json": item.scalars_json,
                            "foundation_json": item.foundation_json,
                            "actions_md": item.actions_md,
                        }
                        if include_payload
                        else {}
                    ),
                }
                for item in items
            ],
        }

    @mcp.tool
    async def get_tax_profile(workspace_id: int) -> dict[str, Any]:
        """Get the workspace's capital gains tax configuration."""
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_tax_profile"
            )
            profile = await _tax_service(session).get_profile(workspace_id)
        return {
            "public_id": str(profile.public_id),
            "resident_status": profile.resident_status,
            "tax_regime": profile.tax_regime,
            "other_income": str(profile.other_income),
            "slab_override_pct": str(profile.slab_override_pct)
            if profile.slab_override_pct is not None
            else None,
            "surcharge_override_pct": str(profile.surcharge_override_pct)
            if profile.surcharge_override_pct is not None
            else None,
            "cess_pct": str(profile.cess_pct),
            "explicit_regime": profile.explicit_regime,
            "apply_carry_forward": bool(profile.apply_carry_forward),
            "fx_overrides": dict(profile.fx_overrides or {}),
            "notes": profile.notes,
        }

    @mcp.tool
    async def update_tax_profile(
        workspace_id: int,
        resident_status: str | None = None,
        tax_regime: str | None = None,
        other_income: str | None = None,
        slab_override_pct: str | None = None,
        surcharge_override_pct: str | None = None,
        cess_pct: str | None = None,
        explicit_regime: str | None = None,
        apply_carry_forward: bool | None = None,
        fx_overrides: dict[str, str] | None = None,
        notes: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Update the workspace's capital gains tax configuration.

        All fields are optional — only provided fields are updated.
        `fx_overrides` accepts keys in "YYYY-MM" (legacy monthly) or "YYYY-MM-DD:CURRENCY" (daily) format.
        """
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": "Update tax profile? Confirm to continue.",
            }

        changes: dict[str, Any] = {}
        if resident_status is not None:
            changes["resident_status"] = resident_status
        if tax_regime is not None:
            changes["tax_regime"] = tax_regime
        if other_income is not None:
            changes["other_income"] = other_income
        if slab_override_pct is not None:
            changes["slab_override_pct"] = slab_override_pct
        if surcharge_override_pct is not None:
            changes["surcharge_override_pct"] = surcharge_override_pct
        if cess_pct is not None:
            changes["cess_pct"] = cess_pct
        if explicit_regime is not None:
            changes["explicit_regime"] = explicit_regime
        if apply_carry_forward is not None:
            changes["apply_carry_forward"] = apply_carry_forward
        if fx_overrides is not None:
            changes["fx_overrides"] = fx_overrides
        if notes is not None:
            changes["notes"] = notes

        if not changes:
            return {"status": "error", "message": "No fields to update."}

        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="update_tax_profile"
            )
            profile = await _tax_service(session).update_profile(workspace_id, changes)
            await session.commit()
        return {
            "public_id": str(profile.public_id),
            "resident_status": profile.resident_status,
            "tax_regime": profile.tax_regime,
            "other_income": str(profile.other_income),
            "slab_override_pct": str(profile.slab_override_pct)
            if profile.slab_override_pct is not None
            else None,
            "surcharge_override_pct": str(profile.surcharge_override_pct)
            if profile.surcharge_override_pct is not None
            else None,
            "cess_pct": str(profile.cess_pct),
            "explicit_regime": profile.explicit_regime,
            "apply_carry_forward": bool(profile.apply_carry_forward),
            "fx_overrides": dict(profile.fx_overrides or {}),
            "notes": profile.notes,
        }

    @mcp.tool
    async def set_fx_rate(
        workspace_id: int,
        date_str: str,
        currency: str,
        rate: str,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Set a daily Rule 115 FX rate for a specific date and currency.

        `date_str`: "YYYY-MM-DD" — the Rule 115 specified date (last day of month before sale month).
        `currency`: e.g., "USD", "GBP".
        `rate`: SBI TT buying rate ₹ per 1 unit (decimal string).
        The key is stored as "YYYY-MM-DD:CURRENCY" for multi-currency workspaces.
        """
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": f"Set FX rate for {date_str} ({currency.upper()}) to {rate}? Confirm to continue.",
            }

        # Validate that the date is a real calendar date, not only the right shape.
        try:
            parsed_date = date.fromisoformat(date_str)
        except ValueError:
            return {"status": "error", "message": "date must be a valid YYYY-MM-DD date."}
        if parsed_date.isoformat() != date_str:
            return {"status": "error", "message": "date must be YYYY-MM-DD format."}

        currency = currency.upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            return {"status": "error", "message": "currency must be a 3-letter code (e.g., USD)."}

        try:
            rate_value = Decimal(rate)
        except (InvalidOperation, ValueError):
            return {"status": "error", "message": "rate must be a decimal string."}
        if not rate_value.is_finite() or rate_value <= 0 or rate_value > Decimal("1000"):
            return {"status": "error", "message": "rate must be finite and between 0 and 1000."}

        composite_key = f"{date_str}:{currency}"
        # Canonical text (no exponent / padding forms) so get_fx_rate returns what was meant.
        rate_text = format(rate_value, "f")

        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="set_fx_rate"
            )
            service = _tax_service(session)
            # A deliberate correction: replace any existing value. The merge runs under a
            # row lock so a concurrent writer's entries are not lost.
            await service.set_fx_overrides(workspace_id, {composite_key: rate_text}, overwrite=True)
            profile = await service.get_profile(workspace_id)
            await session.commit()

        return {
            "status": "success",
            "message": f"FX rate set for {date_str} ({currency}): {rate_text}",
            "key": composite_key,
            "rate": rate_text,
            "fx_overrides": dict(profile.fx_overrides or {}),
        }

    @mcp.tool
    async def set_fx_rates(
        workspace_id: int,
        rates: list[dict[str, str]],
        overwrite_existing: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Backfill many daily Rule 115 FX rates in one call.

        ``rates`` is a list of ``{"date": "YYYY-MM-DD", "currency": "USD", "rate": "83.4"}``
        (up to 400 -- a financial year of daily rates). Use ``list_missing_fx_rates`` to find
        the gaps. Everything is validated first and nothing is written if any entry is bad;
        the error names the entry (``rates[3]: ...``). Dates may not be in the future.

        A rate already stored for the same date + currency is **kept** (reported under
        ``skipped_existing``) unless ``overwrite_existing=true``; an identical value is
        ``unchanged``. Without ``confirmed=true`` nothing is written and the result is a
        preview of the counts (``add`` / ``update`` / ``skip_existing`` / ``unchanged``).
        Concurrent calls merge safely.
        """
        try:
            entries = parse_fx_entries(rates)
        except AppValidationError as exc:
            return {"status": "error", "message": exc.detail}

        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="set_fx_rates"
            )
            service = _tax_service(session)
            if not confirmed:
                preview = await service.set_fx_overrides(
                    workspace_id, entries, overwrite=overwrite_existing, apply=False
                )
                return {
                    "status": "needs_confirmation",
                    "needs_confirmation": True,
                    "message": (
                        f"Store {len(entries)} FX rate(s)? "
                        f"{len(preview.added)} new, {len(preview.updated)} replaced, "
                        f"{len(preview.skipped_existing)} kept as already set, "
                        f"{len(preview.unchanged)} identical. Confirm to continue."
                    ),
                    "counts": {
                        "add": len(preview.added),
                        "update": len(preview.updated),
                        "skip_existing": len(preview.skipped_existing),
                        "unchanged": len(preview.unchanged),
                    },
                }
            result = await service.set_fx_overrides(
                workspace_id, entries, overwrite=overwrite_existing
            )
            await session.commit()
        return {
            "status": "success",
            "counts": {
                "added": len(result.added),
                "updated": len(result.updated),
                "skipped_existing": len(result.skipped_existing),
                "unchanged": len(result.unchanged),
            },
            "skipped_existing": result.skipped_existing,
        }

    @mcp.tool
    async def get_lrs_summary(
        workspace_id: int,
        fy: str | None = None,
    ) -> dict[str, Any]:
        """FY outward remittance vs the USD 250,000 LRS cap, with TCS status.

        Built from INR-funded transfers into foreign-currency investing accounts
        (e.g. Paasa / a US brokerage) in the workspace; repatriations and INR
        brokerage funding are excluded. `fy` is an Indian financial year label
        such as "2026-27" (1 Apr - 31 Mar, IST) and defaults to the current one.
        Money values are decimal strings. See `assumptions` for limits.
        """
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_lrs_summary"
            )
            service = LrsService(
                CapitalTransferRepository(session), fx_rate_repository=FxRateRepository(session)
            )
            try:
                summary = await service.get_lrs_summary(workspace_id, fy)
            except ValidationError as exc:
                return {"status": "error", "message": exc.detail}
        return {
            "status": "success",
            "workspace_id": workspace_id,
            **summary,
            "fy_start": summary["fy_start"].isoformat(),
            "fy_end": summary["fy_end"].isoformat(),
        }

    @mcp.tool
    async def get_fx_rate(
        workspace_id: int,
        date_str: str,
        currency: str,
    ) -> dict[str, Any]:
        """Read the stored Rule 115 FX rate for a date + currency.

        Reads the user-provided rate written via `set_fx_rate` (key
        "YYYY-MM-DD:CURRENCY"). Returns status "not_found" (the REST 404) when no
        rate is stored for that exact date and currency.
        """
        try:
            parsed = date.fromisoformat(date_str)
        except ValueError:
            return {"status": "error", "message": "date_str must be a valid YYYY-MM-DD date."}
        if parsed.isoformat() != date_str:
            return {"status": "error", "message": "date_str must be YYYY-MM-DD format."}
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_fx_rate"
            )
            try:
                code = normalize_currency_code(currency)
                rate = await _tax_service(session).get_fx_rate(workspace_id, parsed, code)
            except ValidationError as exc:
                return {"status": "error", "message": exc.detail}
        if rate is None:
            return {
                "status": "not_found",
                "message": f"No FX rate stored for {date_str} ({code}).",
            }
        return {
            "status": "success",
            "date": date_str,
            "currency": code,
            "rate": rate,
            "source": "user_provided",
        }

    @mcp.tool
    async def list_missing_fx_rates(
        workspace_id: int,
        from_date: str,
        to_date: str,
        currency: str,
    ) -> dict[str, Any]:
        """Dates in [from_date, to_date] (YYYY-MM-DD, max 366 days) with no stored Rule 115 rate."""
        try:
            from_d = date.fromisoformat(from_date)
            to_d = date.fromisoformat(to_date)
        except ValueError:
            return {"status": "error", "message": "dates must be valid YYYY-MM-DD dates."}
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_missing_fx_rates"
            )
            try:
                code = normalize_currency_code(currency)
                missing = await _tax_service(session).list_missing_fx_rates(
                    workspace_id, from_d, to_d, code
                )
            except ValidationError as exc:
                return {"status": "error", "message": exc.detail}
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "from_date": from_d.isoformat(),
            "to_date": to_d.isoformat(),
            "currency": code,
            "missing": [d.isoformat() for d in missing],
            "missing_count": len(missing),
        }

    @mcp.tool
    async def update_investing_account_status(
        workspace_id: int,
        account_id: str,
        market_scope: str | None = None,
        exchange_name: str | None = None,
        symbol: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Set a brokerage account's market scope, or flag one holding's exchange.

        The capital-gains tax regime depends on where a security is listed.
        `market_scope` sets the account-level default ("india", "foreign" or
        "mixed") used when the reference security master has no entry for a
        ticker.

        `exchange_name` sets the exchange on a holding. With `symbol` it
        applies to that one holding and also sets its country override,
        which beats the account default. Without `symbol` it applies to
        every holding in the account whose exchange is still unset.

        Holdings whose exchange is already set are skipped, and an
        unrecognised exchange name leaves the country unset rather than
        guessing.
        """
        if market_scope is not None and market_scope not in {"india", "foreign", "mixed"}:
            return {
                "status": "error",
                "message": "market_scope must be 'india', 'foreign' or 'mixed'.",
            }
        if not market_scope and not exchange_name:
            return {
                "status": "error",
                "message": "Supply market_scope and/or exchange_name.",
            }
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    f"Update the market status for account {account_id}? Confirm to continue."
                ),
                "account_id": account_id,
                "market_scope": market_scope,
                "exchange_name": exchange_name,
                "symbol": symbol,
            }
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:write",
                tool="update_investing_account_status",
            )
            service = _tax_service(session)
            try:
                account = await service.set_account_market_scope(
                    workspace_id, account_id, market_scope
                )
            except ValueError as exc:
                return {"status": "error", "message": str(exc)}

            updated: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            if exchange_name:
                mic, country = resolve_exchange(exchange_name)
                instruments = await service.list_account_instruments(workspace_id, account_id)
                if symbol:
                    instruments = [i for i in instruments if i.symbol.upper() == symbol.upper()]
                    if not instruments:
                        return {
                            "status": "error",
                            "message": f"No holding named '{symbol}' in this account.",
                        }
                for instrument in instruments:
                    if instrument.exchange and not symbol:
                        skipped.append({
                            "symbol": instrument.symbol,
                            "reason": f"exchange already set to {instrument.exchange}",
                        })
                        continue
                    instrument.exchange = exchange_name
                    if country:
                        instrument.exchange_country_override = country
                    instrument.updated_at = datetime.now(UTC)
                    session.add(instrument)
                    updated.append({
                        "symbol": instrument.symbol,
                        "exchange": instrument.exchange,
                        "exchange_country_override": instrument.exchange_country_override,
                    })
                await session.flush()

            # Error returns above leave the transaction uncommitted (the session closes
            # and rolls back); only a fully applied change is committed.
            await session.commit()
            return {
                "status": "success",
                "account": {
                    "public_id": str(account.public_id),
                    "name": account.name,
                    "market_scope": account.market_scope,
                },
                "instruments_updated": updated,
                "instruments_skipped": skipped,
                "notes": [
                    "The account market scope is a default; a holding-level "
                    "exchange country override takes precedence over it.",
                    "An unrecognised exchange name leaves the country unset rather than guessing.",
                ],
            }

    @mcp.tool
    async def set_holding_compliance(
        workspace_id: int,
        holding_id: str,
        verdict: str,
        sources: list[dict[str, Any]],
        as_of_date: str,
        impure_income_pct: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Record (create or replace) the halal compliance verdict for a holding.

        Requires the dedicated mcp:research grant and explicit confirmation.
        Idempotent: the latest call wins and the previous value stays in the
        audit log.

        `verdict` is one of "halal", "non_halal", "permissible" (not fully
        halal but acceptable once income is purified) or "unknown".
        `sources` is 1-10 citations, each {"screener_name", "url" (http/https),
        "rule_reference"} plus optional "verdict" and "as_of_date" (YYYY-MM-DD)
        so screeners that disagree stay visible side by side. `as_of_date` is
        the date of the verdict (not in the future). `impure_income_pct` is the
        screener-stated share (0-100, decimal string) of non-permissible income;
        it drives get_purification_estimate and is never inferred.
        """
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    f"Record compliance verdict '{verdict}' (as of {as_of_date}, "
                    f"{len(sources)} source(s)) for holding {holding_id}, replacing any "
                    "existing record? Confirm to continue."
                ),
                "holding_id": holding_id,
                "verdict": verdict,
                "as_of_date": as_of_date,
                "impure_income_pct": impure_income_pct,
                "source_count": len(sources),
            }
        try:
            holding_public_id = uuid.UUID(holding_id)
            payload = HoldingComplianceUpsert.model_validate({
                "verdict": verdict,
                "sources": sources,
                "as_of_date": as_of_date,
                "impure_income_pct": impure_income_pct,
            })
        except (TypeError, ValueError, PydanticValidationError) as exc:
            return {"status": "error", "message": f"Invalid compliance record: {exc}"}

        async with postgres.async_session_maker() as session:
            user_id = await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:research",
                tool="set_holding_compliance",
            )
            try:
                compliance = await _compliance_service(session).set_compliance(
                    workspace_id,
                    holding_public_id,
                    payload,
                    actor_id=user_id,
                    audit_logger=AuditLogger(session),
                )
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                "entity_type": "holding_compliance",
                "compliance": compliance.model_dump(mode="json"),
                "summary": f"Saved '{compliance.verdict}' verdict with {len(payload.sources)} source(s).",
            }

    @mcp.tool
    async def get_holding_compliance(workspace_id: int, holding_id: str) -> dict[str, Any]:
        """Get the stored halal compliance verdict for a holding.

        Returns the verdict, its date, the non-permissible income percentage
        and every cited source (screener name, URL, rule reference, and each
        screener's own verdict/date). `sources_disagree` is true when the
        cited screeners gave different verdicts. When the holding has no
        record yet, `compliance` is null.
        """
        try:
            holding_public_id = uuid.UUID(holding_id)
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid holding_id."}
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="get_holding_compliance",
            )
            try:
                compliance = await _compliance_service(session).get_compliance(
                    workspace_id, holding_public_id
                )
            except (APIError, ValueError) as exc:
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                "workspace_id": workspace_id,
                "holding_id": holding_id,
                "compliance": compliance.model_dump(mode="json") if compliance else None,
                "message": (None if compliance else "No compliance record for this holding yet."),
            }

    @mcp.tool
    async def get_purification_estimate(
        workspace_id: int,
        holding_id: str | None = None,
        dividend_id: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Estimate the halal purification (charity) amount for income.

        Pass exactly one of `holding_id` (all income recorded against that
        holding, optionally limited by `from_date`/`to_date` as YYYY-MM-DD)
        or `dividend_id` (one income event). Amount = gross income x the
        holding's recorded non-permissible-income %, as a decimal string in
        the holding's currency. If no percentage is recorded the amount is
        "0.00" and `basis` says why; nothing is guessed. Estimate only, not
        a fatwa: see the `disclaimer` field.
        """
        try:
            holding_public_id = uuid.UUID(holding_id) if holding_id else None
            dividend_public_id = uuid.UUID(dividend_id) if dividend_id else None
            start = date.fromisoformat(from_date) if from_date else None
            end = date.fromisoformat(to_date) if to_date else None
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {
                "status": "error",
                "message": "Invalid holding_id, dividend_id, from_date or to_date.",
            }
        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="get_purification_estimate",
            )
            try:
                estimate = await _compliance_service(session).get_purification_estimate(
                    workspace_id,
                    holding_public_id=holding_public_id,
                    dividend_public_id=dividend_public_id,
                    from_date=start,
                    to_date=end,
                )
            except (APIError, ValueError) as exc:
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                "workspace_id": workspace_id,
                "estimate": estimate.model_dump(mode="json"),
            }

    @mcp.tool
    async def set_holding_theme_tags(
        workspace_id: int,
        holdings: list[dict[str, Any]],
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Set theme tags on holdings.

        Requires mcp:research scope and explicit confirmation.
        Each holding entry: {"holding_id": str, "tags": [{"theme": str, "target_weight_pct": float?}]}
        """
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": f"Set theme tags on {len(holdings)} holdings? Confirm to continue.",
                "holdings": holdings,
            }
        if not 1 <= len(holdings) <= 200:
            return {
                "status": "error",
                "message": "holdings must contain between 1 and 200 entries.",
            }

        validated = []
        for i, h in enumerate(holdings):
            try:
                holding_public_id = uuid.UUID(h["holding_id"])
                tag_list = h.get("tags", [])
                if not isinstance(tag_list, list) or len(tag_list) > 20:
                    return {
                        "status": "error",
                        "message": f"holding {i}: tags must be a list of 0-20 items.",
                    }
                tags = []
                for t in tag_list:
                    tag = str(t["theme"]).strip()
                    if not tag or len(tag) > 64:
                        return {
                            "status": "error",
                            "message": f"holding {i}: theme must be 1-64 chars.",
                        }
                    tw = t.get("target_weight_pct")
                    if tw is not None:
                        tw = float(tw)
                        if tw < 0 or tw > 100:
                            return {
                                "status": "error",
                                "message": f"holding {i}: target_weight_pct must be 0-100.",
                            }
                    tags.append(ThemeTagInput(theme=tag, target_weight_pct=tw))
                validated.append(HoldingThemeTagsInput(holding_id=holding_public_id, tags=tags))
            except (KeyError, TypeError, ValueError) as exc:
                return {"status": "error", "message": f"holding {i}: invalid entry: {exc}"}

        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:research", tool="set_holding_theme_tags"
            )
            try:
                result = await _theme_tag_service(session).set_tags(workspace_id, validated)
                await session.commit()
            except (APIError, ValueError) as exc:
                await session.rollback()
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                **result.model_dump(mode="json"),
                "summary": f"Updated theme tags on {len(validated)} holding(s).",
            }

    @mcp.tool
    async def get_lookthrough_exposure(
        workspace_id: int,
        as_of_date: str | None = None,
        group_by: GroupBy | None = None,
    ) -> dict[str, Any]:
        """Get look-through exposure by theme/company/sector/country.

        Omit ``group_by`` for every grouping, or pass one of company, sector,
        country, theme to limit the response to it.

        Uses exactly one latest constituent snapshot per fund (newest by as_of_date, fetched_at).
        OTHER-* placeholder rows and unmapped companies go to "not itemised" (never rescaled).
        """
        try:
            as_of = date.fromisoformat(as_of_date) if as_of_date else datetime.now(UTC).date()
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            return {"status": "error", "message": "Invalid as_of_date. Use YYYY-MM-DD."}

        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_lookthrough_exposure"
            )
            try:
                result = await _theme_exposure_service(session).lookthrough_exposure(
                    workspace_id, as_of, group_by=group_by
                )
            except PydanticValidationError as exc:
                return {"status": "error", "message": str(exc)}
            return {
                "status": "success",
                "workspace_id": workspace_id,
                **result.model_dump(mode="json"),
            }

    @mcp.tool
    async def get_allocation_vs_target(
        workspace_id: int,
        new_cash: str = "0",
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        """Allocation vs target with optional new-cash deployment.

        ``new_cash`` defaults to 0. If positive, computes buy-only deployment to
        underweight themes.
        Never suggests sells. Largest-remainder rounding ensures exact sums.
        """
        try:
            as_of = date.fromisoformat(as_of_date) if as_of_date else datetime.now(UTC).date()
            cash = Decimal(new_cash)
        except (
            TypeError,
            ValueError,
            decimal.InvalidOperation,
            PydanticValidationError,
            AppValidationError,
        ):
            return {"status": "error", "message": "Invalid as_of_date or new_cash."}

        async with postgres.async_session_maker() as session:
            await authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_allocation_vs_target"
            )
            try:
                result = await _theme_exposure_service(session).allocation_vs_target(
                    workspace_id, as_of, new_cash=cash
                )
                return {
                    "status": "success",
                    "workspace_id": workspace_id,
                    **result.model_dump(mode="json"),
                }
            except AppValidationError as exc:
                return {"status": "error", "message": exc.detail}

    return mcp


def create_mcp_asgi_app(mcp: FastMCP, path: str = "/mcp"):
    """Build the authenticated Streamable HTTP app with DNS-rebinding protection."""
    base_url = urlparse(settings.MCP_BASE_URL or "")
    allowed_hosts = [
        item.strip() for item in settings.MCP_ALLOWED_HOSTS.split(",") if item.strip()
    ] or ([base_url.netloc] if base_url.netloc else [])
    default_origin = (
        f"{base_url.scheme}://{base_url.netloc}" if base_url.scheme and base_url.netloc else None
    )
    configured_origins = [
        settings._normalize_origin(item.strip())
        for item in settings.MCP_ALLOWED_ORIGINS.split(",")
        if item.strip()
    ]
    # MCP has its own transport-level Origin policy. Keep it independent from
    # the API CORS/CSRF settings so MCP clients can be scoped separately.
    # Add browser-based clients (for example, MCP Inspector) explicitly.
    allowed_origins = configured_origins or ([default_origin] if default_origin else [])
    return mcp.http_app(
        path=path,
        host_origin_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
