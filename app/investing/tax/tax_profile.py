"""The workspace tax profile — the user's tax configuration.

Plain dataclasses so the engine stays free of DB access; the service
layer maps these to and from the ``workspace_tax_profiles`` table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.investing.tax.regime import RegimeType
from app.investing.tax.slab import CESS_RATE, TAX_REGIME_NEW

RESIDENT = "resident"
RESIDENT_NOT_ORDINARILY_RESIDENT = "resident_not_ordinarily_resident"
NON_RESIDENT = "non_resident"

RESIDENT_STATUSES = (
    RESIDENT,
    RESIDENT_NOT_ORDINARILY_RESIDENT,
    NON_RESIDENT,
)


@dataclass
class TaxProfile:
    """A workspace's tax configuration."""

    resident_status: str = RESIDENT
    tax_regime: str = TAX_REGIME_NEW
    """``new`` (Sec 115BAC) or ``old``."""

    other_income: Decimal = Decimal("0")
    """Taxable other income, after the user's own deductions."""

    slab_override_pct: Decimal | None = None
    surcharge_override_pct: Decimal | None = None
    cess_pct: Decimal = CESS_RATE
    explicit_regime: RegimeType | None = None
    apply_carry_forward: bool = True

    fx_overrides: dict[str, Decimal] = field(default_factory=dict)
    """Rule 115 rate per sale month, keyed ``"YYYY-MM"``."""

    notes: str | None = None


@dataclass
class FxRate:
    """The FX rate used for one sale month, and where it came from."""

    month: str
    specified_date: str
    currency: str
    rate: Decimal
    source: str
    """``manual`` or ``app_rate_estimate``."""

    is_estimated: bool

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "month": self.month,
            "specified_date": self.specified_date,
            "currency": self.currency,
            "rate": str(self.rate),
            "source": self.source,
            "is_estimated": self.is_estimated,
        }
