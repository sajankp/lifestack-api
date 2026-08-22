"""Authenticated MCP tools and resources for Lifestack."""

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from pydantic import ValidationError as PydanticValidationError

from app.auth.models import User
from app.capture.tools import AgentTools
from app.config import settings
from app.core.audit import AuditLogger
from app.core.currency import (
    build_required_pairs,
    convert_amount,
    effective_display_as_of,
    fx_rates_used,
)
from app.core.database import postgres
from app.core.exceptions import APIError
from app.finance.models import AccountType
from app.finance.repository import (
    AccountRepository,
    CurrencyRepository,
    FinanceSettingRepository,
    FxRateRepository,
    NetWorthSnapshotRepository,
)
from app.finance.service import AccountService, NetWorthService
from app.health.repository import MedicationRepository
from app.investing.performance_service import InvestingSummaryService
from app.investing.repository import (
    CashBalanceRepository,
    CompanyRepository,
    DividendRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InstrumentConstituentRepository,
    InstrumentRepository,
    InvestingOrderRepository,
    PortfolioSnapshotRepository,
)
from app.investing.schemas import (
    DividendCreate,
    DividendResponse,
    InstrumentConstituentUpsert,
)
from app.investing.service import ConstituentService, DividendService, HoldingService
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
        )
        result = await getattr(agent_tools, tool_name)(**kwargs)
        if result.get("status") == "success":
            await session.commit()
        else:
            await session.rollback()
        return result


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


def _dividend_service(session: Any) -> DividendService:
    return DividendService(
        DividendRepository(session),
        CashBalanceRepository(session),
        AccountRepository(session),
        HoldingRepository(session),
        CurrencyRepository(session),
    )


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
                "account_type": account.account_type.value,
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
        except (TypeError, ValueError):
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
                workspace_id,
                user_id,
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
        except (TypeError, ValueError):
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
                holding.currency.upper()
                for holding in raw_holdings
                if holding.quantity != 0
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
        """Read the latest sourced ETF or mutual-fund constituent snapshot."""
        try:
            instrument_id = uuid.UUID(instrument_public_id)
            as_of = date.fromisoformat(as_of_date) if as_of_date else datetime.now(UTC).date()
        except (TypeError, ValueError):
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
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Replace one sourced ETF or mutual-fund constituent snapshot.

        Requires the dedicated mcp:research grant and explicit confirmation.
        Each constituent must include a ticker or ISIN and a decimal weight.
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
        except (TypeError, ValueError):
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
        except (TypeError, ValueError):
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
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "needs_confirmation": True,
                "message": (
                    f"Record {income_type} income of {gross_amount} {currency} for "
                    f"{symbol or 'the brokerage account'} on {pay_date}? Confirm to continue."
                ),
                "account_id": account_id,
                "symbol": symbol,
                "income_type": income_type,
                "gross_amount": gross_amount,
                "tax_withheld": tax_withheld,
                "currency": currency,
                "pay_date": pay_date,
            }

        try:
            payload = DividendCreate(
                account_id=uuid.UUID(account_id),
                symbol=symbol,
                income_type=income_type,
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
                "summary": f"Recorded {payload.income_type} income of {payload.net_amount} {payload.currency}.",
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
        limit: int = 10,
    ) -> dict[str, Any]:
        """Find bounded expense candidates in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:read",
            "find_spending_transactions",
            {
                "from_day": from_day,
                "to_day": to_day,
                "category_name": category_name,
                "amount": amount,
                "search": search,
                "account_name": account_name,
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
    ) -> dict[str, Any]:
        """Create an expense in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:write",
            "log_spending_transaction",
            {
                "amount": amount,
                "category_name": category_name,
                "description": description,
                "account_name": account_name,
                "occurred_at": occurred_at,
                "tags": tags,
                "allow_duplicate": allow_duplicate,
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
        limit: int = 10,
    ) -> dict[str, Any]:
        """List expenses for one local calendar day in an authorized workspace."""
        return await _run_capture_tool(
            workspace_id,
            "mcp:read",
            "list_spending_transactions",
            {
                "day": day,
                "category_name": category_name,
                "amount": amount,
                "search": search,
                "account_name": account_name,
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
    async def get_investing_summary(workspace_id: int) -> dict[str, Any]:
        """Get a read-only investing summary for an authorized workspace."""
        return await _run_capture_tool(workspace_id, "mcp:read", "get_investing_summary", {})

    @mcp.tool
    async def get_account_balances(workspace_id: int) -> dict[str, Any]:
        """Get read-only spending-account balances for an authorized workspace."""
        return await _run_capture_tool(workspace_id, "mcp:read", "get_account_balances", {})

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
