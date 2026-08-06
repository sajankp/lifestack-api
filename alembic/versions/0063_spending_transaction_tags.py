"""Add workspace-scoped spending tags and transaction links."""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "0063_spending_transaction_tags"
down_revision: str | None = "0062_user_auth_identities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "spending_tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("normalized_name", sa.String(length=80), nullable=False),
        sa.Column("color", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint(
            "workspace_id", "normalized_name", name="uq_spending_tag_workspace_name"
        ),
    )
    op.create_index("ix_spending_tags_public_id", "spending_tags", ["public_id"], unique=True)
    op.create_index("ix_spending_tags_workspace_id", "spending_tags", ["workspace_id"])

    op.create_table(
        "spending_transaction_tags",
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["transaction_id", "workspace_id"],
            ["spending_transactions.id", "spending_transactions.workspace_id"],
            name="fk_spending_transaction_tags_transaction_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tag_id", "workspace_id"],
            ["spending_tags.id", "spending_tags.workspace_id"],
            name="fk_spending_transaction_tags_tag_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("transaction_id", "tag_id"),
    )
    op.create_index(
        "ix_spending_transaction_tags_workspace_id",
        "spending_transaction_tags",
        ["workspace_id"],
    )

    # Existing imports/manual entries already use the labels column. Copy
    # those values into real tags so the new UI does not strand prior data.
    # Do this through the migration connection rather than a database-specific
    # UUID function so fresh installs do not depend on an extension.
    connection = op.get_bind()
    tag_ids: dict[tuple[int, str], int] = {}
    rows = connection.execute(
        sa.text(
            "SELECT id, workspace_id, labels FROM spending_transactions WHERE labels IS NOT NULL"
        )
    )
    for _transaction_id, workspace_id, labels in rows:
        for raw_name in str(labels).split(","):
            name = " ".join(raw_name.strip().split())
            normalized = name.lower()
            if not normalized or (workspace_id, normalized) in tag_ids:
                continue
            tag_row = connection.execute(
                sa.text(
                    """
                    INSERT INTO spending_tags
                        (public_id, workspace_id, name, normalized_name, created_at, updated_at)
                    VALUES (:public_id, :workspace_id, :name, :normalized_name, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    RETURNING id
                    """
                ),
                {
                    "public_id": uuid4(),
                    "workspace_id": workspace_id,
                    "name": name,
                    "normalized_name": normalized,
                },
            ).scalar_one()
            tag_ids[(workspace_id, normalized)] = int(tag_row)

    rows = connection.execute(
        sa.text(
            "SELECT id, workspace_id, labels FROM spending_transactions WHERE labels IS NOT NULL"
        )
    )
    for transaction_id, workspace_id, labels in rows:
        for raw_name in str(labels).split(","):
            normalized = " ".join(raw_name.strip().split()).lower()
            tag_id = tag_ids.get((workspace_id, normalized))
            if tag_id is not None:
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO spending_transaction_tags (transaction_id, tag_id, workspace_id)
                        VALUES (:transaction_id, :tag_id, :workspace_id)
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    {
                        "transaction_id": transaction_id,
                        "tag_id": tag_id,
                        "workspace_id": workspace_id,
                    },
                )


def downgrade() -> None:
    op.drop_index(
        "ix_spending_transaction_tags_workspace_id", table_name="spending_transaction_tags"
    )
    op.drop_table("spending_transaction_tags")
    op.drop_index("ix_spending_tags_workspace_id", table_name="spending_tags")
    op.drop_index("ix_spending_tags_public_id", table_name="spending_tags")
    op.drop_table("spending_tags")
