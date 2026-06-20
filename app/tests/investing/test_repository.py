from unittest.mock import AsyncMock

import pytest

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
    repository = HoldingPriceRepository(session)

    with pytest.raises(ValueError, match="workspace_id and holding_id must not be None"):
        await repository.delete_for_holding(workspace_id, holding_id)  # type: ignore[arg-type]

    session.execute.assert_not_awaited()
    session.flush.assert_not_awaited()
