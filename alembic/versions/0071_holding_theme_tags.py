"""add holding theme tags with optional target weights.

Holdings can be tagged with investment themes (e.g. "AI power/grid",
"battery", "robotics"). A tag may carry a target weight (percent of the whole
portfolio); a holding may carry several themes and a theme may span several
holdings, with the theme target being the sum of its tags' targets. Tags are
workspace-scoped, unique per (holding, theme), and cascade-delete with the
holding.

Tags drive get_allocation_vs_target (drift vs target, new-cash deployment that
never sells) and the theme breakdown of get_lookthrough_exposure.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0071_holding_theme_tags"
down_revision: str | None = "0070_holding_compliance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investing_holding_theme_tags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("holding_id", sa.Integer(), nullable=False),
        sa.Column("theme", sa.String(length=64), nullable=False),
        sa.Column("target_weight_pct", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["holding_id"],
            ["investing_holdings.id"],
            name="fk_investing_holding_theme_tags_holding",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_investing_holding_theme_tags_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "holding_id", "theme", name="uq_investing_holding_theme_tag_holding_theme"
        ),
        sa.CheckConstraint(
            "target_weight_pct IS NULL OR (target_weight_pct >= 0 AND target_weight_pct <= 100)",
            name="ck_investing_holding_theme_tag_target_weight_pct",
        ),
    )
    op.create_index(
        "ix_investing_holding_theme_tags_public_id",
        "investing_holding_theme_tags",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        "ix_investing_holding_theme_tags_workspace_id",
        "investing_holding_theme_tags",
        ["workspace_id"],
    )
    op.create_index(
        "ix_investing_holding_theme_tags_holding_id",
        "investing_holding_theme_tags",
        ["holding_id"],
    )
    op.create_index(
        "ix_investing_holding_theme_tags_workspace_theme",
        "investing_holding_theme_tags",
        ["workspace_id", "theme"],
    )


def downgrade() -> None:
    op.drop_table("investing_holding_theme_tags")
