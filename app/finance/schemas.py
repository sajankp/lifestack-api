import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.finance.models import AccountType


class CurrencyResponse(BaseModel):
    code: str
    name: str
    symbol: str | None
    minor_unit: int
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class AccountCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    account_type: AccountType = AccountType.brokerage
    default_currency_code: str = Field(..., min_length=1, max_length=10)

    @field_validator("default_currency_code")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    account_type: AccountType | None = None
    default_currency_code: str | None = Field(default=None, min_length=1, max_length=10)
    is_active: bool | None = None

    @field_validator("default_currency_code")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper()


class AccountResponse(BaseModel):
    public_id: uuid.UUID
    name: str
    account_type: AccountType
    default_currency_code: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
