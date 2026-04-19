from fastapi import Depends, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workflows import UserRegistrationWorkflow
from app.auth.repository import UserRepository
from app.auth.service import AuthService
from app.core.database.postgres import get_db_session
from app.platform.repository import MembershipRepository, WorkspaceRepository
from app.platform.service import WorkspaceService
from app.spending.repository import BudgetRepository, CategoryRepository, TransactionRepository
from app.spending.service import BudgetService, CategoryService, TransactionService
from app.todo.repository import TodoRepository
from app.todo.service import TodoService

limiter = Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def get_user_repo(session: AsyncSession = Depends(get_db_session)) -> UserRepository:
    return UserRepository(session)


async def get_auth_service(repo: UserRepository = Depends(get_user_repo)) -> AuthService:
    return AuthService(repo)


async def get_current_user(request: Request) -> dict:
    if not hasattr(request.state, "user_id") or not request.state.user_id:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
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


async def get_current_workspace_id(
    request: Request, workspace_service: WorkspaceService = Depends(get_workspace_service)
) -> int:
    if not hasattr(request.state, "user_id") or not request.state.user_id:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Stage 1: resolve the user's first/default workspace.
    workspaces = await workspace_service.get_user_workspaces(request.state.user_id)
    if not workspaces:
        workspace = await workspace_service.ensure_default_workspace(
            request.state.user_id, request.state.username
        )
        return workspace.id

    return workspaces[0].id


# ---------------------------------------------------------------------------
# Spending  (must be defined BEFORE get_user_registration_workflow)
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


# ---------------------------------------------------------------------------
# Registration workflow  (needs spending category service)
# ---------------------------------------------------------------------------


async def get_user_registration_workflow(
    auth_service: AuthService = Depends(get_auth_service),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
    category_service: CategoryService = Depends(get_spending_category_service),
) -> UserRegistrationWorkflow:
    return UserRegistrationWorkflow(auth_service, workspace_service, category_service)
