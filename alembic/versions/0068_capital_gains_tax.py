"""add market_scope to accounts, exchange_country_override to instruments,
and the workspace_tax_profiles table (spec-099).

The capital-gains tax feature needs to know which regime a holding falls
under. Two things make that possible:

* ``accounts.market_scope`` — a broker-level default for accounts whose
  holdings are not covered by the reference security master.
* ``investing_instruments.exchange_country_override`` — a per-holding
  override that beats both the account default and the master.
* ``workspace_tax_profiles`` — the user's tax configuration.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0068_capital_gains_tax"
down_revision: str | None = "0067_dividend_credit_account"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "market_scope",
            sa.String(length=20),
            nullable=True,
        ),
    )
    op.add_column(
        "investing_instruments",
        sa.Column(
            "exchange_country_override",
            sa.String(length=2),
            nullable=True,
        ),
    )
    op.create_table(
        "workspace_tax_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("resident_status", sa.String(length=40), nullable=False),
        sa.Column("tax_regime", sa.String(length=10), nullable=False),
        sa.Column("other_income", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("slab_override_pct", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("surcharge_override_pct", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("cess_pct", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("explicit_regime", sa.String(length=20), nullable=True),
        sa.Column("apply_carry_forward", sa.Boolean(), nullable=False),
        sa.Column("fx_overrides", sa.JSON(), nullable=False),
        sa.Column("notes", sa.String(length=1000), nullable=True),
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
            ["workspace_id"], ["workspaces.id"], name="fk_workspace_tax_profiles_workspace"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workspace_tax_profiles_public_id",
        "workspace_tax_profiles",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        "ix_workspace_tax_profiles_workspace_id",
        "workspace_tax_profiles",
        ["workspace_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_tax_profiles_workspace_id", table_name="workspace_tax_profiles")
    op.drop_index("ix_workspace_tax_profiles_public_id", table_name="workspace_tax_profiles")
    op.drop_table("workspace_tax_profiles")
    op.drop_column("investing_instruments", "exchange_country_override")
    op.drop_column("accounts", "market_scope")
