"""Import table models here so Alembic sees the full metadata graph."""

from sqlmodel import SQLModel

from app.auth.models import AuthSession, User  # noqa: F401
from app.core.audit import AuditLog  # noqa: F401
from app.exports.models import ExportRecord  # noqa: F401
from app.finance.models import (  # noqa: F401
    Account,
    CapitalTransfer,
    Currency,
    FxRate,
    WorkspaceCurrency,
    WorkspaceFinanceSetting,
)
from app.investing.models import (  # noqa: F401
    CashBalance,
    Company,
    Holding,
    HoldingPrice,
    Instrument,
    InstrumentConstituent,
    PortfolioSnapshot,
)
from app.notifications.models import (  # noqa: F401
    Notification,
    NotificationDelivery,
    NotificationPreference,
)
from app.platform.models import Workspace, WorkspaceMembership  # noqa: F401
from app.spending.models import (  # noqa: F401
    RecurringTransaction,
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
)
from app.summaries.models import WeeklySummary  # noqa: F401
from app.todo.models import RecurringTodoRule, Todo  # noqa: F401

metadata = SQLModel.metadata
