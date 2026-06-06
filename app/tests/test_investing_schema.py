import uuid
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from app.investing.models import PortfolioSnapshot
from app.investing.schemas import HoldingPriceBulkCreate, HoldingPriceItem


def test_portfolio_snapshot_has_latest_query_index():
    indexes = {index.name: index for index in PortfolioSnapshot.__table__.indexes}

    index = indexes["ix_portfolio_snapshots_workspace_snapshot_date_desc"]

    assert [str(expression) for expression in index.expressions] == [
        "portfolio_snapshots.workspace_id",
        "snapshot_date DESC",
    ]


def test_holding_price_bulk_create_validation():
    item = HoldingPriceItem(holding_public_id=uuid.uuid4(), unit_price=150.00)

    # CASE 1: Valid date (today)
    valid_data = HoldingPriceBulkCreate(price_date=date.today(), prices=[item])
    assert valid_data.price_date == date.today()

    # CASE 2: Future date
    future_date = date.today() + timedelta(days=1)
    with pytest.raises(ValidationError) as exc_info:
        HoldingPriceBulkCreate(price_date=future_date, prices=[item])
    assert "Price date cannot be in the future" in str(exc_info.value)

    # CASE 3: Date before 1900
    old_date = date(1899, 12, 31)
    with pytest.raises(ValidationError) as exc_info:
        HoldingPriceBulkCreate(price_date=old_date, prices=[item])
    assert "Price date cannot be before year 1900" in str(exc_info.value)
