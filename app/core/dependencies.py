from fastapi import Depends, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workflows import UserRegistrationWorkflow
from app.auth.repository import AuthSessionRepository, UserRepository
from app.auth.service import AuthService
from app.config import settings
from app.core.audit import AuditLogger
from app.core.auth import get_user_info_from_token
from app.core.database.postgres import get_db_session
from app.core.exceptions import CSRFFailedError, UnauthorizedError
from app.finance.repository import (
    AccountRepository,
    CapitalTransferRepository,
    CurrencyRepository,
    FinanceSettingRepository,
    FxRateRepository,
)
from app.finance.service import (
    AccountService,
    CapitalTransferService,
    CurrencyService,
    FinanceSettingService,
    FxRateService,
)
from app.investing.repository import (
    CashBalanceRepository,
    CompanyRepository,
    HoldingRepository,
    InstrumentConstituentRepository,
    InstrumentRepository,
)
from app.investing.service import (
    CashBalanceService,
    ConstituentService,
    ExposureAnalyticsService,
    HoldingService,
    InstrumentService,
    InvestingSummaryService,
)
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
    enabled=settings.RATE_LIMIT_ENABLED,
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


async def get_current_user(
    request: Request,
    auth_session_repo: AuthSessionRepository = Depends(get_auth_session_repo),
) -> dict:
    token = request.cookies.get("access_token")
    token_from_cookie = token is not None

    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

    if not token:
        raise UnauthorizedError(detail="Not authenticated")

    username, user_id, sid, default_workspace_id = get_user_info_from_token(token)

    try:
        uid = int(user_id)
    except (ValueError, TypeError):
        uid = user_id

    if token_from_cookie and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("Origin")
        referer = request.headers.get("Referer")

        # Determine which source to validate (Origin preferred)
        source = origin or referer
        if not source:
            raise CSRFFailedError(
                detail="Origin or Referer header is required for cookie-authenticated requests"
            )

        try:
            normalized_source = settings._normalize_origin(source)
        except ValueError:
            raise CSRFFailedError(
                detail=f"{'Origin' if origin else 'Referer'} header is invalid"
            ) from None

        if (
            not settings.csrf_trusted_origins
            or normalized_source not in settings.csrf_trusted_origins
        ):
            source_name = "Origin" if origin else "Referer"
            raise CSRFFailedError(
                detail=f"{source_name} is not allowed for cookie-authenticated requests"
            )

    auth_session = await auth_session_repo.get_active_by_sid(sid, uid)
    if not auth_session:
        raise UnauthorizedError(detail="Session is no longer active")

    request.state.user_id = uid
    request.state.username = username
    request.state.sid = sid
    request.state.default_workspace_id = default_workspace_id

    return {
        "id": uid,
        "username": username,
        "sid": sid,
        "default_workspace_id": default_workspace_id,
    }


async def get_current_user_optional(
    request: Request,
    auth_session_repo: AuthSessionRepository = Depends(get_auth_session_repo),
) -> dict | None:
    """Soft authentication dependency that returns None instead of raising 401."""
    try:
        return await get_current_user(request, auth_session_repo)
    except UnauthorizedError:
        return None


async def get_audit_logger(session: AsyncSession = Depends(get_db_session)) -> AuditLogger:
    return AuditLogger(session)


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


# ---------------------------------------------------------------------------
# Investing
# ---------------------------------------------------------------------------


async def get_investing_holding_repo(
    session: AsyncSession = Depends(get_db_session),
) -> HoldingRepository:
    return HoldingRepository(session)


async def get_investing_cash_balance_repo(
    session: AsyncSession = Depends(get_db_session),
) -> CashBalanceRepository:
    return CashBalanceRepository(session)


async def get_investing_instrument_repo(
    session: AsyncSession = Depends(get_db_session),
) -> InstrumentRepository:
    return InstrumentRepository(session)


async def get_investing_company_repo(
    session: AsyncSession = Depends(get_db_session),
) -> CompanyRepository:
    return CompanyRepository(session)


async def get_investing_constituent_repo(
    session: AsyncSession = Depends(get_db_session),
) -> InstrumentConstituentRepository:
    return InstrumentConstituentRepository(session)


async def get_finance_setting_repo(
    session: AsyncSession = Depends(get_db_session),
) -> FinanceSettingRepository:
    return FinanceSettingRepository(session)


async def get_finance_fx_rate_repo(
    session: AsyncSession = Depends(get_db_session),
) -> FxRateRepository:
    return FxRateRepository(session)


async def get_finance_transfer_repo(
    session: AsyncSession = Depends(get_db_session),
) -> CapitalTransferRepository:
    return CapitalTransferRepository(session)


async def get_finance_currency_repo(
    session: AsyncSession = Depends(get_db_session),
) -> CurrencyRepository:
    return CurrencyRepository(session)


async def get_finance_account_repo(
    session: AsyncSession = Depends(get_db_session),
) -> AccountRepository:
    return AccountRepository(session)


async def get_investing_holding_service(
    repo: HoldingRepository = Depends(get_investing_holding_repo),
    instrument_repo: InstrumentRepository = Depends(get_investing_instrument_repo),
    company_repo: CompanyRepository = Depends(get_investing_company_repo),
    account_repo: AccountRepository = Depends(get_finance_account_repo),
    currency_repo: CurrencyRepository = Depends(get_finance_currency_repo),
) -> HoldingService:
    return HoldingService(repo, instrument_repo, company_repo, account_repo, currency_repo)


async def get_investing_cash_balance_service(
    repo: CashBalanceRepository = Depends(get_investing_cash_balance_repo),
    account_repo: AccountRepository = Depends(get_finance_account_repo),
    currency_repo: CurrencyRepository = Depends(get_finance_currency_repo),
) -> CashBalanceService:
    return CashBalanceService(repo, account_repo, currency_repo)


async def get_investing_summary_service(
    holding_repo: HoldingRepository = Depends(get_investing_holding_repo),
    cash_repo: CashBalanceRepository = Depends(get_investing_cash_balance_repo),
    finance_setting_repo: FinanceSettingRepository = Depends(get_finance_setting_repo),
    fx_rate_repo: FxRateRepository = Depends(get_finance_fx_rate_repo),
) -> InvestingSummaryService:
    return InvestingSummaryService(holding_repo, cash_repo, finance_setting_repo, fx_rate_repo)


async def get_investing_instrument_service(
    instrument_repo: InstrumentRepository = Depends(get_investing_instrument_repo),
    company_repo: CompanyRepository = Depends(get_investing_company_repo),
) -> InstrumentService:
    return InstrumentService(instrument_repo, company_repo)


async def get_investing_constituent_service(
    instrument_repo: InstrumentRepository = Depends(get_investing_instrument_repo),
    company_repo: CompanyRepository = Depends(get_investing_company_repo),
    constituent_repo: InstrumentConstituentRepository = Depends(get_investing_constituent_repo),
) -> ConstituentService:
    return ConstituentService(instrument_repo, company_repo, constituent_repo)


async def get_investing_analytics_service(
    holding_repo: HoldingRepository = Depends(get_investing_holding_repo),
    instrument_repo: InstrumentRepository = Depends(get_investing_instrument_repo),
    company_repo: CompanyRepository = Depends(get_investing_company_repo),
    constituent_repo: InstrumentConstituentRepository = Depends(get_investing_constituent_repo),
) -> ExposureAnalyticsService:
    return ExposureAnalyticsService(holding_repo, instrument_repo, company_repo, constituent_repo)


# ---------------------------------------------------------------------------
# Finance references
# ---------------------------------------------------------------------------


async def get_finance_currency_service(
    repo: CurrencyRepository = Depends(get_finance_currency_repo),
) -> CurrencyService:
    return CurrencyService(repo)


async def get_finance_account_service(
    account_repo: AccountRepository = Depends(get_finance_account_repo),
    currency_repo: CurrencyRepository = Depends(get_finance_currency_repo),
) -> AccountService:
    return AccountService(account_repo, currency_repo)


async def get_finance_setting_service(
    setting_repo: FinanceSettingRepository = Depends(get_finance_setting_repo),
    currency_repo: CurrencyRepository = Depends(get_finance_currency_repo),
) -> FinanceSettingService:
    return FinanceSettingService(setting_repo, currency_repo)


async def get_finance_fx_rate_service(
    fx_repo: FxRateRepository = Depends(get_finance_fx_rate_repo),
    currency_repo: CurrencyRepository = Depends(get_finance_currency_repo),
) -> FxRateService:
    return FxRateService(fx_repo, currency_repo)


async def get_finance_transfer_service(
    transfer_repo: CapitalTransferRepository = Depends(get_finance_transfer_repo),
    account_repo: AccountRepository = Depends(get_finance_account_repo),
    currency_repo: CurrencyRepository = Depends(get_finance_currency_repo),
) -> CapitalTransferService:
    return CapitalTransferService(transfer_repo, account_repo, currency_repo)


async def get_current_workspace_id(
    current_user: dict = Depends(get_current_user),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
    membership_repo: MembershipRepository = Depends(get_membership_repo),
    category_service: CategoryService = Depends(get_spending_category_service),
) -> int:
    """Resolve the active workspace for the current request.

    **Fallback provisioning (defense-in-depth):**
    If the authenticated user has no workspace (e.g. a registration that partially
    failed before the workspace step, or a user created via admin tooling), this
    dependency will provision a default workspace and seed spending categories.

    Under normal operation, ``UserRegistrationWorkflow`` handles all of this
    atomically during registration, so this fallback path should never execute.
    It exists as a safety net, not as the primary provisioning mechanism.
    """
    if current_user.get("default_workspace_id") is not None:
        claimed_workspace_id = int(current_user["default_workspace_id"])
        membership = await membership_repo.get_membership(claimed_workspace_id, current_user["id"])
        if membership:
            return claimed_workspace_id

    workspaces = await workspace_service.get_user_workspaces(current_user["id"])
    if not workspaces:
        workspace = await workspace_service.ensure_default_workspace(
            current_user["id"], current_user["username"]
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
