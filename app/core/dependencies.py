from fastapi import Depends, Request
from slowapi import Limiter
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workflows import DashboardSummaryWorkflow, UserRegistrationWorkflow
from app.auth.dependencies import get_auth_service as get_auth_service
from app.auth.dependencies import get_auth_session_repo as get_auth_session_repo
from app.auth.dependencies import get_current_user as get_current_user
from app.auth.dependencies import get_current_user_optional as get_current_user_optional
from app.auth.dependencies import get_user_repo as get_user_repo
from app.auth.service import AuthService
from app.config import settings
from app.core.audit import AuditLogger
from app.core.database.postgres import get_db_session
from app.core.exceptions import ForbiddenError
from app.exports.repository import ExportRepository
from app.exports.service import ExportService
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
from app.imports.repository import ImportRepository
from app.imports.service import ImportService
from app.investing.repository import (
    CashBalanceRepository,
    CompanyRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InstrumentConstituentRepository,
    InstrumentRepository,
    InvestingOrderRepository,
    LotRepository,
    PortfolioSnapshotRepository,
)
from app.investing.service import (
    CashBalanceService,
    ConstituentService,
    ExposureAnalyticsService,
    HoldingService,
    InstrumentService,
    InvestingOrderService,
    InvestingSummaryService,
    PerformanceService,
)
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.platform.repository import MembershipRepository, WorkspaceRepository
from app.platform.service import WorkspaceService
from app.spending.repository import (
    BudgetRepository,
    CategoryRepository,
    RecurringTransactionRepository,
    TransactionRepository,
)
from app.spending.service import (
    BudgetService,
    CategoryService,
    RecurringTransactionService,
    TransactionService,
)
from app.summaries.repository import WeeklySummaryRepository
from app.summaries.service import WeeklySummaryService
from app.todo.repository import TodoRepository
from app.todo.service import TodoService


def get_client_ip(request: Request) -> str:
    """Resolve the real client IP address checking TRUSTED_PROXIES."""
    client_host = request.client.host if request.client else None
    if not client_host:
        return "unknown"

    if client_host in settings.TRUSTED_PROXIES:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            ips = [ip.strip() for ip in xff.split(",")]
            if ips:
                return ips[0]
    return client_host


def _rate_limit_key_func(request: Request) -> str:
    """Use authenticated user ID when available, fall back to IP."""
    if hasattr(request.state, "user_id") and request.state.user_id:
        return f"user:{request.state.user_id}"
    return get_client_ip(request)


limiter = Limiter(
    key_func=_rate_limit_key_func,
    storage_uri=settings.RATE_LIMIT_STORAGE_URI,
    enabled=settings.RATE_LIMIT_ENABLED,
)


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


async def get_recurring_repo(
    session: AsyncSession = Depends(get_db_session),
) -> RecurringTransactionRepository:
    return RecurringTransactionRepository(session)


async def get_spending_category_service(
    repo: CategoryRepository = Depends(get_category_repo),
) -> CategoryService:
    return CategoryService(repo)


async def get_spending_transaction_service(
    tx_repo: TransactionRepository = Depends(get_transaction_repo),
    cat_repo: CategoryRepository = Depends(get_category_repo),
    session: AsyncSession = Depends(get_db_session),
) -> TransactionService:
    account_repo = AccountRepository(session)
    return TransactionService(tx_repo, cat_repo, account_repo)


async def get_spending_budget_service(
    budget_repo: BudgetRepository = Depends(get_budget_repo),
    cat_repo: CategoryRepository = Depends(get_category_repo),
) -> BudgetService:
    return BudgetService(budget_repo, cat_repo)


async def get_spending_recurring_service(
    recurring_repo: RecurringTransactionRepository = Depends(get_recurring_repo),
    tx_repo: TransactionRepository = Depends(get_transaction_repo),
    cat_repo: CategoryRepository = Depends(get_category_repo),
) -> RecurringTransactionService:
    return RecurringTransactionService(recurring_repo, tx_repo, cat_repo)


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


async def get_investing_holding_price_repo(
    session: AsyncSession = Depends(get_db_session),
) -> HoldingPriceRepository:
    return HoldingPriceRepository(session)


async def get_investing_snapshot_repo(
    session: AsyncSession = Depends(get_db_session),
) -> PortfolioSnapshotRepository:
    return PortfolioSnapshotRepository(session)


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
    holding_price_repo: HoldingPriceRepository = Depends(get_investing_holding_price_repo),
) -> HoldingService:
    return HoldingService(
        repo,
        instrument_repo,
        company_repo,
        account_repo,
        currency_repo,
        holding_price_repo,
    )


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
    holding_price_repo: HoldingPriceRepository = Depends(get_investing_holding_price_repo),
    snapshot_repo: PortfolioSnapshotRepository = Depends(get_investing_snapshot_repo),
) -> InvestingSummaryService:
    return InvestingSummaryService(
        holding_repo,
        cash_repo,
        finance_setting_repo,
        fx_rate_repo,
        holding_price_repo,
        snapshot_repo,
    )


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
    finance_setting_repo: FinanceSettingRepository = Depends(get_finance_setting_repo),
    fx_rate_repo: FxRateRepository = Depends(get_finance_fx_rate_repo),
) -> ExposureAnalyticsService:
    return ExposureAnalyticsService(
        holding_repo,
        instrument_repo,
        company_repo,
        constituent_repo,
        finance_setting_repo,
        fx_rate_repo,
    )


async def get_investing_performance_service(
    holding_repo: HoldingRepository = Depends(get_investing_holding_repo),
    cash_repo: CashBalanceRepository = Depends(get_investing_cash_balance_repo),
    holding_price_repo: HoldingPriceRepository = Depends(get_investing_holding_price_repo),
    snapshot_repo: PortfolioSnapshotRepository = Depends(get_investing_snapshot_repo),
    finance_setting_repo: FinanceSettingRepository = Depends(get_finance_setting_repo),
    fx_rate_repo: FxRateRepository = Depends(get_finance_fx_rate_repo),
    instrument_repo: InstrumentRepository = Depends(get_investing_instrument_repo),
) -> PerformanceService:
    return PerformanceService(
        holding_repo,
        cash_repo,
        holding_price_repo,
        snapshot_repo,
        finance_setting_repo,
        fx_rate_repo,
        instrument_repo,
    )


async def get_investing_order_repo(
    session: AsyncSession = Depends(get_db_session),
) -> InvestingOrderRepository:
    return InvestingOrderRepository(session)


async def get_investing_lot_repo(
    session: AsyncSession = Depends(get_db_session),
) -> LotRepository:
    return LotRepository(session)


async def get_investing_order_service(
    order_repo: InvestingOrderRepository = Depends(get_investing_order_repo),
    holding_repo: HoldingRepository = Depends(get_investing_holding_repo),
    cash_balance_repo: CashBalanceRepository = Depends(get_investing_cash_balance_repo),
    account_repo: AccountRepository = Depends(get_finance_account_repo),
    currency_repo: CurrencyRepository = Depends(get_finance_currency_repo),
    instrument_service: InstrumentService = Depends(get_investing_instrument_service),
    lot_repo: LotRepository = Depends(get_investing_lot_repo),
) -> InvestingOrderService:
    return InvestingOrderService(
        order_repo,
        holding_repo,
        cash_balance_repo,
        account_repo,
        currency_repo,
        instrument_service,
        lot_repo,
    )


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


async def get_import_repo(
    session: AsyncSession = Depends(get_db_session),
) -> ImportRepository:
    return ImportRepository(session)


async def get_import_service(
    repo: ImportRepository = Depends(get_import_repo),
    session: AsyncSession = Depends(get_db_session),
    order_service: InvestingOrderService = Depends(get_investing_order_service),
) -> ImportService:
    return ImportService(repo, session, order_service=order_service)


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
    cash_balance_repo: CashBalanceRepository = Depends(get_investing_cash_balance_repo),
) -> CapitalTransferService:
    return CapitalTransferService(transfer_repo, account_repo, currency_repo, cash_balance_repo)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


async def get_export_repo(session: AsyncSession = Depends(get_db_session)) -> ExportRepository:
    return ExportRepository(session)


async def get_export_service(repo: ExportRepository = Depends(get_export_repo)) -> ExportService:
    return ExportService(repo)


async def get_notification_repo(
    session: AsyncSession = Depends(get_db_session),
) -> NotificationRepository:
    return NotificationRepository(session)


async def get_notification_service(
    repo: NotificationRepository = Depends(get_notification_repo),
) -> NotificationService:
    return NotificationService(repo)


async def get_weekly_summary_repo(
    session: AsyncSession = Depends(get_db_session),
) -> WeeklySummaryRepository:
    return WeeklySummaryRepository(session)


async def get_weekly_summary_service(
    repo: WeeklySummaryRepository = Depends(get_weekly_summary_repo),
    session: AsyncSession = Depends(get_db_session),
    notification_service: NotificationService = Depends(get_notification_service),
) -> WeeklySummaryService:
    return WeeklySummaryService(repo, session, notification_service)


async def get_current_workspace_id(
    request: Request,
    current_user: dict = Depends(get_current_user),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
    workspace_repo: WorkspaceRepository = Depends(get_workspace_repo),
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
    membership = None
    workspace_id = None

    if current_user.get("default_workspace_id") is not None:
        claimed_workspace_id = int(current_user["default_workspace_id"])
        membership = await membership_repo.get_membership(claimed_workspace_id, current_user["id"])
        if membership:
            workspace_id = claimed_workspace_id

    if workspace_id is None:
        workspaces = await workspace_service.get_user_workspaces(current_user["id"])
        if not workspaces:
            workspace = await workspace_service.ensure_default_workspace(
                current_user["id"], current_user["username"]
            )
            await category_service.provision_default_categories(workspace.id)
            workspace_id = workspace.id
        else:
            workspace_id = workspaces[0].id

        if membership is None or membership.workspace_id != workspace_id:
            membership = await membership_repo.get_membership(workspace_id, current_user["id"])

    # Enforce that the resolved workspace is active
    resolved_workspace = await workspace_repo.get_by_id(workspace_id)
    if not resolved_workspace or not resolved_workspace.is_active:
        raise ForbiddenError(detail="Workspace is inactive or does not exist")

    # Store resolved workspace_id, membership, and role on request state for downstream checks
    request.state.workspace_id = workspace_id
    if membership:
        request.state.membership = membership
        request.state.role = membership.role
    else:
        raise ForbiddenError(detail="Not a member of this workspace")

    return workspace_id


async def get_current_membership(
    request: Request,
    current_user: dict = Depends(get_current_user),
    workspace_id: int = Depends(get_current_workspace_id),
    membership_repo: MembershipRepository = Depends(get_membership_repo),
):
    if hasattr(request.state, "membership") and request.state.membership:
        return request.state.membership

    membership = await membership_repo.get_membership(workspace_id, current_user["id"])
    if not membership:
        raise ForbiddenError(detail="Not a member of this workspace")
    request.state.membership = membership
    request.state.role = membership.role
    return membership


ROLE_RANK = {
    "owner": 4,
    "admin": 3,
    "member": 2,
    "viewer": 1,
}


def _role_key(role) -> str:
    if hasattr(role, "value"):
        return str(role.value).lower()
    return str(role).split(".")[-1].lower()


def require_min_role(min_role: str):
    async def dependency(
        membership=Depends(get_current_membership),
    ):
        user_role = _role_key(membership.role)
        target_role = _role_key(min_role)

        if ROLE_RANK.get(user_role, 0) < ROLE_RANK.get(target_role, 0):
            raise ForbiddenError(detail="Insufficient workspace permissions")
        return membership

    return dependency


# ---------------------------------------------------------------------------
# Registration workflow  (needs spending category service)
# ---------------------------------------------------------------------------


async def get_user_registration_workflow(
    auth_service: AuthService = Depends(get_auth_service),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
    category_service: CategoryService = Depends(get_spending_category_service),
) -> UserRegistrationWorkflow:
    return UserRegistrationWorkflow(auth_service, workspace_service, category_service)


async def get_dashboard_summary_workflow(
    todo_service: TodoService = Depends(get_todo_service),
    transaction_service: TransactionService = Depends(get_spending_transaction_service),
    budget_service: BudgetService = Depends(get_spending_budget_service),
    investing_performance_service: PerformanceService = Depends(get_investing_performance_service),
) -> DashboardSummaryWorkflow:
    return DashboardSummaryWorkflow(
        todo_service=todo_service,
        transaction_service=transaction_service,
        budget_service=budget_service,
        investing_performance_service=investing_performance_service,
    )
