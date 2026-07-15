from decimal import Decimal
from typing import Any

from app.investing.schemas import InstrumentResponse


def populate_valuation_fields(
    data: dict, quantity: Decimal, avg_cost: Decimal, unit_price: Decimal | None
) -> None:
    current_price = unit_price if unit_price is not None else avg_cost
    current_value = quantity * current_price
    book_value = quantity * avg_cost
    gain_loss = current_value - book_value
    gain_loss_pct = (gain_loss / book_value * Decimal("100")) if book_value else Decimal("0")

    data["current_price"] = current_price
    data["current_value"] = current_value
    data["book_value"] = book_value
    data["gain_loss"] = gain_loss
    data["gain_loss_pct"] = gain_loss_pct


async def instrument_response(
    instrument_service,
    workspace_id: int,
    instrument,
    company_cache: dict[int, Any] | None = None,
) -> InstrumentResponse:
    data = instrument.model_dump()
    if instrument.company_id is not None:
        company = (
            company_cache.get(instrument.company_id)
            if company_cache is not None
            else await instrument_service.company_repo.get_by_id(instrument.company_id)
        )
        if company is not None:
            data["ticker"] = company.ticker
        if company is not None and company.workspace_id == workspace_id:
            data["company_id"] = company.public_id
        else:
            data["company_id"] = None
    return InstrumentResponse.model_validate(data)
