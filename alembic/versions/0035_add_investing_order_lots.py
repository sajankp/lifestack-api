"""add investing_order_lots and investing_lot_consumptions (FIFO cost basis)

Spec-044: replaces the moving-average cost-basis model with FIFO lot
consumption (Section 45(2A), Income-tax Act 1961; CBDT Circular 768).

Backfill note: this recomputes ``avg_cost`` on every existing holding (and
``realized_gain_loss``/``avg_cost_at_sale`` on every existing sell order)
using FIFO instead of moving-average. These figures will change for any
holding that has ever had a partial sell — by design, to match broker/tax
reporting — but this means historical numbers that may already appear in
exported reports will be rewritten.

Revision ID: 0035_add_investing_order_lots
Revises: 0034_import_batch_commit_error
Create Date: 2026-06-30 00:00:00.000000
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

import sqlalchemy as sa

from alembic import op

revision = "0035_add_investing_order_lots"
down_revision = "0034_import_batch_commit_error"
branch_labels = None
depends_on = None

MONEY_QUANT = Decimal("0.01")
AVG_COST_PRECISION = Decimal("0.000001")


def upgrade() -> None:
    op.create_table(
        "investing_order_lots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("holding_id", sa.Integer(), nullable=False),
        sa.Column("buy_order_id", sa.Integer(), nullable=False),
        sa.Column("original_quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("remaining_quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("cost_per_unit", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_investing_order_lots_workspace"
        ),
        sa.ForeignKeyConstraint(
            ["holding_id"],
            ["investing_holdings.id"],
            name="fk_investing_order_lots_holding",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["buy_order_id"],
            ["investing_orders.id"],
            name="fk_investing_order_lots_buy_order",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_investing_order_lots_workspace_id", "investing_order_lots", ["workspace_id"]
    )
    op.create_index("ix_investing_order_lots_holding_id", "investing_order_lots", ["holding_id"])
    op.create_index(
        "ix_investing_order_lots_buy_order_id", "investing_order_lots", ["buy_order_id"]
    )
    op.create_index(
        "ix_investing_order_lots_holding_acquired_at",
        "investing_order_lots",
        ["holding_id", "acquired_at"],
    )

    op.create_table(
        "investing_lot_consumptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sell_order_id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=False),
        sa.Column("quantity_consumed", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("cost_per_unit", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["sell_order_id"],
            ["investing_orders.id"],
            name="fk_investing_lot_consumptions_sell_order",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"],
            ["investing_order_lots.id"],
            name="fk_investing_lot_consumptions_lot",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_investing_lot_consumptions_sell_order_id",
        "investing_lot_consumptions",
        ["sell_order_id"],
    )
    op.create_index(
        "ix_investing_lot_consumptions_lot_id", "investing_lot_consumptions", ["lot_id"]
    )

    _backfill_fifo_lots()


def _backfill_fifo_lots() -> None:
    """Replay every holding's order history with FIFO and populate lots.

    Standalone re-implementation of the FIFO replay used by
    ``InvestingOrderService._replay_orders`` — migrations must not import
    application code, since app code is free to change after this revision
    is frozen in history.
    """
    connection = op.get_bind()

    holdings = connection.execute(
        sa.text("SELECT id, workspace_id, symbol, account_id FROM investing_holdings")
    ).fetchall()

    for holding_id, workspace_id, symbol, account_id in holdings:
        orders = connection.execute(
            sa.text(
                "SELECT id, order_type, quantity, price_per_unit, occurred_at "
                "FROM investing_orders "
                "WHERE workspace_id = :workspace_id AND symbol = :symbol "
                "AND account_id = :account_id "
                "ORDER BY occurred_at ASC, id ASC"
            ),
            {"workspace_id": workspace_id, "symbol": symbol, "account_id": account_id},
        ).fetchall()

        if not orders:
            continue

        queue: deque[dict] = deque()
        all_lots: dict[int, dict] = {}
        consumptions: list[dict] = []

        for order_id, order_type, quantity, price_per_unit, occurred_at in orders:
            quantity = Decimal(quantity)
            price_per_unit = Decimal(price_per_unit)
            if order_type == "buy":
                lot = {
                    "buy_order_id": order_id,
                    "original_quantity": quantity,
                    "remaining": quantity,
                    "cost_per_unit": price_per_unit,
                    "acquired_at": occurred_at,
                }
                queue.append(lot)
                all_lots[order_id] = lot
            else:
                to_consume = quantity
                realized = Decimal("0")
                cost_consumed = Decimal("0")
                while to_consume > 0:
                    if not queue:
                        # Data predates order-level validation guarantees;
                        # skip rather than fail the whole migration.
                        break
                    lot = queue[0]
                    take = min(lot["remaining"], to_consume)
                    realized += take * (price_per_unit - lot["cost_per_unit"])
                    cost_consumed += take * lot["cost_per_unit"]
                    consumptions.append({
                        "sell_order_id": order_id,
                        "buy_order_id": lot["buy_order_id"],
                        "quantity_consumed": take,
                        "cost_per_unit": lot["cost_per_unit"],
                    })
                    lot["remaining"] -= take
                    to_consume -= take
                    if lot["remaining"] == 0:
                        queue.popleft()
                if cost_consumed > 0 or quantity > 0:
                    realized_q = realized.quantize(MONEY_QUANT)
                    avg_cost_at_sale_q = (
                        (cost_consumed / quantity).quantize(AVG_COST_PRECISION)
                        if quantity > 0
                        else Decimal("0")
                    )
                    connection.execute(
                        sa.text(
                            "UPDATE investing_orders "
                            "SET realized_gain_loss = :realized, avg_cost_at_sale = :avg_cost "
                            "WHERE id = :order_id"
                        ),
                        {
                            "realized": realized_q,
                            "avg_cost": avg_cost_at_sale_q,
                            "order_id": order_id,
                        },
                    )

        final_qty = sum((lot["remaining"] for lot in queue), Decimal("0"))
        final_avg_cost = (
            (
                sum((lot["remaining"] * lot["cost_per_unit"] for lot in queue), Decimal("0"))
                / final_qty
            ).quantize(AVG_COST_PRECISION)
            if final_qty > 0
            else Decimal("0")
        )

        connection.execute(
            sa.text(
                "UPDATE investing_holdings SET quantity = :qty, avg_cost = :avg_cost "
                "WHERE id = :holding_id"
            ),
            {"qty": final_qty, "avg_cost": final_avg_cost, "holding_id": holding_id},
        )

        for lot in all_lots.values():
            result = connection.execute(
                sa.text(
                    "INSERT INTO investing_order_lots "
                    "(workspace_id, holding_id, buy_order_id, original_quantity, "
                    "remaining_quantity, cost_per_unit, acquired_at, created_at) "
                    "VALUES (:workspace_id, :holding_id, :buy_order_id, :original_quantity, "
                    ":remaining_quantity, :cost_per_unit, :acquired_at, now()) "
                    "RETURNING id"
                ),
                {
                    "workspace_id": workspace_id,
                    "holding_id": holding_id,
                    "buy_order_id": lot["buy_order_id"],
                    "original_quantity": lot["original_quantity"],
                    "remaining_quantity": lot["remaining"],
                    "cost_per_unit": lot["cost_per_unit"],
                    "acquired_at": lot["acquired_at"],
                },
            )
            lot["lot_id"] = result.scalar_one()

        for ev in consumptions:
            connection.execute(
                sa.text(
                    "INSERT INTO investing_lot_consumptions "
                    "(sell_order_id, lot_id, quantity_consumed, cost_per_unit, created_at) "
                    "VALUES (:sell_order_id, :lot_id, :quantity_consumed, :cost_per_unit, now())"
                ),
                {
                    "sell_order_id": ev["sell_order_id"],
                    "lot_id": all_lots[ev["buy_order_id"]]["lot_id"],
                    "quantity_consumed": ev["quantity_consumed"],
                    "cost_per_unit": ev["cost_per_unit"],
                },
            )


def downgrade() -> None:
    op.drop_table("investing_lot_consumptions")
    op.drop_table("investing_order_lots")
