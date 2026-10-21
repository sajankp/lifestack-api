"""Request/response schemas for the capital gains tax endpoints (spec-099)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator


class TaxBucketResponse(BaseModel):
    regime: str
    section: str
    rate_pct: str
    gain_native: str
    gain_inr: str
    fees_native: str
    fees_inr: str
    exemption_applied: str
    tax: str
    sell_count: int
    lot_count: int


class TaxFxRateResponse(BaseModel):
    month: str
    specified_date: str
    currency: str
    rate: str
    source: str
    is_estimated: bool


class TaxSlabResponse(BaseModel):
    rate_pct: str
    source: str
    tax_regime: str
    surcharge_pct: str
    surcharge_source: str
    cess_pct: str
    rebate_amount: str
    total_income_considered: str
    marginal_relief_applied: bool


class TaxPositionResponse(BaseModel):
    symbol: str
    account_name: str
    instrument_type: str
    currency: str
    quantity: str
    cost_per_unit: str
    acquired_at: str
    sold_at: str
    long_term_mark_date: str
    holding_months: int
    is_long_term: bool
    regime: str
    classified_by: str
    classification_detail: str
    gain_native: str
    gain_inr: str
    fees_native: str
    sale_price_per_unit: str


class TaxSummaryResponse(BaseModel):
    status: str
    fy_label: str
    currency: str
    buckets: dict[str, TaxBucketResponse]
    total_gain_inr: str
    total_tax_inr: str
    effective_rate_pct: str
    slab: TaxSlabResponse | None = None
    fx_rates: list[TaxFxRateResponse]
    positions: list[TaxPositionResponse]
    assumptions: list[str]
    disclaimers: list[str]
    reconciliation_failures: list[dict[str, str]] = []
    missing_fx_rates: list[dict[str, str]] = []
    message: str | None = None


class TaxRegimeResponse(BaseModel):
    key: str
    label: str
    description: str
    ltcg_holding_months: int
    stcg_section: str
    ltcg_section: str
    ltcg_rate_pct: str
    ltcg_exemption_inr: str
    stcg_is_flat: bool
    stcg_flat_rate_pct: str | None
    cess_pct: str


class TaxProfileResponse(BaseModel):
    public_id: str
    resident_status: str
    tax_regime: str
    other_income: str
    slab_override_pct: str | None
    surcharge_override_pct: str | None
    cess_pct: str
    explicit_regime: str | None
    apply_carry_forward: bool
    fx_overrides: dict[str, str]
    notes: str | None

    model_config = ConfigDict(from_attributes=True)


class TaxProfileUpdate(BaseModel):
    resident_status: str | None = None
    tax_regime: str | None = None
    other_income: str | None = None
    slab_override_pct: str | None = None
    surcharge_override_pct: str | None = None
    cess_pct: str | None = None
    explicit_regime: str | None = None
    apply_carry_forward: bool | None = None
    fx_overrides: dict[str, str] | None = None
    notes: str | None = None

    @field_validator("resident_status")
    @classmethod
    def _validate_resident_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = {"resident", "resident_not_ordinarily_resident", "non_resident"}
        if value not in allowed:
            raise ValueError(f"resident_status must be one of {sorted(allowed)}")
        return value

    @field_validator("tax_regime")
    @classmethod
    def _validate_tax_regime(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in {"new", "old"}:
            raise ValueError("tax_regime must be 'new' or 'old'")
        return value

    @field_validator("explicit_regime")
    @classmethod
    def _validate_explicit_regime(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in {"indian", "foreign"}:
            raise ValueError("explicit_regime must be 'indian' or 'foreign'")
        return value

    @field_validator("slab_override_pct", "surcharge_override_pct", "cess_pct")
    @classmethod
    def _validate_pct(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("must be a number") from exc
        if number < 0 or number > 100:
            raise ValueError("must be between 0 and 100")
        return value

    @field_validator("other_income")
    @classmethod
    def _validate_other_income(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("must be a number") from exc
        if number < 0:
            raise ValueError("must not be negative")
        return value
