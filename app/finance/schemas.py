import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.finance.models import AccountType, CurrencyDisplayPreference, TransferModule


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


class WorkspaceFinanceSettingUpdate(BaseModel):
    reporting_currency_code: str | None = Field(default=None, min_length=1, max_length=10)
    currency_display_preference: CurrencyDisplayPreference | None = None

    @field_validator("reporting_currency_code")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper()


class WorkspaceFinanceSettingResponse(BaseModel):
    reporting_currency_code: str | None
    currency_display_preference: CurrencyDisplayPreference
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UserFinanceSettingUpdate(BaseModel):
    reporting_currency_override_code: str | None = Field(default=None, min_length=1, max_length=10)
    currency_display_preference_override: CurrencyDisplayPreference | None = None

    @field_validator("reporting_currency_override_code")
    @classmethod
    def normalize_override_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper()


class UserFinanceSettingResponse(BaseModel):
    reporting_currency_override_code: str | None
    currency_display_preference_override: CurrencyDisplayPreference | None
    workspace_reporting_currency_code: str | None
    workspace_currency_display_preference: CurrencyDisplayPreference
    effective_reporting_currency_code: str | None
    effective_currency_display_preference: CurrencyDisplayPreference
    updated_at: datetime


class FxRateUpsert(BaseModel):
    base_currency_code: str = Field(..., min_length=1, max_length=10)
    quote_currency_code: str = Field(..., min_length=1, max_length=10)
    rate: Decimal = Field(..., gt=0, decimal_places=10)
    as_of: datetime
    fetched_at: datetime
    source: str = Field(..., min_length=1, max_length=64)

    @field_validator("base_currency_code", "quote_currency_code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()


class FxRateResponse(BaseModel):
    base_currency_code: str
    quote_currency_code: str
    rate: Decimal
    as_of: datetime
    fetched_at: datetime
    source: str

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class CapitalTransferCreate(BaseModel):
    from_module: TransferModule = TransferModule.spending
    to_module: TransferModule = TransferModule.investing
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    from_currency_code: str = Field(..., min_length=1, max_length=10)
    to_currency_code: str = Field(..., min_length=1, max_length=10)
    gross_amount: Decimal = Field(..., ge=0, decimal_places=2)
    fx_rate_used: Decimal | None = Field(default=None, gt=0, decimal_places=10)
    fx_fee_amount: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    platform_fee_amount: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    tax_amount: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    net_amount_received: Decimal = Field(..., ge=0, decimal_places=2)
    occurred_at: datetime
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("from_currency_code", "to_currency_code")
    @classmethod
    def normalize_transfer_currency(cls, value: str) -> str:
        return value.strip().upper()


class CapitalTransferResponse(BaseModel):
    public_id: uuid.UUID
    from_module: TransferModule
    to_module: TransferModule
    from_account_id: int
    to_account_id: int
    from_account_public_id: uuid.UUID | None = None
    to_account_public_id: uuid.UUID | None = None
    from_account_name: str | None = None
    to_account_name: str | None = None
    from_account_type: AccountType | None = None
    to_account_type: AccountType | None = None
    from_currency_code: str
    to_currency_code: str
    gross_amount: Decimal
    fx_rate_used: Decimal | None
    fx_fee_amount: Decimal
    platform_fee_amount: Decimal
    tax_amount: Decimal
    net_amount_received: Decimal
    occurred_at: datetime
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})
