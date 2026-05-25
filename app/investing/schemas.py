import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.investing.models import InstrumentType


class HoldingCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    account_name: str = Field(default="primary", min_length=1, max_length=100)
    quantity: Decimal = Field(..., gt=0, decimal_places=8)
    avg_cost: Decimal = Field(..., ge=0, decimal_places=2)
    currency: str = Field(..., min_length=1, max_length=10)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()


class HoldingUpdate(BaseModel):
    quantity: Decimal | None = Field(default=None, gt=0, decimal_places=8)
    avg_cost: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    currency: str | None = Field(default=None, min_length=1, max_length=10)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper()


class HoldingResponse(BaseModel):
    public_id: uuid.UUID
    symbol: str
    account_name: str
    quantity: Decimal
    avg_cost: Decimal
    currency: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class CashBalanceCreate(BaseModel):
    account_name: str = Field(..., min_length=1, max_length=100)
    balance: Decimal = Field(..., decimal_places=2)
    currency: str = Field(..., min_length=1, max_length=10)
    as_of: datetime

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()


class CashBalanceUpdate(BaseModel):
    balance: Decimal | None = Field(default=None, decimal_places=2)
    currency: str | None = Field(default=None, min_length=1, max_length=10)
    as_of: datetime | None = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper()


class CashBalanceResponse(BaseModel):
    public_id: uuid.UUID
    account_name: str
    balance: Decimal
    currency: str
    as_of: datetime
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class InvestingSummaryResponse(BaseModel):
    portfolio_value: Decimal | None = None
    holdings_count: int
    cash_total: Decimal | None = None
    currency_breakdown: dict[str, Decimal]
    daily_change: Decimal | None = None
    reporting_currency: str | None = None
    valuation_status: str = "unavailable"
    fx_as_of: datetime | None = None

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class InstrumentCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    name: str = Field(..., min_length=1, max_length=255)
    instrument_type: InstrumentType = InstrumentType.stock
    ticker: str | None = Field(default=None, min_length=1, max_length=20)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper()


class InstrumentResponse(BaseModel):
    public_id: uuid.UUID
    symbol: str
    name: str
    instrument_type: InstrumentType
    company_id: uuid.UUID | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class InstrumentConstituentCreate(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=255)
    company_ticker: str | None = Field(default=None, min_length=1, max_length=20)
    weight: Decimal = Field(..., gt=0, le=1, decimal_places=8)

    @field_validator("company_ticker")
    @classmethod
    def normalize_company_ticker(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper()


class InstrumentConstituentUpsert(BaseModel):
    as_of_date: date
    source: str = Field(..., min_length=1, max_length=64)
    fetched_at: datetime
    constituents: list[InstrumentConstituentCreate] = Field(default_factory=list, min_length=1)


class InstrumentConstituentResponse(BaseModel):
    company_id: uuid.UUID
    company_name: str
    company_ticker: str | None
    weight: Decimal
    as_of_date: date
    source: str

    model_config = ConfigDict(json_encoders={Decimal: str})


class ExposureCompanyRow(BaseModel):
    company_id: uuid.UUID
    company_name: str
    company_ticker: str | None
    direct_exposure: Decimal
    lookthrough_exposure: Decimal

    model_config = ConfigDict(json_encoders={Decimal: str})


class ExposureAnalyticsResponse(BaseModel):
    as_of_date: date
    analysis_status: str
    snapshot_coverage: Decimal
    staleness_days: int | None = None
    warnings: list[str]
    exposure: list[ExposureCompanyRow]
    total_direct_exposure: Decimal
    total_lookthrough_exposure: Decimal

    model_config = ConfigDict(json_encoders={Decimal: str})


class OverlapRow(BaseModel):
    company_id: uuid.UUID
    company_name: str
    company_ticker: str | None
    overlap_exposure: Decimal
    portfolio_share: Decimal

    model_config = ConfigDict(json_encoders={Decimal: str})


class OverlapAnalyticsResponse(BaseModel):
    as_of_date: date
    analysis_status: str
    snapshot_coverage: Decimal
    warnings: list[str]
    top_5_concentration_pct: Decimal
    top_10_concentration_pct: Decimal
    duplicate_exposure_index: Decimal
    overlaps: list[OverlapRow]

    model_config = ConfigDict(json_encoders={Decimal: str})


class HoldingPriceItem(BaseModel):
    holding_public_id: uuid.UUID
    unit_price: Decimal = Field(..., gt=0)


class HoldingPriceBulkCreate(BaseModel):
    price_date: date
    prices: list[HoldingPriceItem] = Field(default_factory=list, min_length=1)


class PerformanceSummaryResponse(BaseModel):
    total_value: Decimal
    total_cost: Decimal
    total_gain_loss: Decimal
    total_gain_loss_pct: Decimal | None
    snapshot_date: date
    currency: str

    model_config = ConfigDict(json_encoders={Decimal: str})
