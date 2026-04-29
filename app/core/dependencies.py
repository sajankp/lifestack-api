from fastapi import Depends, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workflows import UserRegistrationWorkflow
from app.auth.repository import AuthSessionRepository, UserRepository
from app.auth.service import AuthService
from app.config import settings
from app.core.database.postgres import get_db_session
from app.core.exceptions import UnauthorizedError
from app.platform.repository import MembershipRepository, WorkspaceRepository
from app.platform.service import WorkspaceService
from app.spending.repository import BudgetRepository, CategoryRepository, TransactionRepository
from app.spending.service import BudgetService, CategoryService, TransactionService
from app.todo.repository import TodoRepository
from app.todo.service import TodoService


def _rate_limit_key_func(request: Request) -> str:
    """Use authenticated user ID when available, fall back to IP."""
    if hasattr(request.state, "user_id") and request.state.user_id:
        return f"user:{request.state.user_id}"
    return get_remote_address(request)


limiter = Limiter(
    key_func=_rate_limit_key_func,
    storage_uri=settings.RATE_LIMIT_STORAGE_URI,
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def get_user_repo(session: AsyncSession = Depends(get_db_session)) -> UserRepository:
    return UserRepository(session)


async def get_auth_session_repo(
    session: AsyncSession = Depends(get_db_session),
) -> AuthSessionRepository:
    return AuthSessionRepository(session)


async def get_auth_service(
    repo: UserRepository = Depends(get_user_repo),
    session_repo: AuthSessionRepository = Depends(get_auth_session_repo),
) -> AuthService:
    return AuthService(repo, session_repo)


async def get_current_user(request: Request) -> dict:
    if not hasattr(request.state, "user_id") or not request.state.user_id:
        raise UnauthorizedError(detail="Not authenticated")
    return {"id": request.state.user_id, "username": request.state.username}


# ---------------------------------------------------------------------------
# Todo
# ---------------------------------------------------------------------------


async def get_todo_repo(session: AsyncSession = Depends(get_db_session)) -> TodoRepository:
    return TodoRepository(session)


async def get_todo_service(repo: TodoRepository = Depends(get_todo_repo)) -> TodoService:
    return TodoService(repo)


# ---------------------------------------------------------------------------
# Platform (workspaces)
# ---------------------------------------------------------------------------


async def get_workspace_repo(
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceRepository:
    return WorkspaceRepository(session)


async def get_membership_repo(
    session: AsyncSession = Depends(get_db_session),
) -> MembershipRepository:
    return MembershipRepository(session)


async def get_workspace_service(
    workspace_repo: WorkspaceRepository = Depends(get_workspace_repo),
    membership_repo: MembershipRepository = Depends(get_membership_repo),
) -> WorkspaceService:
    return WorkspaceService(workspace_repo, membership_repo)


# ---------------------------------------------------------------------------
# Spending  (must be defined BEFORE get_current_workspace_id and
# get_user_registration_workflow)
# ---------------------------------------------------------------------------


async def get_category_repo(
    session: AsyncSession = Depends(get_db_session),
) -> CategoryRepository:
    return CategoryRepository(session)


async def get_transaction_repo(
    session: AsyncSession = Depends(get_db_session),
) -> TransactionRepository:
    return TransactionRepository(session)


async def get_budget_repo(
    session: AsyncSession = Depends(get_db_session),
) -> BudgetRepository:
    return BudgetRepository(session)


async def get_spending_category_service(
    repo: CategoryRepository = Depends(get_category_repo),
) -> CategoryService:
    return CategoryService(repo)


async def get_spending_transaction_service(
    tx_repo: TransactionRepository = Depends(get_transaction_repo),
    cat_repo: CategoryRepository = Depends(get_category_repo),
) -> TransactionService:
    return TransactionService(tx_repo, cat_repo)


async def get_spending_budget_service(
    budget_repo: BudgetRepository = Depends(get_budget_repo),
    cat_repo: CategoryRepository = Depends(get_category_repo),
) -> BudgetService:
    return BudgetService(budget_repo, cat_repo)


async def get_current_workspace_id(
    request: Request,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
    category_service: CategoryService = Depends(get_spending_category_service),
) -> int:
    if not hasattr(request.state, "user_id") or not request.state.user_id:
        raise UnauthorizedError(detail="Not authenticated")

    # Stage 1: resolve the user's first/default workspace.
    workspaces = await workspace_service.get_user_workspaces(request.state.user_id)
    if not workspaces:
        workspace = await workspace_service.ensure_default_workspace(
            request.state.user_id, request.state.username
        )
        await category_service.provision_default_categories(workspace.id)
        return workspace.id

    return workspaces[0].id


# ---------------------------------------------------------------------------
# Registration workflow  (needs spending category service)
# ---------------------------------------------------------------------------


async def get_user_registration_workflow(
    auth_service: AuthService = Depends(get_auth_service),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
    category_service: CategoryService = Depends(get_spending_category_service),
) -> UserRegistrationWorkflow:
    return UserRegistrationWorkflow(auth_service, workspace_service, category_service)
