"""health_metrics, health_records, health_measurements (spec-110).

Schema only. The copy of existing ``weight_entries`` rows into ``health_measurements`` is a
reviewed one-time script (``scripts/migrate_weight_to_health_measurements.py``), not part of
this migration. ``weight_entries`` is left untouched as the rollback path and is dropped in a
later, separate change.
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0075_health_measurements"
down_revision: str | None = "0074_todo_description_text"
branch_labels: str | None = None
depends_on: str | None = None


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "health_metrics",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False, server_default="other"),
        sa.Column("value_type", sa.String(length=16), nullable=False, server_default="numeric"),
        sa.Column("canonical_unit", sa.String(length=32), nullable=True),
        sa.Column("unit_conversions", sa.JSON(), nullable=True),
        sa.Column("aliases", sa.JSON(), nullable=True),
        sa.Column("min_value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("max_value", sa.Numeric(precision=18, scale=6), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "code", name="uq_health_metrics_workspace_code"),
        sa.CheckConstraint("value_type IN ('numeric', 'text')", name="ck_health_metrics_type"),
    )
    op.create_index("ix_health_metrics_public_id", "health_metrics", ["public_id"], unique=True)
    op.create_index("ix_health_metrics_workspace_id", "health_metrics", ["workspace_id"])

    op.create_table(
        "health_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="other"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("recorded_on", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(length=120), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="mcp"),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("source_import_id", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["source_import_id"], ["import_batches.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_health_records_public_id", "health_records", ["public_id"], unique=True)
    op.create_index("ix_health_records_workspace_id", "health_records", ["workspace_id"])
    op.create_index("ix_health_records_user_id", "health_records", ["user_id"])
    op.create_index("ix_health_records_source_type", "health_records", ["source_type"])
    op.create_index(
        "ix_health_records_workspace_recorded_on",
        "health_records",
        ["workspace_id", "recorded_on"],
    )
    op.create_index(
        "uq_health_records_workspace_source_ref",
        "health_records",
        ["workspace_id", "source_ref"],
        unique=True,
        postgresql_where=sa.text("source_ref IS NOT NULL"),
    )

    op.create_table(
        "health_measurements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("metric_id", sa.Integer(), nullable=False),
        sa.Column("record_id", sa.Integer(), nullable=True),
        sa.Column("measured_on", sa.Date(), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value_num", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("comparator", sa.String(length=2), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("ref_low", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("ref_high", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("ref_text", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("source_import_id", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["metric_id"], ["health_metrics.id"], name="fk_health_meas_metric", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["record_id"], ["health_records.id"], name="fk_health_meas_record", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["source_import_id"], ["import_batches.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "value_num IS NOT NULL OR value_text IS NOT NULL", name="ck_health_meas_has_value"
        ),
        sa.CheckConstraint(
            "comparator IS NULL OR value_num IS NOT NULL", name="ck_health_meas_comparator_numeric"
        ),
        sa.CheckConstraint(
            "comparator IS NULL OR comparator IN ('<', '<=', '>', '>=')",
            name="ck_health_meas_comparator",
        ),
    )
    op.create_index(
        "ix_health_measurements_public_id", "health_measurements", ["public_id"], unique=True
    )
    op.create_index("ix_health_measurements_workspace_id", "health_measurements", ["workspace_id"])
    op.create_index("ix_health_measurements_user_id", "health_measurements", ["user_id"])
    op.create_index("ix_health_measurements_record_id", "health_measurements", ["record_id"])
    op.create_index("ix_health_measurements_source_type", "health_measurements", ["source_type"])
    op.create_index(
        "ix_health_meas_workspace_metric_day",
        "health_measurements",
        ["workspace_id", "metric_id", "measured_on"],
    )
    op.create_index(
        "uq_health_meas_workspace_source_ref",
        "health_measurements",
        ["workspace_id", "source_ref"],
        unique=True,
        postgresql_where=sa.text("source_ref IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("health_measurements")
    op.drop_table("health_records")
    op.drop_table("health_metrics")
