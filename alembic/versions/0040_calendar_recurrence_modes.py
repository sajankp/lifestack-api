"""add calendar recurrence modes to recurring_todo_rules and recurring_transactions

Spec-053: extends the shared monthly-recurrence vocabulary with "last day of
month" and "Nth weekday of month" modes, on top of the existing
day-of-month default. Both tables share one Postgres enum type
(recurrence_monthly_mode) — explicit pre-create is required here (unlike
op.create_table, which auto-creates named enum types, op.add_column does
not), and only the first use creates it; the second passes
create_type=False.

Existing rows default to monthly_mode="day_of_month" with by_weekday/
by_ordinal null — no data migration. This migration does not touch any
stored next_due_date; the day-of-month drift fix (advance_due_date using
anchor_day instead of current.day) only changes *future* advances.

Revision ID: 0040_calendar_recurrence_modes
Revises: 0039_push_subscriptions
Create Date: 2026-07-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0040_calendar_recurrence_modes"
down_revision = "0039_push_subscriptions"
branch_labels = None
depends_on = None

_ENUM_NAME = "recurrence_monthly_mode"
_ENUM_VALUES = ("day_of_month", "last_day", "nth_weekday")


def _add_recurrence_columns(table: str, *, create_enum_type: bool) -> None:
    op.add_column(
        table,
        sa.Column(
            "monthly_mode",
            sa.Enum(*_ENUM_VALUES, name=_ENUM_NAME, create_type=create_enum_type),
            nullable=False,
            server_default="day_of_month",
        ),
    )
    op.add_column(table, sa.Column("by_weekday", sa.SmallInteger(), nullable=True))
    op.add_column(table, sa.Column("by_ordinal", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        f"ck_{table}_nth_weekday_fields",
        table,
        "(monthly_mode = 'nth_weekday') = (by_weekday IS NOT NULL AND by_ordinal IS NOT NULL)",
    )
    op.create_check_constraint(
        f"ck_{table}_by_weekday_range",
        table,
        "by_weekday IS NULL OR by_weekday BETWEEN 0 AND 6",
    )
    op.create_check_constraint(
        f"ck_{table}_by_ordinal_range",
        table,
        "by_ordinal IS NULL OR by_ordinal IN (-1, 1, 2, 3, 4)",
    )


def _drop_recurrence_columns(table: str) -> None:
    op.drop_constraint(f"ck_{table}_by_ordinal_range", table, type_="check")
    op.drop_constraint(f"ck_{table}_by_weekday_range", table, type_="check")
    op.drop_constraint(f"ck_{table}_nth_weekday_fields", table, type_="check")
    op.drop_column(table, "by_ordinal")
    op.drop_column(table, "by_weekday")
    op.drop_column(table, "monthly_mode")


def upgrade() -> None:
    sa.Enum(*_ENUM_VALUES, name=_ENUM_NAME).create(op.get_bind(), checkfirst=True)
    _add_recurrence_columns("recurring_todo_rules", create_enum_type=False)
    _add_recurrence_columns("recurring_transactions", create_enum_type=False)


def downgrade() -> None:
    _drop_recurrence_columns("recurring_transactions")
    _drop_recurrence_columns("recurring_todo_rules")
    sa.Enum(name=_ENUM_NAME).drop(op.get_bind(), checkfirst=True)
