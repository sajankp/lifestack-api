import uuid
from datetime import UTC, date, datetime

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class WeeklySummary(SQLModel, table=True):
    __tablename__ = "weekly_summaries"
    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    week_start: date = Field(sa_type=sa.Date())
    week_end: date = Field(sa_type=sa.Date())
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    todo_summary: dict = Field(sa_type=sa.JSON())
    spending_summary: dict = Field(sa_type=sa.JSON())
    investing_summary: dict = Field(sa_type=sa.JSON())
    health_summary: dict | None = Field(default=None, sa_type=sa.JSON())
    highlights: dict = Field(sa_type=sa.JSON())
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "week_start", name="uq_weekly_summary_workspace_week"),
    )
