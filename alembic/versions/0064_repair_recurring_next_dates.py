"""recalculate stored recurring dates after calendar-mode fixes.

The initial calendar recurrence implementation could persist an anchor date
instead of the selected Nth weekday/last-day occurrence.  ``next_due_date`` is
derived state, so rebuild it from each rule's immutable anchor and schedule.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa

from alembic import op
from app.core.recurrence import first_due_date

revision = "0064_repair_recurring_next_dates"
down_revision = "0063_spending_transaction_tags"
branch_labels = None
depends_on = None


def _today(timezone: str | None) -> date:
    if not timezone:
        return datetime.now(UTC).date()
    try:
        return datetime.now(ZoneInfo(timezone)).date()
    except ZoneInfoNotFoundError:
        return datetime.now(UTC).date()


def _repair_table(connection, table: str, has_timezone: bool) -> None:
    timezone_sql = ", timezone" if has_timezone else ""
    rows = connection.execute(
        sa.text(
            f"""
            SELECT id, anchor_date, frequency, interval, monthly_mode,
                   by_weekday, by_ordinal, end_date{timezone_sql}
            FROM {table}
            """
        )
    ).mappings()
    for row in rows:
        next_due = first_due_date(
            row["anchor_date"],
            _today(row.get("timezone") if has_timezone else None),
            row["frequency"],
            row["interval"],
            monthly_mode=row["monthly_mode"],
            by_weekday=row["by_weekday"],
            by_ordinal=row["by_ordinal"],
        )
        connection.execute(
            sa.text(
                f"""
                UPDATE {table}
                SET next_due_date = :next_due_date,
                    is_active = CASE
                        WHEN end_date IS NOT NULL AND :next_due_date > end_date THEN FALSE
                        ELSE is_active
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
                """
            ),
            {"id": row["id"], "next_due_date": next_due},
        )


def upgrade() -> None:
    connection = op.get_bind()
    _repair_table(connection, "recurring_transactions", has_timezone=False)
    _repair_table(connection, "recurring_todo_rules", has_timezone=True)


def downgrade() -> None:
    # ``next_due_date`` is derived state and cannot be safely restored to the
    # pre-fix values.  Future edits and scheduler runs continue using the
    # corrected recurrence arithmetic.
    pass
