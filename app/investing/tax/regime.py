"""The two capital-gains regimes and how a holding is classified.

The regime a holding falls under determines the holding-period threshold,
the sections, the rates and the exemption. Getting this wrong produces a
plausible-looking number that is wrong in three ways at once, so the
statutory data lives here as data rather than scattered conditionals.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class RegimeType(StrEnum):
    """Which statutory regime applies to a holding."""

    indian = "indian"
    """Indian listed securities with STT — Sec 111A / 112A."""

    foreign = "foreign"
    """US/foreign listed shares, no STT — Sec 112."""


@dataclass(frozen=True)
class RegimeSpec:
    """The statutory parameters of one regime."""

    key: RegimeType
    ltcg_holding_months: int
    """Months a holding must exceed to be long-term."""

    stcg_section: str
    ltcg_section: str
    ltcg_rate_pct: Decimal
    ltcg_exemption_inr: Decimal
    """Annual exemption against LTCG. Zero for foreign shares."""

    stcg_is_flat: bool
    """True when STCG has a flat rate; False when it follows the slab."""

    stcg_flat_rate_pct: Decimal | None
    indexation_available: bool
    cess_pct: Decimal
    label: str
    description: str


INDIAN_REGIME = RegimeSpec(
    key=RegimeType.indian,
    ltcg_holding_months=12,
    stcg_section="111A",
    ltcg_section="112A",
    ltcg_rate_pct=Decimal("0.125"),
    ltcg_exemption_inr=Decimal("125000"),
    stcg_is_flat=True,
    stcg_flat_rate_pct=Decimal("0.20"),
    indexation_available=False,
    cess_pct=Decimal("0.04"),
    label="Indian listed (STT paid)",
    description=(
        "Listed on an Indian recognised stock exchange with STT paid. "
        "Long-term after 12 months (Sec 112A, 12.5% above the ₹1.25 lakh "
        "annual exemption); short-term at 20% flat (Sec 111A)."
    ),
)

FOREIGN_REGIME = RegimeSpec(
    key=RegimeType.foreign,
    ltcg_holding_months=24,
    stcg_section="112(1)(a)(ii)",
    ltcg_section="112(1)(c)",
    ltcg_rate_pct=Decimal("0.125"),
    ltcg_exemption_inr=Decimal("0"),
    stcg_is_flat=False,
    stcg_flat_rate_pct=None,
    indexation_available=False,
    cess_pct=Decimal("0.04"),
    label="Foreign / US listed (no STT)",
    description=(
        "Not on an Indian recognised stock exchange and no STT paid, so the "
        "concessional equity regime does not apply. Long-term after 24 "
        "months (Sec 112(1)(c), 12.5% with no exemption); short-term at the "
        "taxpayer's marginal slab rate (Sec 112(1)(a)(ii))."
    ),
)

REGIMES: dict[RegimeType, RegimeSpec] = {
    RegimeType.indian: INDIAN_REGIME,
    RegimeType.foreign: FOREIGN_REGIME,
}


class ClassificationSource(StrEnum):
    """Which rule decided a holding's regime — surfaced in the UI."""

    explicit_regime = "explicit_regime"
    """A workspace-level override; the user took control."""

    instrument_override = "instrument_override"
    """A per-holding override on the instrument."""

    security_master = "security_master"
    """The reference security's country code."""

    account_market_scope = "account_market_scope"
    """The brokerage account's market scope."""

    currency_fallback = "currency_fallback"
    """Last resort: a non-INR currency implies a foreign listing."""


@dataclass(frozen=True)
class RegimeClassification:
    """A holding's regime plus the rule that chose it."""

    regime: RegimeSpec
    source: ClassificationSource
    detail: str
    """Human-readable explanation, e.g. the exchange or country used."""


def classify_regime(
    *,
    explicit_regime: RegimeType | None = None,
    instrument_country_override: str | None = None,
    security_country_code: str | None = None,
    account_market_scope: str | None = None,
    holding_currency: str | None = None,
) -> RegimeClassification:
    """Decide which regime a holding falls under.

    Precedence, highest first:

    1. ``explicit_regime`` — the workspace override wins over everything.
    2. ``instrument_country_override`` — the per-holding flag.
    3. ``security_country_code`` — the reference security master. ``"IN"``
       means an Indian recognised exchange; any other country is foreign.
    4. ``account_market_scope`` — the broker account's default.
    5. A non-INR holding currency implies a foreign listing.
    """
    if explicit_regime is not None:
        regime = REGIMES[RegimeType(explicit_regime)]
        return RegimeClassification(
            regime=regime,
            source=ClassificationSource.explicit_regime,
            detail=(
                f"Explicit workspace override — treating these holdings as {regime.label.lower()}."
            ),
        )

    if instrument_country_override:
        country = instrument_country_override.strip().upper()
        if country == "IN":
            return RegimeClassification(
                regime=INDIAN_REGIME,
                source=ClassificationSource.instrument_override,
                detail="Holding-level override set to India.",
            )
        return RegimeClassification(
            regime=FOREIGN_REGIME,
            source=ClassificationSource.instrument_override,
            detail=f"Holding-level override set to {country}.",
        )

    if security_country_code:
        country = security_country_code.strip().upper()
        if country == "IN":
            return RegimeClassification(
                regime=INDIAN_REGIME,
                source=ClassificationSource.security_master,
                detail="Reference security is listed in India.",
            )
        return RegimeClassification(
            regime=FOREIGN_REGIME,
            source=ClassificationSource.security_master,
            detail=f"Reference security is listed in {country}.",
        )

    if account_market_scope and account_market_scope != "mixed":
        regime = INDIAN_REGIME if account_market_scope == "india" else FOREIGN_REGIME
        return RegimeClassification(
            regime=regime,
            source=ClassificationSource.account_market_scope,
            detail=f"Brokerage account default is '{account_market_scope}'.",
        )

    currency = (holding_currency or "").strip().upper()
    if currency and currency != "INR":
        return RegimeClassification(
            regime=FOREIGN_REGIME,
            source=ClassificationSource.currency_fallback,
            detail=f"Holding is denominated in {currency}, not INR.",
        )

    return RegimeClassification(
        regime=INDIAN_REGIME,
        source=ClassificationSource.currency_fallback,
        detail="Defaulted to the Indian regime.",
    )
