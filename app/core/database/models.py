"""Import table models here so Alembic sees the full metadata graph."""

from sqlmodel import SQLModel

from app.auth.models import User  # noqa: F401
from app.platform.models import Workspace, WorkspaceMembership  # noqa: F401
from app.spending.models import SpendingBudget, SpendingCategory, SpendingTransaction  # noqa: F401
from app.todo.models import Todo  # noqa: F401

metadata = SQLModel.metadata
