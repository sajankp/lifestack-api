"""Incremental (FY-aware) tax and calendar-day semantics of simulate_sale.

The first worker's simulation applied the full ₹1.25 lakh Sec 112A
exemption as if the simulated sale were the FY's only long-term gain. These
tests pin the fix: realised lots already booked in the FY are fed to the
engine, and the reported tax is the difference with/without the sale.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.investing.tax.engine import compute_tax_estimate
from app.investing.tax.gains import OrderView, TaxLot
from app.investing.tax.regime import FOREIGN_REGIME, INDIAN_REGIME
from app.investing.tax.simulation import (
    HoldingSnapshot,
    LotSnapshot,
    harvest_candidates,
    list_tax_lots,
    simulate_sale,
)
from app.investing.tax.tax_profile import TaxProfile

IST = timezone(timedelta(hours=5, minutes=30))
# High other income: no 87A rebate noise, surcharge stays 0 (< ₹50 lakh).
RICH = Decimal("3000000")


def _profile(**kwargs) -> TaxProfile:
    kwargs.setdefault("other_income", RICH)
    return TaxProfile(**kwargs)


def _lot(lot_id, qty, cost, acquired) -> LotSnapshot:
    return LotSnapshot(
        lot_id=lot_id,
        quantity=Decimal(qty),
        cost_per_unit=Decimal(cost),
        acquired_at=datetime.fromisoformat(acquired),
    )


def _holding(
    lots, *, regime=INDIAN_REGIME.key, currency="INR", symbol="INFY", holding_id=1
) -> HoldingSnapshot:
    return HoldingSnapshot(
        holding_id=holding_id,
        symbol=symbol,
        account_id="acct-1",
        account_name="Broker",
        currency=currency,
        regime=regime,
        regime_source="security_master",
        lots=tuple(lots),
    )


def _order(order_type, public_id, qty, price, when, *, symbol="TCS", currency="INR") -> OrderView:
    q, p = Decimal(qty), Decimal(price)
    return OrderView(
        public_id=public_id,
        order_type=order_type,
        symbol=symbol,
        quantity=q,
        price_per_unit=p,
        gross_amount=q * p,
        brokerage_fee=Decimal("0"),
        tax_amount=Decimal("0"),
        other_fees=Decimal("0"),
        occurred_at=datetime.fromisoformat(when),
        currency=currency,
        account_id="acct-1",
        account_name="Broker",
        instrument_type="stock",
    )


def _realised(buy, sell, regime=INDIAN_REGIME, fy="2026-27", fx=None, **kw) -> list[TaxLot]:
    """Real engine lots for one buy/sell pair (what TaxService.compute yields)."""
    estimate = compute_tax_estimate(
        orders=[buy, sell],
        fy_label=fy,
        profile=_profile(fx_overrides=fx or {}, **kw),
        regime_resolver=lambda o: (regime, "security_master", "x"),
    )
    assert estimate.status == "success", estimate.message
    return estimate.lots


def _sell_indian(holding, qty, price, *, when="2026-10-16T00:00:00+00:00", realised=(), **kw):
    return simulate_sale(
        holding,
        qty=Decimal(qty),
        sale_date=datetime.fromisoformat(when),
        sale_price=Decimal(price),
        profile=_profile(),
        realised_lots=realised,
        **kw,
    )


LT_LOT = ("1", "1000", "100", "2024-01-01T00:00:00+00:00")


class TestIncrementalExemption:
    def _realised_ltcg(self, gain_per_unit: str, qty: str = "1000"):
        buy = _order("buy", "b-r", qty, "100", "2024-01-01T00:00:00+00:00")
        sell = _order(
            "sell",
            "s-r",
            qty,
            str(Decimal("100") + Decimal(gain_per_unit)),
            "2026-06-10T00:00:00+00:00",
        )
        return _realised(buy, sell)

    def test_only_the_remaining_exemption_shelters_the_simulated_sale(self):
        realised = self._realised_ltcg("120")  # ₹1.2 lakh LTCG already booked
        h = _holding([_lot(*LT_LOT)])
        result = _sell_indian(h, "400", "225", realised=realised)  # +₹50,000 LTCG
        assert result.status == "success"
        assert result.fy_realised_lots == 1
        assert result.fy_realised_ltcg_inr == Decimal("120000.00")
        assert result.ltcg_exemption_remaining_inr == Decimal("5000.00")
        assert result.ltcg_exemption_used_inr == Decimal("5000.00")
        assert result.tax_before_inr == Decimal("0.00")
        # (50,000 - 5,000 remaining exemption) x 12.5% x 1.04 cess
        assert result.tax_inr == Decimal("5850.00")
        assert result.tax_after_inr == Decimal("5850.00")
        text = " ".join(result.assumptions)
        assert "120000" in text and "5000" in text and "2026-27" in text

    def test_isolation_would_have_reported_zero_tax(self):
        """Regression: the old isolation behaviour shelters the whole ₹50k."""
        h = _holding([_lot(*LT_LOT)])
        isolated = _sell_indian(h, "400", "225")
        assert isolated.tax_inr == Decimal("0.00")
        with_prior = _sell_indian(h, "400", "225", realised=self._realised_ltcg("120"))
        assert with_prior.tax_inr == Decimal("5850.00")

    def test_exemption_already_exhausted_taxes_every_rupee(self):
        h = _holding([_lot(*LT_LOT)])
        result = _sell_indian(h, "400", "225", realised=self._realised_ltcg("200"))
        assert result.ltcg_exemption_remaining_inr == Decimal("0.00")
        assert result.ltcg_exemption_used_inr == Decimal("0.00")
        assert result.tax_inr == Decimal("6500.00")  # 50,000 x 12.5% x 1.04

    def test_realised_loss_does_not_create_extra_exemption(self):
        h = _holding([_lot(*LT_LOT)])
        result = _sell_indian(h, "400", "225", realised=self._realised_ltcg("-30"))
        # Net bucket = 50,000 - 30,000 = 20,000, fully inside the ₹1.25 lakh cap.
        assert result.ltcg_exemption_remaining_inr == Decimal("125000")
        assert result.tax_inr == Decimal("0.00")

    def test_loss_sale_reduces_tax_on_gains_already_realised(self):
        h = _holding([_lot(*LT_LOT)])
        # realised +₹2 lakh LTCG (tax on 75k above exemption = 9,750);
        # selling at a ₹30k loss leaves 1.7 lakh (tax on 45k = 5,850).
        result = _sell_indian(
            h, "300", "0", realised=self._realised_ltcg("200"), when="2026-10-16T00:00:00+00:00"
        )
        assert result.gain_native == Decimal("-30000.00")
        assert result.tax_before_inr == Decimal("9750.00")
        assert result.tax_after_inr == Decimal("5850.00")
        assert result.tax_inr == Decimal("-3900.00")
        assert any("cross-bucket" in a for a in result.assumptions)

    def test_realised_lots_from_another_fy_are_ignored(self):
        buy = _order("buy", "b-o", "1000", "100", "2024-01-01T00:00:00+00:00")
        sell = _order("sell", "s-o", "1000", "300", "2025-06-10T00:00:00+00:00")
        previous_fy = _realised(buy, sell, fy="2025-26")
        h = _holding([_lot(*LT_LOT)])
        result = _sell_indian(h, "400", "225", realised=previous_fy)
        assert result.fy_realised_lots == 0
        assert result.tax_inr == Decimal("0.00")  # whole ₹50k sheltered

    def test_input_lots_are_not_mutated(self):
        realised = self._realised_ltcg("120")
        snapshot = [(lot.gain_inr, lot.gain_native) for lot in realised]
        _sell_indian(_holding([_lot(*LT_LOT)]), "400", "225", realised=realised)
        assert [(lot.gain_inr, lot.gain_native) for lot in realised] == snapshot

    def test_foreign_regime_has_no_exemption_but_sees_realised_income(self):
        buy = _order(
            "buy", "b-f", "10", "100", "2022-01-01T00:00:00+00:00", symbol="AAPL", currency="USD"
        )
        sell = _order(
            "sell", "s-f", "10", "200", "2026-06-10T00:00:00+00:00", symbol="AAPL", currency="USD"
        )
        fx = {"2026-06": Decimal("90"), "2026-10": Decimal("100")}
        realised = _realised(buy, sell, regime=FOREIGN_REGIME, fx=fx)
        h = _holding(
            [_lot(1, "10", "100", "2022-01-01T00:00:00+00:00")],
            regime=FOREIGN_REGIME.key,
            currency="USD",
            symbol="AAPL",
        )
        result = simulate_sale(
            h,
            qty=Decimal("10"),
            sale_date=datetime(2026, 10, 16, tzinfo=UTC),
            sale_price=Decimal("200"),
            profile=_profile(fx_overrides=fx),
            realised_lots=realised,
        )
        assert result.status == "success"
        assert result.ltcg_exemption_remaining_inr is None
        # +$1,000 x ₹100 = ₹1,00,000 LTCG at 12.5% Sec 112, no exemption.
        assert result.gain_inr == Decimal("100000.00")
        assert result.tax_inr == Decimal("13000.00")
        assert result.fy_realised_lots == 1

    def test_missing_fx_for_the_sale_still_blocks_the_figure_with_realised_lots(self):
        h = _holding(
            [_lot(1, "10", "100", "2022-01-01T00:00:00+00:00")],
            regime=FOREIGN_REGIME.key,
            currency="USD",
            symbol="AAPL",
        )
        result = simulate_sale(
            h,
            qty=Decimal("10"),
            sale_date=datetime(2026, 10, 16, tzinfo=UTC),
            sale_price=Decimal("200"),
            profile=_profile(),
            realised_lots=self._realised_ltcg("120"),
        )
        assert result.status == "missing_fx_rate"
        assert result.tax_inr is None and result.tax_after_inr is None
        assert result.fx_rate is None


class TestCalendarDaySemantics:
    def test_naive_sale_date_is_rejected(self):
        h = _holding([_lot(*LT_LOT)])
        with pytest.raises(ValueError, match="timezone-aware"):
            simulate_sale(
                h,
                qty=Decimal("1"),
                sale_date=datetime(2026, 10, 16),
                sale_price=Decimal("1"),
                profile=_profile(),
            )

    def test_naive_lot_datetime_is_rejected(self):
        h = _holding([LotSnapshot(1, Decimal("1"), Decimal("1"), datetime(2024, 1, 1))])
        with pytest.raises(ValueError, match="timezone-aware"):
            list_tax_lots(h, as_of=datetime(2026, 10, 16, tzinfo=UTC))

    @pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-1")])
    def test_non_finite_or_negative_inputs_rejected(self, bad):
        h = _holding([_lot(*LT_LOT)])
        kwargs = {
            "qty": Decimal("1"),
            "sale_date": datetime(2026, 10, 16, tzinfo=UTC),
            "sale_price": Decimal("1"),
            "profile": _profile(),
        }
        with pytest.raises(ValueError):
            simulate_sale(h, **{**kwargs, "qty": bad})
        with pytest.raises(ValueError):
            simulate_sale(h, **{**kwargs, "sale_price": bad})
        with pytest.raises(ValueError):
            simulate_sale(h, sell_fees=bad, **kwargs)

    def test_local_day_decides_the_financial_year(self):
        """00:30 IST on 1 April is still 31 March in UTC; the FY is the local one."""
        h = _holding([_lot(*LT_LOT)])
        result = simulate_sale(
            h,
            qty=Decimal("1"),
            sale_date=datetime(2027, 4, 1, 0, 30, tzinfo=IST),
            sale_price=Decimal("150"),
            profile=_profile(),
        )
        assert result.fy_label == "2027-28"
        utc_equivalent = simulate_sale(
            h,
            qty=Decimal("1"),
            sale_date=datetime(2027, 3, 31, 19, 0, tzinfo=UTC),
            sale_price=Decimal("150"),
            profile=_profile(),
        )
        assert utc_equivalent.fy_label == "2026-27"

    def test_holding_period_boundary_uses_local_calendar_days(self):
        # Acquired 00:30 IST on 16 Oct 2025 (= 15 Oct 19:00 UTC).
        h = _holding([
            LotSnapshot(1, Decimal("10"), Decimal("100"), datetime(2025, 10, 16, 0, 30, tzinfo=IST))
        ])
        on_anniversary = list_tax_lots(h, as_of=datetime(2026, 10, 16, 23, 0, tzinfo=IST))[0]
        assert on_anniversary.is_long_term is False
        assert on_anniversary.days_to_long_term == 1
        day_after = list_tax_lots(h, as_of=datetime(2026, 10, 17, 0, 5, tzinfo=IST))[0]
        assert day_after.is_long_term is True
        assert day_after.days_to_long_term == 0

    def test_countdown_never_zero_while_short_term(self):
        h = _holding([_lot(*LT_LOT)])
        for offset in range(-3, 3):
            as_of = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=offset)
            view = list_tax_lots(h, as_of=as_of)[0]
            assert (view.days_to_long_term == 0) == view.is_long_term


class TestLotSelection:
    def test_lots_acquired_after_the_sale_date_are_excluded(self):
        h = _holding([
            _lot(1, "10", "100", "2024-01-01T00:00:00+00:00"),
            _lot(2, "10", "100", "2026-12-01T00:00:00+00:00"),
        ])
        result = _sell_indian(h, "20", "150", when="2026-10-16T00:00:00+00:00")
        assert result.status == "insufficient_quantity"
        assert result.quantity_consumed == Decimal("10")
        assert [c.lot_id for c in result.consumed_lots] == [1]
        assert any("acquired after the sale date" in a for a in result.assumptions)

    def test_no_lot_open_on_sale_date_has_no_tax_figure(self):
        h = _holding([_lot(1, "10", "100", "2026-12-01T00:00:00+00:00")])
        result = _sell_indian(h, "5", "150")
        assert result.status == "insufficient_quantity"
        assert result.quantity_consumed == Decimal("0")
        assert result.tax_inr is None
        assert "none are open" in (result.message or "")

    def test_equal_timestamps_consume_by_numeric_lot_id(self):
        h = _holding([
            _lot(10, "5", "100", "2024-01-01T00:00:00+00:00"),
            _lot(2, "5", "100", "2024-01-01T00:00:00+00:00"),
        ])
        result = _sell_indian(h, "6", "150")
        assert [c.lot_id for c in result.consumed_lots] == [2, 10]

    def test_zero_remaining_lots_are_ignored(self):
        h = _holding([
            _lot(1, "0", "100", "2024-01-01T00:00:00+00:00"),
            _lot(2, "5", "100", "2024-01-02T00:00:00+00:00"),
        ])
        assert [v.lot_id for v in list_tax_lots(h, as_of=datetime(2026, 1, 1, tzinfo=UTC))] == [2]
        assert (
            harvest_candidates([h], {1: Decimal("50")}, as_of=datetime(2026, 1, 1, tzinfo=UTC))[
                0
            ].lot_id
            == 2
        )

    def test_fractional_quantities_stay_exact(self):
        h = _holding([
            _lot(1, "0.00000003", "100", "2024-01-01T00:00:00+00:00"),
            _lot(2, "1", "100", "2024-02-01T00:00:00+00:00"),
        ])
        result = _sell_indian(h, "0.00000004", "200")
        assert result.status == "success"
        assert [c.quantity for c in result.consumed_lots] == [
            Decimal("0.00000003"),
            Decimal("0.00000001"),
        ]
        assert result.quantity_consumed == Decimal("0.00000004")
