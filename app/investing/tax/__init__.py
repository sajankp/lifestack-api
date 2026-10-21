"""Indian capital gains tax estimation for investment holdings.

Pure, stateless domain logic for spec-099. No DB access, no I/O — every
function here takes plain data and returns plain data, which is what makes
the engine testable and lets the same code serve the REST API, the MCP
tools and the CLI.

The statutory facts this encodes:

* Foreign (US-listed) shares are **not** on an Indian recognised stock
  exchange and pay no STT, so the concessional equity regime (111A STCG /
  112A LTCG) does not apply. They fall under **Section 112**:
  a 24-month holding threshold, LTCG at 12.5% with **no** ₹1.25 lakh
  exemption, and STCG at the taxpayer's marginal slab rate.
* Indian listed securities with STT use 111A (STCG 20% flat) and 112A
  (LTCG 12.5% above ₹1.25 lakh per year).
* Capital gains convert to INR at the SBI telegraphic transfer **buying**
  rate on the last day of the month preceding the month of transfer
  (Rule 115; Rule 206 from FY 2026-27).
"""

from app.investing.tax.engine import compute_tax_estimate
from app.investing.tax.fy import fy_bounds, fy_label_for_date, parse_fy_label
from app.investing.tax.gains import (
    GainTotals,
    OrderView,
    TaxLot,
    effective_buy_cost_per_unit,
    replay_fifo_lots,
)
from app.investing.tax.regime import (
    INDIAN_REGIME,
    RegimeSpec,
    RegimeType,
    FOREIGN_REGIME,
    classify_regime,
)
from app.investing.tax.slab import SlabResolution, derive_slab
from app.investing.tax.exchanges import country_for_exchange, resolve_exchange
from app.investing.tax.tax_profile import TaxProfile

__all__ = [
    "FOREIGN_REGIME",
    "GainTotals",
    "OrderView",
    "INDIAN_REGIME",
    "RegimeSpec",
    "RegimeType",
    "SlabResolution",
    "TaxLot",
    "TaxProfile",
    "classify_regime",
    "country_for_exchange",
    "resolve_exchange",
    "compute_tax_estimate",
    "derive_slab",
    "effective_buy_cost_per_unit",
    "fy_bounds",
    "fy_label_for_date",
    "parse_fy_label",
    "replay_fifo_lots",
]
