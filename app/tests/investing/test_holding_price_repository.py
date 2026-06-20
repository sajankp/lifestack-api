from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.investing.models import HoldingPrice
from app.investing.repository import HoldingPriceRepository


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workspace_id", "holding_id"),
    [(None, 1), (1, None), (None, None)],
)
async def test_delete_for_holding_rejects_missing_identifiers(
    workspace_id: int | None, holding_id: int | None
) -> None:
    session = AsyncMock()
    session.add = MagicMock()
    repository = HoldingPriceRepository(session)

    with pytest.raises(ValueError, match="workspace_id and holding_id must not be None"):
        await repository.delete_for_holding(workspace_id, holding_id)  # type: ignore[arg-type]

    session.execute.assert_not_awaited()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert_price_updates_existing_row() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    existing = HoldingPrice(
        id=1,
        workspace_id=2,
        holding_id=3,
        price_date=date(2026, 6, 19),
        unit_price=Decimal("90"),
        source="api",
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    session.execute.return_value = result
    repository = HoldingPriceRepository(session)

    returned = await repository.upsert_price(
        workspace_id=2,
        holding_id=3,
        price_date=date(2026, 6, 19),
        unit_price=Decimal("91.25"),
        source="manual",
    )

    assert returned is existing
    assert existing.unit_price == Decimal("91.25")
    assert existing.source == "manual"
    session.add.assert_called_once_with(existing)
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_price_creates_missing_row() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result
    repository = HoldingPriceRepository(session)

    returned = await repository.upsert_price(
        workspace_id=2,
        holding_id=3,
        price_date=date(2026, 6, 19),
        unit_price=Decimal("91.25"),
    )

    added = session.add.call_args.args[0]
    assert returned is added
    assert added.workspace_id == 2
    assert added.holding_id == 3
    assert added.unit_price == Decimal("91.25")
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_bulk_upsert_prices_updates_and_creates_rows() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    existing = HoldingPrice(
        id=1,
        workspace_id=2,
        holding_id=3,
        price_date=date(2026, 6, 19),
        unit_price=Decimal("90"),
        source="api",
    )
    scalars = MagicMock()
    scalars.all.return_value = [existing]
    result = MagicMock()
    result.scalars.return_value = scalars
    session.execute.return_value = result
    repository = HoldingPriceRepository(session)

    await repository.bulk_upsert_prices(
        workspace_id=2,
        price_date=date(2026, 6, 19),
        prices=[(3, Decimal("91")), (4, Decimal("42"))],
        source="manual",
    )

    assert existing.unit_price == Decimal("91")
    assert existing.source == "manual"
    created = session.add.call_args_list[-1].args[0]
    assert created.holding_id == 4
    assert created.unit_price == Decimal("42")
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_for_holding_executes_scoped_delete() -> None:
    session = AsyncMock()
    repository = HoldingPriceRepository(session)

    await repository.delete_for_holding(2, 3)

    session.execute.assert_awaited_once()
    session.flush.assert_awaited_once()
