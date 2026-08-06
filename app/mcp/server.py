"""Authenticated MCP tools for Lifestack."""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from fastmcp import FastMCP

from app.config import settings
from app.core.database import postgres
from app.finance.repository import (
    AccountRepository,
    CurrencyRepository,
    FinanceSettingRepository,
    FxRateRepository,
    NetWorthSnapshotRepository,
)
from app.finance.service import AccountService, NetWorthService
from app.investing.performance_service import InvestingSummaryService
from app.investing.repository import (
    CashBalanceRepository,
    HoldingPriceRepository,
    HoldingRepository,
    PortfolioSnapshotRepository,
)
from app.mcp.auth import LifestackTokenVerifier
from app.mcp.security import authorize_workspace, validate_page
from app.spending.repository import (
    BudgetRepository,
    CategoryGroupRepository,
    CategoryRepository,
)
from app.spending.service import BudgetService
from app.todo.repository import TodoRepository
from app.todo.schemas import TodoCreate, TodoResponse
from app.todo.service import TodoService


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
