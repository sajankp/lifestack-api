"""Liberalised Remittance Scheme (LRS) summary.

Outward remittances are INR-funded capital transfers from the spending side
into a *foreign-currency* investing account (e.g. Paasa / a US brokerage).
Domestic INR brokerage funding and repatriations (investing -> spending) are
not LRS remittances and are excluded.

Rules encoded here (RBI / Income-tax Act s.206C(1G)):

* The LRS cap is USD 250,000 per **financial year** (1 Apr - 31 Mar), per
  resident individual -- not per calendar year.
* TCS applies at 20% (non-education / non-medical purposes) on the aggregate
  INR remitted in the FY above a threshold. The threshold was INR 7 lakh until
  31 Mar 2025 and INR 10 lakh from FY 2025-26 (Finance Act 2025).
* The FY and each transfer's date are evaluated in IST (UTC+05:30): a transfer
  at 01:00 IST on 1 April belongs to the new FY even though it is 31 March in UTC.

Limits (stated in the response ``assumptions``): the app only sees this
workspace, while the real cap/threshold are per PAN across all banks; the
purpose is assumed to be investment (20% TCS tier).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.core.exceptions import ValidationError
from app.finance.models import CapitalTransfer
from app.finance.repository import CapitalTransferRepository, FxRateRepository
from app.investing.tax.fy import current_fy_label, parse_fy_label

IST = timezone(timedelta(hours=5, minutes=30))

LRS_CAP_USD = Decimal("250000.00")
TCS_RATE_PCT = Decimal("20.00")
_TWO_PLACES = Decimal("0.01")

# First FY with the INR 10 lakh threshold (Finance Act 2025, w.e.f. 1 Apr 2025).
_TCS_THRESHOLD_10_LAKH_FROM_FY_START_YEAR = 2025
# The flat 20% rate applies to the whole of FY 2024-25 onwards; the rate changed
# mid-FY on 1 Oct 2023, so earlier FYs are indicative only.
_TCS_20_PCT_FROM_FY_START_YEAR = 2024
# LRS was introduced in 2004; labels before that are certainly typos.
_MIN_FY_START_YEAR = 2004


def tcs_threshold_inr(fy_start_year: int) -> Decimal:
    """TCS threshold in INR for the FY starting in ``fy_start_year``."""
    if fy_start_year >= _TCS_THRESHOLD_10_LAKH_FROM_FY_START_YEAR:
        return Decimal("1000000.00")
    return Decimal("700000.00")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def resolve_fy(fy_label: str | None) -> tuple[str, date, date]:
    """Validate an FY label (default: current FY) -> (label, start, end)."""
    label = (fy_label or "").strip() or current_fy_label(datetime.now(IST))
    try:
        start, end = parse_fy_label(label)
    except ValueError as exc:
        raise ValidationError(detail=str(exc)) from exc
    max_start_year = int(current_fy_label(datetime.now(IST))[:4]) + 1
    if not _MIN_FY_START_YEAR <= start.year <= max_start_year:
        raise ValidationError(
            detail=f"FY {label!r} is out of range ({_MIN_FY_START_YEAR}-{max_start_year})"
        )
    return label, start, end


class LrsService:
    def __init__(
        self,
        transfer_repository: CapitalTransferRepository,
        fx_rate_repository: FxRateRepository | None = None,
    ) -> None:
        self.transfer_repository = transfer_repository
        self.fx_rate_repository = fx_rate_repository

    async def _usd_rates(self, transfers: list[CapitalTransfer]) -> dict[tuple[str, date], Decimal]:
        """System FX rates (currency -> USD) for non-USD destinations."""
        rates: dict[tuple[str, date], Decimal] = {}
        if self.fx_rate_repository is None:
            return rates
        needed = {
            (t.to_currency_code, t.occurred_at.astimezone(IST).date())
            for t in transfers
            if t.to_currency_code != "USD"
        }
        for currency, on_date in sorted(needed):
            row = await self.fx_rate_repository.get_latest_rate(
                base_currency_code=currency,
                quote_currency_code="USD",
                as_of=datetime.combine(on_date, time.max, tzinfo=IST),
            )
            if row is not None:
                rates[(currency, on_date)] = Decimal(str(row.rate))
        return rates

    @staticmethod
    def _usd_value(
        transfer: CapitalTransfer, usd_rates: dict[tuple[str, date], Decimal]
    ) -> Decimal | None:
        """USD remitted by one transfer, or None when it cannot be converted.

        ``fx_rate_used`` is to-currency per 1 INR, so ``gross * rate`` is the
        amount remitted in the destination currency before fees.
        """
        if transfer.fx_rate_used is None:
            return None
        remitted = transfer.gross_amount * transfer.fx_rate_used
        if transfer.to_currency_code == "USD":
            return remitted
        rate = usd_rates.get((
            transfer.to_currency_code,
            transfer.occurred_at.astimezone(IST).date(),
        ))
        return None if rate is None else remitted * rate

    async def get_lrs_summary(
        self, workspace_id: int, fy_label: str | None = None
    ) -> dict[str, Any]:
        label, start, end = resolve_fy(fy_label)
        transfers = list(
            await self.transfer_repository.list_outward_remittances(
                workspace_id,
                start=datetime.combine(start, time.min, tzinfo=IST),
                end_exclusive=datetime.combine(end + timedelta(days=1), time.min, tzinfo=IST),
            )
        )
        usd_rates = await self._usd_rates(transfers)

        total_usd = Decimal("0")
        total_inr = Decimal("0")
        unconverted: list[str] = []
        for t in transfers:
            total_inr += t.gross_amount  # from_currency is INR by construction
            usd = self._usd_value(t, usd_rates)
            if usd is None:
                unconverted.append(str(t.public_id))
            else:
                total_usd += usd
        total_usd = _money(total_usd)
        total_inr = _money(total_inr)

        threshold = tcs_threshold_inr(start.year)
        tcs_applicable = total_inr > threshold
        tcs_amount = (
            _money((total_inr - threshold) * TCS_RATE_PCT / Decimal("100"))
            if tcs_applicable
            else Decimal("0.00")
        )

        assumptions = [
            "Remittances = INR-funded transfers from spending accounts into "
            "foreign-currency investing accounts (LRS outward remittances). "
            "INR brokerage funding and repatriations are excluded.",
            f"LRS cap: USD {LRS_CAP_USD:,.0f} per financial year (1 Apr - 31 Mar, IST).",
            f"TCS: {TCS_RATE_PCT}% on the INR remitted above INR {threshold:,.0f} in the FY "
            "(investment purpose; education/medical rates differ).",
            "Totals cover this workspace only; the real cap and TCS threshold are per PAN "
            "across all banks and workspaces.",
        ]
        if start.year < _TCS_20_PCT_FROM_FY_START_YEAR:
            assumptions.append(
                "TCS rates before FY 2024-25 differed (changed on 1 Oct 2023); the TCS "
                "figures for this FY are indicative only."
            )
        if unconverted:
            assumptions.append(
                f"{len(unconverted)} transfer(s) could not be converted to USD (no "
                "fx_rate_used or no stored FX rate) and are excluded from total_usd."
            )

        return {
            "fy_label": label,
            "fy_start": start,
            "fy_end": end,
            "transfer_count": len(transfers),
            "total_usd": str(total_usd),
            "cap_usd": str(LRS_CAP_USD),
            "cap_remaining_usd": str(max(Decimal("0.00"), LRS_CAP_USD - total_usd)),
            "cap_exceeded": total_usd > LRS_CAP_USD,
            "pct_of_cap": str(_money(total_usd / LRS_CAP_USD * Decimal("100"))),
            "total_inr": str(total_inr),
            "tcs_threshold_inr": str(threshold),
            "tcs_rate_pct": str(TCS_RATE_PCT),
            "tcs_applicable": tcs_applicable,
            "tcs_amount_inr": str(tcs_amount),
            "currency": "USD",
            "unconverted_transfer_ids": unconverted,
            "assumptions": assumptions,
        }


__all__ = ["IST", "LRS_CAP_USD", "LrsService", "resolve_fy", "tcs_threshold_inr"]
