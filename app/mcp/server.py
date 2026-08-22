"""Authenticated MCP tools and resources for Lifestack."""

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token

from app.auth.models import User
from app.capture.tools import AgentTools
from app.config import settings
from app.core.database import postgres
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
    HoldingPriceRepository,
    HoldingRepository,
    PortfolioSnapshotRepository,
)
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
