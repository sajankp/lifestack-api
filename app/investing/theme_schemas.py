"""Schemas for holding theme tags, look-through exposure and allocation-vs-target.

All money and percentage fields are ``Decimal`` (serialised as strings).
Percentages are 0-100 numbers rounded to 2 dp, amounts are in the response's
``currency`` rounded to 2 dp.
"""

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_WHITESPACE_RE = re.compile(r"\s+")

GroupBy = Literal["company", "sector", "country", "theme"]
AnalysisStatus = Literal["complete", "partial"]


def normalize_theme(value: str) -> str:
    """Trim and collapse internal whitespace; reject an empty theme."""
    cleaned = _WHITESPACE_RE.sub(" ", value).strip()
    if not cleaned:
        raise ValueError("theme must not be empty")
    return cleaned


# ---------------------------------------------------------------------------
# Theme tags
# ---------------------------------------------------------------------------


class ThemeTagInput(BaseModel):
    """One theme on a holding, with an optional target (percent of the portfolio)."""

    theme: str = Field(min_length=1, max_length=64)
    target_weight_pct: Decimal | None = Field(
        default=None,
        ge=0,
        le=100,
        decimal_places=2,
        description="Share of the whole portfolio (0-100) this holding should contribute "
        "to the theme. Omit for a grouping-only tag with no target.",
    )

    @field_validator("theme")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return normalize_theme(value)


class HoldingThemeTagsInput(BaseModel):
    """Replace the complete tag set of one holding (empty ``tags`` clears it)."""

    holding_id: uuid.UUID = Field(description="Holding public_id")
    tags: list[ThemeTagInput] = Field(default_factory=list, max_length=20)


class HoldingThemeTagsBulkRequest(BaseModel):
    holdings: list[HoldingThemeTagsInput] = Field(min_length=1, max_length=200)


class HoldingThemeTagsReplace(BaseModel):
    tags: list[ThemeTagInput] = Field(default_factory=list, max_length=20)


class HoldingThemeTagResponse(BaseModel):
    public_id: uuid.UUID
    holding_id: uuid.UUID
    theme: str
    target_weight_pct: Decimal | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(json_encoders={Decimal: str})


class ThemeTargetRow(BaseModel):
    theme: str
    target_weight_pct: Decimal | None
    holdings_count: int


class HoldingThemeTagsResult(BaseModel):
    """Outcome of a tag write: the stored tags plus the resulting theme targets."""

    tags: list[HoldingThemeTagResponse]
    theme_targets: list[ThemeTargetRow]
    total_target_pct: Decimal
    unallocated_target_pct: Decimal


# ---------------------------------------------------------------------------
# Look-through exposure
# ---------------------------------------------------------------------------


class LookthroughCompanyRow(BaseModel):
    company_id: uuid.UUID
    company_name: str
    company_ticker: str | None
    sector: str | None
    country_code: str | None
    direct_value: Decimal
    lookthrough_value: Decimal
    total_value: Decimal
    pct: Decimal


class LookthroughBucketRow(BaseModel):
    key: str
    label: str
    value: Decimal
    pct: Decimal


class LookthroughThemeRow(BaseModel):
    theme: str
    holdings_count: int
    value: Decimal
    pct: Decimal


class FundCoverageRow(BaseModel):
    instrument_id: uuid.UUID
    symbol: str
    name: str | None
    instrument_type: str
    value: Decimal
    itemised_pct: Decimal
    snapshot_date: date | None
    source: str | None
    constituent_rows: int


class LookthroughExposureResponse(BaseModel):
    as_of_date: date
    currency: str | None
    status: AnalysisStatus
    group_by: GroupBy | None = None
    total_value: Decimal
    itemised_value: Decimal
    not_itemised_value: Decimal
    not_itemised_pct: Decimal
    by_company: list[LookthroughCompanyRow] | None = None
    by_sector: list[LookthroughBucketRow] | None = None
    by_country: list[LookthroughBucketRow] | None = None
    by_theme: list[LookthroughThemeRow] | None = None
    unthemed_value: Decimal | None = None
    fund_coverage: list[FundCoverageRow] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fx_as_of: datetime | None = None
    fx_rates_used: dict[str, Decimal] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Allocation vs target
# ---------------------------------------------------------------------------


class AllocationHoldingRow(BaseModel):
    holding_id: uuid.UUID
    symbol: str
    current_value: Decimal
    target_weight_pct: Decimal | None


class AllocationThemeRow(BaseModel):
    theme: str
    holdings_count: int
    current_value: Decimal
    current_pct: Decimal
    target_weight_pct: Decimal | None
    drift_pct: Decimal | None = Field(
        description="current_pct - target_weight_pct; negative = underweight. "
        "Null when the theme has no target."
    )
    target_value: Decimal | None
    shortfall: Decimal = Field(
        description="How far the theme sits below its target value measured against the "
        "portfolio including the new cash; never negative."
    )
    suggested_new_cash: Decimal
    post_deployment_pct: Decimal
    holdings: list[AllocationHoldingRow] = Field(default_factory=list)


class AllocationVsTargetResponse(BaseModel):
    as_of_date: date
    currency: str | None
    status: AnalysisStatus
    portfolio_value: Decimal
    new_cash: Decimal
    post_deployment_value: Decimal
    total_target_pct: Decimal
    unallocated_target_pct: Decimal
    unthemed_value: Decimal
    total_suggested: Decimal
    unallocated_cash: Decimal
    themes: list[AllocationThemeRow]
    warnings: list[str] = Field(default_factory=list)
    fx_as_of: datetime | None = None
    fx_rates_used: dict[str, Decimal] = Field(default_factory=dict)
