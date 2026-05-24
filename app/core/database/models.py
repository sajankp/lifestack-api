"""Import table models here so Alembic sees the full metadata graph."""

from sqlmodel import SQLModel

from app.auth.models import AuthSession, User  # noqa: F401
from app.core.audit import AuditLog  # noqa: F401
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
    Instrument,
    InstrumentConstituent,
)
from app.platform.models import Workspace, WorkspaceMembership  # noqa: F401
from app.spending.models import SpendingBudget, SpendingCategory, SpendingTransaction  # noqa: F401
from app.todo.models import Todo  # noqa: F401

metadata = SQLModel.metadata
