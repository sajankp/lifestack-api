import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.investing.models import ComplianceVerdict, CorporateActionType, InstrumentType, OrderType
from app.investing.sectors import normalise_sector
from app.spending.schemas import SourceMetadataResponse

DIVIDEND_INCOME_TYPES = ("dividend", "interest", "coupon")


class HoldingUpdate(BaseModel):
    symbol: str | None = Field(default=None, min_length=1, max_length=20)
    quantity: Decimal | None = Field(default=None, gt=0, decimal_places=8)
    avg_cost: Decimal | None = Field(default=None, ge=0, decimal_places=6)
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
    source_type: str = "manual"
    source_metadata: SourceMetadataResponse | None = None
    created_at: datetime
    updated_at: datetime

    current_price: Decimal | None = None
    current_value: Decimal | None = None
    book_value: Decimal | None = None
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
    instrument_type: InstrumentType = Field(default=InstrumentType.stock)
    instrument_name: str | None = Field(default=None, max_length=255)
    quantity: Decimal = Field(..., gt=0, decimal_places=8)
    price_per_unit: Decimal = Field(..., gt=0, decimal_places=6)
    currency: str = Field(..., min_length=1, max_length=10)
    brokerage_fee: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    tax_amount: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    other_fees: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    exchange_name: str | None = Field(default=None, max_length=50)
    occurred_at: datetime
    notes: str | None = None
    source_ref: str | None = Field(default=None, max_length=255)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def require_name_for_mutual_fund(self) -> "InvestingOrderCreate":
        if self.instrument_type == InstrumentType.mutual_fund and not self.instrument_name:
            raise ValueError("instrument_name is required when instrument_type is mutual_fund")
        return self


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


class InvestingOrderUpdate(BaseModel):
    order_type: OrderType | None = None
    quantity: Decimal | None = Field(default=None, gt=0, decimal_places=8)
    price_per_unit: Decimal | None = Field(default=None, gt=0, decimal_places=6)
    brokerage_fee: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    tax_amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    other_fees: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    exchange_name: str | None = Field(default=None, max_length=50)
    occurred_at: datetime | None = None
    notes: str | None = None


class InvestingOrderBulkCreate(BaseModel):
    account_id: uuid.UUID
    orders: list[InvestingOrderCreate]


class CorporateActionCreate(BaseModel):
    account_id: uuid.UUID
    symbol: str = Field(..., min_length=1, max_length=20)
    action_type: CorporateActionType
    # Semantics are action_type-dependent (spec-051): for a split, ratio_base
    # "old" units become ratio_quote "new" units; for a bonus, ratio_quote
    # units are granted free per ratio_base units held.
    ratio_base: Decimal = Field(..., gt=0, decimal_places=4)
    ratio_quote: Decimal = Field(..., gt=0, decimal_places=4)
    ex_date: date
    notes: str | None = Field(default=None, max_length=255)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class CorporateActionResponse(BaseModel):
    public_id: uuid.UUID
    account_id: uuid.UUID
    account_name: str
    symbol: str
    action_type: CorporateActionType
    ratio_base: Decimal
    ratio_quote: Decimal
    ex_date: date
    notes: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class HoldingVerificationResponse(BaseModel):
    public_id: uuid.UUID
    account_id: uuid.UUID
    account_name: str
    source: str
    statement_date: date | None = None
    match_count: int
    quantity_drift_count: int
    missing_in_lifestack_count: int
    missing_at_depository_count: int
    report: list[dict] = Field(default_factory=list)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


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


def _normalize_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().upper()
    return value or None


class InstrumentCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    name: str = Field(..., min_length=1, max_length=255)
    instrument_type: InstrumentType = InstrumentType.stock
    ticker: str | None = Field(default=None, min_length=1, max_length=20)
    isin: str | None = Field(default=None, min_length=1, max_length=20)
    exchange: str | None = Field(default=None, min_length=1, max_length=50)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("ticker", "isin", "exchange")
    @classmethod
    def normalize_ticker(cls, value: str | None) -> str | None:
        return _normalize_identifier(value)


class InstrumentUpdate(BaseModel):
    instrument_type: InstrumentType | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    ticker: str | None = Field(default=None, min_length=1, max_length=20)
    isin: str | None = Field(default=None, min_length=1, max_length=20)
    exchange: str | None = Field(default=None, min_length=1, max_length=50)
    exchange_country_override: str | None = Field(default=None, min_length=2, max_length=2)
    """Per-holding tax-regime override (spec-099): "IN" | "US" | null to clear.

    Beats both the brokerage account's market scope and the reference
    security master for this holding.
    """

    @field_validator("ticker", "isin", "exchange")
    @classmethod
    def normalize_identifiers(cls, value: str | None) -> str | None:
        return _normalize_identifier(value)

    @field_validator("exchange_country_override")
    @classmethod
    def validate_country_override(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"IN", "US"}:
            raise ValueError("exchange_country_override must be 'IN' or 'US'")
        return normalized


class InstrumentResponse(BaseModel):
    public_id: uuid.UUID
    symbol: str
    name: str
    instrument_type: InstrumentType
    company_id: uuid.UUID | None = None
    ticker: str | None = None
    isin: str | None = None
    exchange: str | None = None
    exchange_country_override: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class InstrumentConstituentCreate(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=255)
    company_ticker: str | None = Field(default=None, min_length=1, max_length=20)
    company_isin: str | None = Field(default=None, min_length=1, max_length=20)
    company_exchange: str | None = Field(default=None, min_length=1, max_length=50)
    sector: str | None = Field(default=None, min_length=1, max_length=100)
    industry: str | None = Field(default=None, min_length=1, max_length=100)
    weight: Decimal = Field(..., gt=0, le=1, decimal_places=8)

    @field_validator("company_ticker", "company_isin", "company_exchange")
    @classmethod
    def normalize_company_identifiers(cls, value: str | None) -> str | None:
        return _normalize_identifier(value)

    @field_validator("sector", "industry")
    @classmethod
    def normalize_sector_industry(cls, value: str | None) -> str | None:
        # Blank / whitespace-only means "not provided"; GICS names are
        # canonicalised so case variants never split an allocation bucket.
        return normalise_sector(value)

    @model_validator(mode="after")
    def require_an_identifier(self) -> "InstrumentConstituentCreate":
        """spec-083 §6 mandate: a name-only constituent row (no isin, no
        ticker) is exactly how "Apple Inc" / "Apple Inc." / "AAPL" fragmented
        into separate Company rows pre-spec-083 — reject it outright rather
        than accept it and let identity resolution guess. A row that *does*
        carry an identifier but doesn't resolve against reference data still
        passes here (that's `identifier_status=unresolved`, not a 422).
        """
        if not (self.company_isin or self.company_ticker):
            raise ValueError(
                f"company '{self.company_name}' needs a company_isin or company_ticker "
                "identifier — name alone is not enough (spec-083 identifier mandate)"
            )
        return self


class InstrumentConstituentUpsert(BaseModel):
    as_of_date: date
    source: str = Field(..., min_length=1, max_length=64)
    fetched_at: datetime
    constituents: list[InstrumentConstituentCreate] = Field(default_factory=list, min_length=1)
    renormalise: bool = False
    overwrite_existing: bool = False
    """When true, a row's sector/industry overwrites an existing value on the
    constituent company. When false (default), an already-set company sector or
    industry is left untouched by the write.
    """


class InstrumentConstituentResponse(BaseModel):
    company_id: uuid.UUID
    company_name: str
    company_ticker: str | None
    sector: str | None = None
    industry: str | None = None
    weight: Decimal
    as_of_date: date
    source: str

    model_config = ConfigDict(json_encoders={Decimal: str})


class ReferenceSecurityResponse(BaseModel):
    public_id: uuid.UUID
    isin: str | None
    ticker: str | None
    exchange: str | None
    amfi_code: str | None
    security_type: InstrumentType
    name: str
    country_code: str | None
    source: str

    model_config = ConfigDict(from_attributes=True)


class ReferenceResolveResponse(BaseModel):
    identifier_status: Literal["resolved", "unresolved", "ambiguous"]
    match: ReferenceSecurityResponse | None = None


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
    valuation_basis: str = "market_value"
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
    valuation_basis: str = "market_value"
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


class PerformanceHistoryPoint(BaseModel):
    snapshot_date: date
    holdings_value: Decimal
    total_cost: Decimal
    total_value: Decimal
    cash_value: Decimal
    unrealized_gain_loss: Decimal
    unrealized_gain_loss_pct: Decimal | None
    benchmark_value: Decimal | None = None
    benchmark_return_pct: Decimal | None = None

    model_config = ConfigDict(json_encoders={Decimal: str})


class PerformanceHistoryResponse(BaseModel):
    currency: str
    points: list[PerformanceHistoryPoint] = Field(default_factory=list)
    benchmark_symbol: str = "SPY"
    benchmark_return_pct: Decimal | None = None
    alpha_pct: Decimal | None = None

    model_config = ConfigDict(json_encoders={Decimal: str})


# ---------------------------------------------------------------------------

# Dividends / income events (spec-073)
# ---------------------------------------------------------------------------


class DividendCreate(BaseModel):
    account_id: uuid.UUID
    credit_account_id: uuid.UUID | None = None
    symbol: str | None = Field(default=None, min_length=1, max_length=20)
    income_type: str = Field(default="dividend")
    gross_amount: Decimal = Field(..., gt=0, decimal_places=2)
    tax_withheld: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    currency: str = Field(..., min_length=1, max_length=10)
    pay_date: date
    external_ref: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("income_type")
    @classmethod
    def validate_income_type(cls, value: str) -> str:
        if value not in DIVIDEND_INCOME_TYPES:
            raise ValueError(f"income_type must be one of {DIVIDEND_INCOME_TYPES}")
        return value

    @model_validator(mode="after")
    def validate_net_and_attribution(self) -> "DividendCreate":
        # Strictly less: net_amount must stay > 0 (DB CHECK) — full
        # withholding would otherwise surface as an IntegrityError 500.
        if self.tax_withheld >= self.gross_amount:
            raise ValueError("tax_withheld must be less than gross_amount")
        return self


class DividendUpdate(BaseModel):
    credit_account_id: uuid.UUID | None = None
    symbol: str | None = Field(default=None, min_length=1, max_length=20)
    income_type: str | None = None
    gross_amount: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    tax_withheld: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    currency: str | None = Field(default=None, min_length=1, max_length=10)
    pay_date: date | None = None
    external_ref: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None

    @field_validator("income_type")
    @classmethod
    def validate_income_type(cls, value: str | None) -> str | None:
        if value is not None and value not in DIVIDEND_INCOME_TYPES:
            raise ValueError(f"income_type must be one of {DIVIDEND_INCOME_TYPES}")
        return value


class DividendResponse(BaseModel):
    public_id: uuid.UUID
    account_id: uuid.UUID
    account_name: str
    credit_account_id: uuid.UUID | None = None
    credit_account_name: str | None = None
    holding_id: uuid.UUID | None = None
    symbol: str | None = None
    income_type: str
    gross_amount: Decimal
    tax_withheld: Decimal
    net_amount: Decimal
    currency: str
    pay_date: date
    external_ref: str | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class DividendBulkImportRow(BaseModel):
    account_id: uuid.UUID
    credit_account_id: uuid.UUID | None = None
    symbol: str | None = Field(default=None, max_length=20)
    income_type: str = Field(default="dividend")
    gross_amount: Decimal = Field(..., gt=0, decimal_places=2)
    tax_withheld: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=2)
    currency: str = Field(..., min_length=1, max_length=10)
    pay_date: date
    external_ref: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()


class DividendBulkImportRequest(BaseModel):
    rows: list[DividendBulkImportRow]


class DividendBulkImportRejectedRow(BaseModel):
    row: int
    reason: str


class DividendBulkImportResult(BaseModel):
    imported: int
    updated: int
    skipped: int
    rejected: list[DividendBulkImportRejectedRow]


# ---------------------------------------------------------------------------
# Return metrics: XIRR / annualized / realized-split (spec-071)
# ---------------------------------------------------------------------------


class PositionMetrics(BaseModel):
    xirr: Decimal | None = None
    annualized_return_pct: Decimal | None = None
    annualization_reliable: bool = False
    holding_days: int | None = None
    total_return_pct: Decimal | None = None
    realized: Decimal = Decimal("0")
    unrealized: Decimal = Decimal("0")
    market_value: Decimal = Decimal("0")
    invested: Decimal = Decimal("0")

    model_config = ConfigDict(json_encoders={Decimal: str})


class ScopeReturnMetrics(BaseModel):
    xirr: Decimal | None = None
    annualized_return_pct: Decimal | None = None
    annualization_reliable: bool = False
    holding_days: int | None = None
    # Simple (non-annualized) total return for the scope — the UI's INV-7
    # fallback for sub-year spans, where no annualized figure may be shown.
    total_return_pct: Decimal | None = None
    realized: Decimal = Decimal("0")
    unrealized: Decimal = Decimal("0")
    data_quality: str = "complete"
    open: PositionMetrics
    closed: PositionMetrics

    model_config = ConfigDict(json_encoders={Decimal: str})


class MaxDrawdown(BaseModel):
    pct: Decimal
    peak_date: date
    trough_date: date

    model_config = ConfigDict(json_encoders={Decimal: str})


class AccountReturnMetrics(ScopeReturnMetrics):
    account_id: uuid.UUID
    account_name: str
    currency: str


class CurrencyReturnMetrics(ScopeReturnMetrics):
    currency: str


class OverallReturnMetrics(ScopeReturnMetrics):
    max_drawdown: MaxDrawdown | None = None


class ReturnMetricsResponse(BaseModel):
    currency: str | None = None
    valuation_status: str
    overall: OverallReturnMetrics
    by_account: list[AccountReturnMetrics]
    by_currency: list[CurrencyReturnMetrics]

    model_config = ConfigDict(json_encoders={Decimal: str})


class AssetClassAllocationItem(BaseModel):
    key: str  # "stock", "etf", "mutual_fund", "cash"
    label: str
    value: Decimal
    pct: float
    count: int

    model_config = ConfigDict(json_encoders={Decimal: str})


class SectorAllocationItem(BaseModel):
    sector: str
    value: Decimal
    pct: float

    model_config = ConfigDict(json_encoders={Decimal: str})


class FundCoverageItem(BaseModel):
    instrument_public_id: uuid.UUID
    symbol: str
    name: str
    instrument_type: str
    value: Decimal
    coverage_pct: float
    snapshot_date: date | None
    source: str | None
    constituent_rows: int

    model_config = ConfigDict(json_encoders={Decimal: str})


class PortfolioAllocationResponse(BaseModel):
    as_of_date: date
    currency: str
    total_portfolio_value: Decimal
    holdings_value: Decimal
    cash_value: Decimal
    asset_classes: list[AssetClassAllocationItem]
    sectors: list[SectorAllocationItem]
    lookthrough: bool = False
    fund_coverage: list[FundCoverageItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    model_config = ConfigDict(json_encoders={Decimal: str})


class MonthlyDividendPoint(BaseModel):
    month: str  # "YYYY-MM"
    gross_amount: Decimal
    tax_withheld: Decimal
    net_amount: Decimal
    payment_count: int

    model_config = ConfigDict(json_encoders={Decimal: str})


# ---------------------------------------------------------------------------
# Halal compliance verdicts + purification estimates (spec-104)
# ---------------------------------------------------------------------------

MAX_COMPLIANCE_SOURCES = 10
MAX_SOURCE_URL_LENGTH = 2048
PURIFICATION_DISCLAIMER = (
    "Estimate only, not a fatwa or religious ruling. Screeners disagree and rules "
    "differ by school; consult a qualified scholar before acting on this figure."
)


def _not_in_future(value: date | None) -> date | None:
    """Reject dates after tomorrow (UTC); one day of slack covers users ahead of UTC."""
    if value is not None and value > datetime.now(UTC).date() + timedelta(days=1):
        raise ValueError("date cannot be in the future")
    return value


class ComplianceSource(BaseModel):
    """A verifiable citation behind a compliance verdict.

    Each source may carry that screener's own verdict and as-of date so
    disagreeing screeners stay visible side by side instead of being merged.
    """

    screener_name: str = Field(..., min_length=1, max_length=100)
    url: str = Field(..., min_length=1, max_length=MAX_SOURCE_URL_LENGTH)
    rule_reference: str = Field(..., min_length=1, max_length=500)
    verdict: ComplianceVerdict | None = None
    as_of_date: date | None = None

    @field_validator("screener_name", "rule_reference", "url", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @field_validator("screener_name", "rule_reference")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        parsed = urlparse(v)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or any(ch.isspace() or ord(ch) < 32 for ch in v)
        ):
            raise ValueError("url must be an absolute http(s) URL")
        return v

    @field_validator("as_of_date")
    @classmethod
    def _source_date_not_future(cls, v: date | None) -> date | None:
        return _not_in_future(v)


class HoldingComplianceUpsert(BaseModel):
    verdict: ComplianceVerdict
    sources: list[ComplianceSource] = Field(..., min_length=1, max_length=MAX_COMPLIANCE_SOURCES)
    as_of_date: date
    impure_income_pct: Decimal | None = Field(
        default=None, ge=0, le=100, max_digits=7, decimal_places=4
    )

    @field_validator("as_of_date")
    @classmethod
    def _as_of_not_future(cls, v: date) -> date:
        return _not_in_future(v)  # type: ignore[return-value]


class HoldingComplianceResponse(BaseModel):
    public_id: uuid.UUID
    holding_public_id: uuid.UUID
    verdict: ComplianceVerdict
    sources: list[ComplianceSource]
    sources_disagree: bool = False
    as_of_date: date
    impure_income_pct: Decimal | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(json_encoders={Decimal: str})


class PurificationEstimateResponse(BaseModel):
    scope: Literal["holding", "dividend"]
    target_public_id: uuid.UUID
    holding_public_id: uuid.UUID | None = None
    verdict: ComplianceVerdict
    compliance_on_file: bool
    verdict_as_of_date: date | None = None
    impure_income_pct: Decimal | None = None
    base_amount: Decimal
    purification_amount: Decimal
    currency: str
    dividend_count: int = 0
    basis: str
    assumptions: list[str] = Field(default_factory=list)
    disclaimer: str = PURIFICATION_DISCLAIMER

    model_config = ConfigDict(json_encoders={Decimal: str})


class DividendHistoryResponse(BaseModel):
    currency: str
    total_dividends_received: Decimal
    trailing_12m_dividends: Decimal
    monthly_history: list[MonthlyDividendPoint]

    model_config = ConfigDict(json_encoders={Decimal: str})


class CompanySectorRow(BaseModel):
    """One ``set_company_sector`` row (spec-100).

    Exactly one company key: ``holding_public_id``, ``isin``, or
    ``ticker`` + ``exchange``. ``sector`` is required (max 100 chars, GICS
    names recommended; case/whitespace variants of GICS names are
    canonicalised). ``industry`` / ``country_code`` are optional and only
    written when provided.
    """

    holding_public_id: uuid.UUID | None = None
    isin: str | None = Field(default=None, max_length=20)
    ticker: str | None = Field(default=None, max_length=20)
    exchange: str | None = Field(default=None, max_length=50)
    sector: str | None = Field(default=None, max_length=100)
    industry: str | None = Field(default=None, max_length=100)
    country_code: str | None = Field(default=None, max_length=10)

    @field_validator("isin", "ticker")
    @classmethod
    def _upper_identifier(cls, value: str | None) -> str | None:
        return _normalize_identifier(value)

    @field_validator("exchange")
    @classmethod
    def _strip_exchange(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("sector", "industry")
    @classmethod
    def _normalise_free_text(cls, value: str | None) -> str | None:
        return normalise_sector(value)

    @field_validator("country_code")
    @classmethod
    def _upper_country(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper() or None

    @model_validator(mode="after")
    def _require_sector_and_one_key(self) -> "CompanySectorRow":
        if self.sector is None:
            raise ValueError("sector is required")
        keys = [
            self.holding_public_id is not None,
            self.isin is not None,
            self.ticker is not None or self.exchange is not None,
        ]
        if sum(keys) != 1:
            raise ValueError(
                "supply exactly one of holding_public_id, isin, or ticker plus exchange"
            )
        if (self.ticker is None) != (self.exchange is None):
            raise ValueError("ticker and exchange must be supplied together")
        return self
