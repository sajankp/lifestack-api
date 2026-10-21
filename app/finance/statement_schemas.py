import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class AccountStatementResponse(BaseModel):
    public_id: uuid.UUID
    account_public_id: uuid.UUID
    period_start: date
    period_end: date
    closing_balance: Decimal | None
    currency_code: str
    reconciled_through: date | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})
