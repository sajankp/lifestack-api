"""stale_price_days on workspace_finance_settings (spec-059 §4).

Per-workspace threshold (default 7 days) after which a holding's stored price
is reported as ``stale`` instead of ``current`` in list_investment_holdings,
so a dashboard never treats an old NAV or close as fresh.
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0073_stale_price_days"
down_revision: str | None = "0072_portfolio_report_history"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "workspace_finance_settings",
        sa.Column(
            "stale_price_days",
            sa.Integer(),
            nullable=False,
            server_default="7",
        ),
    )


def downgrade() -> None:
    op.drop_column("workspace_finance_settings", "stale_price_days")
