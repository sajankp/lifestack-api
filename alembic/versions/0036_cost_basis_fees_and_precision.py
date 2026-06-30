"""capitalize fees into cost basis and widen Holding.avg_cost precision

Spec-046: two cost-basis accuracy fixes.

1. Buy-side brokerage/tax/other fees are capitalized into each FIFO lot's
   cost_per_unit (price + total_fees / quantity); sell-side fees reduce the
   sell order's realized_gain_loss. Cost basis, avg_cost, book_value, and
   realized G/L now match broker/tax reporting (Section 48, Income-tax Act).
2. ``investing_holdings.avg_cost`` widens from Numeric(12, 2) to
   Numeric(18, 6) so low-NAV/high-quantity holdings stop losing book-value
   precision to 2 dp rounding (matches OrderLot.cost_per_unit precision).

Backfill: re-derives every holding's lots, avg_cost, and each sell order's
realized_gain_loss/avg_cost_at_sale. Holdings with non-zero order fees and
low-NAV holdings will have these figures rewritten by design. This is a
standalone re-implementation of ``InvestingOrderService._replay_orders`` —
migrations must not import application code.

Revision ID: 0036_cost_basis_fees_and_precision
Revises: 0035_add_investing_order_lots
Create Date: 2026-06-30 00:00:00.000000
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

import sqlalchemy as sa

from alembic import op

revision = "0036_cost_basis_fees_and_precision"
down_revision = "0035_add_investing_order_lots"
branch_labels = None
depends_on = None

MONEY_QUANT = Decimal("0.01")
AVG_COST_PRECISION = Decimal("0.000001")


def _effective_buy_cost(price: Decimal, fees: Decimal, quantity: Decimal) -> Decimal:
    if fees == 0 or quantity == 0:
        return price
    return (price + fees / quantity).quantize(AVG_COST_PRECISION)


def upgrade() -> None:
    op.alter_column(
        "investing_holdings",
        "avg_cost",
        type_=sa.Numeric(precision=18, scale=6),
        existing_nullable=False,
    )
    _rederive_cost_basis_with_fees()


def downgrade() -> None:
    op.alter_column(
        "investing_holdings",
        "avg_cost",
        type_=sa.Numeric(precision=12, scale=2),
        existing_nullable=False,
    )


def _rederive_cost_basis_with_fees() -> None:
    """Clear FIFO lots/consumptions and replay every holding with fees folded in."""
    connection = op.get_bind()

    # 0035 populated lots with raw price as cost_per_unit; rebuild them.
    connection.execute(sa.text("DELETE FROM investing_lot_consumptions"))
    connection.execute(sa.text("DELETE FROM investing_order_lots"))

    holdings = connection.execute(
        sa.text("SELECT id, workspace_id, symbol, account_id FROM investing_holdings")
    ).fetchall()

    for holding_id, workspace_id, symbol, account_id in holdings:
        orders = connection.execute(
            sa.text(
                "SELECT id, order_type, quantity, price_per_unit, "
                "brokerage_fee, tax_amount, other_fees, occurred_at "
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

        for (
            order_id,
            order_type,
            quantity,
            price_per_unit,
            brokerage_fee,
            tax_amount,
            other_fees,
            occurred_at,
        ) in orders:
            quantity = Decimal(quantity)
            price_per_unit = Decimal(price_per_unit)
            fees = Decimal(brokerage_fee or 0) + Decimal(tax_amount or 0) + Decimal(other_fees or 0)

            if order_type == "buy":
                lot = {
                    "buy_order_id": order_id,
                    "original_quantity": quantity,
                    "remaining": quantity,
                    "cost_per_unit": _effective_buy_cost(price_per_unit, fees, quantity),
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
                    # Sell-side fees reduce realized proceeds.
                    realized_q = (realized - fees).quantize(MONEY_QUANT)
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
