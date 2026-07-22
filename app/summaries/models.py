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
    # spec-076: additive sections, same "None when unavailable/no data" shape
    # as health_summary above.
    dividend_summary: dict | None = Field(default=None, sa_type=sa.JSON())
    net_worth_summary: dict | None = Field(default=None, sa_type=sa.JSON())
    return_metrics_summary: dict | None = Field(default=None, sa_type=sa.JSON())
    highlights: dict = Field(sa_type=sa.JSON())
    # spec-080: when the user first opened this summary. NULL = unread; drives
    # dismissal of the "summary is ready" morning-briefing line.
    read_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    # spec-076: regeneration trail. A regenerated summary is never overwritten
    # in place — the superseded row is retained forever (no cap) and gets
    # superseded_by_id pointed at its replacement; the replacement carries
    # regenerated_at/regeneration_reason. NULL on both = never regenerated.
    superseded_by_id: int | None = Field(default=None, index=True)
    regenerated_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    regeneration_reason: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        # spec-076: at most one CURRENT (non-superseded) row per workspace/week
        # -- a plain unique constraint would reject a regenerated row for the
        # same week, since the superseded predecessor is retained (not
        # deleted). Partial index instead of UniqueConstraint so it can carry
        # the WHERE clause.
        sa.Index(
            "uq_weekly_summary_workspace_week_current",
            "workspace_id",
            "week_start",
            unique=True,
            postgresql_where=sa.text("superseded_by_id IS NULL"),
        ),
        sa.UniqueConstraint("id", "workspace_id", name="uq_weekly_summaries_id_workspace"),
        sa.ForeignKeyConstraint(
            ["superseded_by_id", "workspace_id"],
            ["weekly_summaries.id", "weekly_summaries.workspace_id"],
            name="fk_weekly_summaries_superseded_by",
        ),
    )


class WorkspaceSummarySetting(SQLModel, table=True):
    """Per-workspace weekly-summary cadence (spec-076). Singleton per
    workspace, same shape as WorkspaceFinanceSetting. Monthly cadence was
    explicitly deferred (spec-076 resolved question 3) — v1 is day/hour only."""

    __tablename__ = "workspace_summary_settings"

    id: int | None = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspaces.id", unique=True, index=True)
    # date.weekday() convention: 0 = Monday .. 6 = Sunday.
    cadence_day_of_week: int = Field(default=0)
    cadence_hour_utc: int = Field(default=1)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
