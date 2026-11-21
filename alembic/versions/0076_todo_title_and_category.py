"""Widen todo titles to 200 chars and add an optional todo category.

Widens ``todos.title`` and ``recurring_todo_rules.title`` from VARCHAR(100) to
VARCHAR(200). The description columns are already TEXT (0074_todo_description_text),
so only the Pydantic validation limit changed there (2000 -> 20000) and no column
change is needed.

Adds a nullable ``category`` VARCHAR(100) to ``todos`` — a free-text label used to
group todos (e.g. "Finance"). Null means uncategorised. All todo queries are already
workspace-scoped via ``todos.workspace_id``, so no separate categories table is
needed; "create a category" is simply assigning the label on a todo.

The downgrade drops the category column and narrows titles back to VARCHAR(100);
it fails loudly (no truncation) if any title exceeds 100 characters.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0076_todo_title_and_category"
down_revision: str | None = "0075_health_measurements"
branch_labels: str | None = None
depends_on: str | None = None

_TITLE_TABLES = ("todos", "recurring_todo_rules")


def upgrade() -> None:
    for table in _TITLE_TABLES:
        op.alter_column(
            table,
            "title",
            existing_type=sa.String(length=100),
            type_=sa.String(length=200),
            existing_nullable=False,
        )
    op.add_column(
        "todos",
        sa.Column("category", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("todos", "category")
    for table in _TITLE_TABLES:
        op.alter_column(
            table,
            "title",
            existing_type=sa.String(length=200),
            type_=sa.String(length=100),
            existing_nullable=False,
        )
