import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.investing.models import InstrumentType, OrderType
from app.spending.schemas import SourceMetadataResponse


class HoldingCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    account_id: uuid.UUID
    quantity: Decimal = Field(..., gt=0, decimal_places=8)
    avg_cost: Decimal = Field(..., ge=0, decimal_places=2)
    currency: str = Field(..., min_length=1, max_length=10)
    instrument_type: InstrumentType = InstrumentType.stock

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()


class HoldingUpdate(BaseModel):
    symbol: str | None = Field(default=None, min_length=1, max_length=20)
    quantity: Decimal | None = Field(default=None, gt=0, decimal_places=8)
    avg_cost: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    currency: str | None = Field(default=None, min_length=1, max_length=10)
    instrument_type: InstrumentType | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper()

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper()


class HoldingResponse(BaseModel):
    public_id: uuid.UUID
    symbol: str
    instrument_type: InstrumentType = InstrumentType.stock
    account_id: uuid.UUID
    account_name: str
    quantity: Decimal
    avg_cost: Decimal
    currency: str
    source_metadata: SourceMetadataResponse | None = None
    created_at: datetime
    updated_at: datetime

    current_price: Decimal | None = None
    current_value: Decimal | None = None
    gain_loss: Decimal | None = None
    gain_loss_pct: Decimal | None = None

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class CashBalanceCreate(BaseModel):
    account_id: uuid.UUID
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
    account_id: uuid.UUID
    account_name: str
    balance: Decimal
    currency: str
    as_of: datetime
    trigger_type: str | None = None
    trigger_ref: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class InvestingOrderCreate(BaseModel):
    account_id: uuid.UUID
    order_type: OrderType
    symbol: str = Field(..., min_length=1, max_length=20)
    quantity: Decimal = Field(..., gt=0, decimal_places=8)
    price_per_unit: Decimal = Field(..., gt=0, decimal_places=6)
    currency: str = Field(..., min_length=1, max_length=10)
    brokerage_fee: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    tax_amount: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    other_fees: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    exchange_name: str | None = Field(default=None, max_length=50)
    occurred_at: datetime
    notes: str | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()


class InvestingOrderResponse(BaseModel):
    public_id: uuid.UUID
    account_id: uuid.UUID
    account_name: str
    order_type: OrderType
    symbol: str
    instrument_type: str | None = None
    quantity: Decimal
    price_per_unit: Decimal
    gross_amount: Decimal
    brokerage_fee: Decimal
    tax_amount: Decimal
    other_fees: Decimal
    net_amount: Decimal
    currency: str
    exchange_name: str | None = None
    occurred_at: datetime
    notes: str | None = None
    realized_gain_loss: Decimal | None = None
    avg_cost_at_sale: Decimal | None = None
    source_type: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class InvestingOrderBulkCreate(BaseModel):
    account_id: uuid.UUID
    orders: list[InvestingOrderCreate]


class InvestingSummaryResponse(BaseModel):
    portfolio_value: Decimal | None = None
    holdings_count: int
    cash_total: Decimal | None = None
    currency_breakdown: dict[str, Decimal]
    daily_change: Decimal | None = None
    reporting_currency: str | None = None
    valuation_status: str = "unavailable"
    fx_as_of: datetime | None = None
    fx_rates_used: dict[str, Decimal] = Field(default_factory=dict)

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


class InstrumentUpdate(BaseModel):
    instrument_type: InstrumentType | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)


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
    renormalise: bool = False


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
    currency: str | None = None
    fx_as_of: datetime | None = None
    fx_rates_used: dict[str, Decimal] = Field(default_factory=dict)
    snapshot_coverage: Decimal
    staleness_days: int | None = None
    warnings: list[str]
    display_threshold_pct: Decimal = Decimal("0.5")
    hidden_exposure_count: int = 0
    exposure: list[ExposureCompanyRow]
    total_direct_exposure: Decimal | None
    total_lookthrough_exposure: Decimal | None

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
    currency: str | None = None
    fx_as_of: datetime | None = None
    fx_rates_used: dict[str, Decimal] = Field(default_factory=dict)
    snapshot_coverage: Decimal
    warnings: list[str]
    display_threshold_pct: Decimal = Decimal("0.5")
    hidden_overlap_count: int = 0
    top_5_concentration_pct: Decimal
    top_10_concentration_pct: Decimal
    duplicate_exposure_index: Decimal
    overlaps: list[OverlapRow]

    model_config = ConfigDict(json_encoders={Decimal: str})


class HoldingPriceItem(BaseModel):
    holding_public_id: uuid.UUID
    unit_price: Decimal = Field(..., gt=0, le=Decimal("1000000"))


class HoldingPriceBulkCreate(BaseModel):
    price_date: date
    prices: list[HoldingPriceItem] = Field(default_factory=list, min_length=1, max_length=500)

    @field_validator("price_date")
    @classmethod
    def validate_price_date(cls, value: date) -> date:
        today = datetime.now(UTC).date()
        if value > today:
            raise ValueError("Price date cannot be in the future")
        if value < date(1900, 1, 1):
            raise ValueError("Price date cannot be before year 1900")
        return value

    @field_validator("prices")
    @classmethod
    def validate_unique_holdings(cls, value: list[HoldingPriceItem]) -> list[HoldingPriceItem]:
        seen: set[uuid.UUID] = set()
        for item in value:
            if item.holding_public_id in seen:
                raise ValueError(f"Duplicate holding_public_id found: {item.holding_public_id}")
            seen.add(item.holding_public_id)
        return value


class PerformanceSummaryResponse(BaseModel):
    total_value: Decimal
    total_cost: Decimal
    portfolio_value: Decimal
    invested_value: Decimal
    cash_total: Decimal
    total_gain_loss: Decimal
    total_gain_loss_pct: Decimal | None
    daily_change: Decimal | None
    daily_change_pct: Decimal | None
    snapshot_date: date
    previous_snapshot_date: date | None
    currency: str
    valuation_status: str
    holdings_count: int
    fx_rates_used: dict[str, Decimal] = Field(default_factory=dict)

    model_config = ConfigDict(json_encoders={Decimal: str})
