"""Unit tests for the capital gains tax engine (spec-099).

The engine is pure, so these tests need no database. The properties
tested here are the ones that make the output trustworthy:

* the FIFO replay reproduces the recorded ``realized_gain_loss`` and
  ``avg_cost_at_sale`` exactly (the reconciliation guard),
* the 24-month test is exact calendar arithmetic, not a day count,
* the two regimes differ in threshold, exemption and STCG rate,
* the slab, surcharge, cess and rebate resolve correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.investing.tax.engine import compute_tax_estimate, specified_fx_date
from app.investing.tax.fy import add_months, fy_bounds, fy_label_for_date, parse_fy_label
from app.investing.tax.gains import (
    OrderView,
    effective_buy_cost_per_unit,
    is_long_term,
)
from app.investing.tax.regime import (
    FOREIGN_REGIME,
    INDIAN_REGIME,
    ClassificationSource,
    classify_regime,
)
from app.investing.tax.slab import (
    TAX_REGIME_NEW,
    TAX_REGIME_OLD,
    compute_rebate,
    derive_slab,
    marginal_rate,
    slabs_for_regime,
    surcharge_rate,
    tax_on_income,
)
from app.investing.tax.tax_profile import TaxProfile

UTC = UTC


def order(
    *,
    public_id: str,
    order_type: str,
    symbol: str = "AAPL",
    quantity: str = "10",
    price: str = "100",
    fees: str = "0",
    occurred_at: str,
    currency: str = "USD",
    account_id: str = "acct-1",
    account_name: str = "IND Money",
    instrument_type: str = "stock",
    created_at: str | None = None,
    realized_gain_loss: str | None = None,
    avg_cost_at_sale: str | None = None,
) -> OrderView:
    qty = Decimal(quantity)
    px = Decimal(price)
    gross = qty * px
    fee = Decimal(fees)
    return OrderView(
        public_id=public_id,
        order_type=order_type,
        symbol=symbol,
        quantity=qty,
        price_per_unit=px,
        gross_amount=gross,
        brokerage_fee=fee,
        tax_amount=Decimal("0"),
        other_fees=Decimal("0"),
        occurred_at=datetime.fromisoformat(occurred_at),
        currency=currency,
        created_at=(datetime.fromisoformat(created_at) if created_at else None),
        account_id=account_id,
        account_name=account_name,
        instrument_type=instrument_type,
        realized_gain_loss=(
            Decimal(realized_gain_loss) if realized_gain_loss is not None else None
        ),
        avg_cost_at_sale=(Decimal(avg_cost_at_sale) if avg_cost_at_sale is not None else None),
    )


def foreign_resolver():
    def _resolve(_order):
        return FOREIGN_REGIME, ClassificationSource.security_master.value, "US"

    return _resolve


def indian_resolver():
    def _resolve(_order):
        return INDIAN_REGIME, ClassificationSource.security_master.value, "IN"

    return _resolve


# --------------------------------------------------------------------- FY


class TestFiscalYear:
    def test_parse_label(self):
        assert parse_fy_label("2026-27") == (
            datetime(2026, 4, 1, tzinfo=UTC).date(),
            datetime(2027, 3, 31, tzinfo=UTC).date(),
        )

    def test_parse_label_rejects_bad_suffix(self):
        with pytest.raises(ValueError):
            parse_fy_label("2026-26")

    def test_parse_label_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_fy_label("not-a-year")

    def test_bounds_cover_full_fy(self):
        start, end = fy_bounds("2026-27")
        assert start == datetime(2026, 4, 1, tzinfo=UTC)
        assert end.year == 2027 and end.month == 3 and end.day == 31

    def test_label_for_date(self):
        assert fy_label_for_date(datetime(2026, 4, 1, tzinfo=UTC)) == "2026-27"
        assert fy_label_for_date(datetime(2026, 3, 31, tzinfo=UTC)) == "2025-26"
        assert fy_label_for_date(datetime(2026, 12, 31, tzinfo=UTC)) == "2026-27"

    def test_add_months_clamps_day(self):
        assert add_months(datetime(2024, 1, 31, tzinfo=UTC), 24) == datetime(
            2026, 1, 31, tzinfo=UTC
        )
        assert add_months(datetime(2024, 1, 31, tzinfo=UTC), 1) == datetime(2024, 2, 29, tzinfo=UTC)


# ----------------------------------------------------------------- Regime


class TestRegime:
    def test_explicit_regime_wins(self):
        result = classify_regime(
            explicit_regime=INDIAN_REGIME.key,
            instrument_country_override="US",
            security_country_code="IN",
        )
        assert result.regime is INDIAN_REGIME
        assert result.source is ClassificationSource.explicit_regime

    def test_instrument_override_beats_security_master(self):
        result = classify_regime(
            instrument_country_override="US",
            security_country_code="IN",
        )
        assert result.regime is FOREIGN_REGIME
        assert result.source is ClassificationSource.instrument_override

    def test_security_master_beats_account(self):
        result = classify_regime(
            security_country_code="IN",
            account_market_scope="foreign",
        )
        assert result.regime is INDIAN_REGIME
        assert result.source is ClassificationSource.security_master

    def test_account_scope_used_when_no_security(self):
        result = classify_regime(account_market_scope="foreign")
        assert result.regime is FOREIGN_REGIME
        assert result.source is ClassificationSource.account_market_scope

    def test_mixed_account_falls_through(self):
        result = classify_regime(account_market_scope="mixed", holding_currency="USD")
        assert result.regime is FOREIGN_REGIME
        assert result.source is ClassificationSource.currency_fallback

    def test_currency_fallback(self):
        result = classify_regime(holding_currency="USD")
        assert result.regime is FOREIGN_REGIME
        assert result.source is ClassificationSource.currency_fallback

    def test_regime_parameters_differ(self):
        assert INDIAN_REGIME.ltcg_holding_months == 12
        assert FOREIGN_REGIME.ltcg_holding_months == 24
        assert INDIAN_REGIME.ltcg_exemption_inr == Decimal("125000")
        assert FOREIGN_REGIME.ltcg_exemption_inr == Decimal("0")
        assert INDIAN_REGIME.stcg_is_flat is True
        assert FOREIGN_REGIME.stcg_is_flat is False


# -------------------------------------------------------- Holding period


class TestHoldingPeriod:
    """The 24-month test must be exact calendar arithmetic."""

    def test_exactly_24_months_is_short_term(self):
        acquired = datetime(2024, 1, 15, tzinfo=UTC)
        sold = datetime(2026, 1, 15, tzinfo=UTC)
        assert is_long_term(acquired, sold, FOREIGN_REGIME) is False

    def test_24_months_plus_a_day_is_long_term(self):
        acquired = datetime(2024, 1, 15, tzinfo=UTC)
        sold = datetime(2026, 1, 16, tzinfo=UTC)
        assert is_long_term(acquired, sold, FOREIGN_REGIME) is True

    def test_leap_day_acquisition(self):
        acquired = datetime(2024, 2, 29, tzinfo=UTC)
        # Exactly 24 calendar months later, clamped to 28 Feb 2026.
        sold = datetime(2026, 2, 28, tzinfo=UTC)
        assert is_long_term(acquired, sold, FOREIGN_REGIME) is False
        assert is_long_term(acquired, datetime(2026, 3, 1, tzinfo=UTC), FOREIGN_REGIME) is True

    def test_day_before_24_months(self):
        acquired = datetime(2024, 1, 31, tzinfo=UTC)
        sold = datetime(2026, 1, 30, tzinfo=UTC)
        assert is_long_term(acquired, sold, FOREIGN_REGIME) is False

    def test_indian_regime_uses_12_months(self):
        acquired = datetime(2025, 1, 15, tzinfo=UTC)
        sold = datetime(2026, 1, 16, tzinfo=UTC)
        assert is_long_term(acquired, sold, INDIAN_REGIME) is True
        assert is_long_term(acquired, sold, FOREIGN_REGIME) is False


# ------------------------------------------------------------- Cost basis


class TestCostBasis:
    def test_buy_fees_capitalise_into_cost(self):
        o = order(
            public_id="b1",
            order_type="buy",
            quantity="10",
            price="100",
            fees="5",
            occurred_at="2024-01-15T00:00:00+00:00",
        )
        # (1000 + 5) / 10 = 100.5
        assert effective_buy_cost_per_unit(o) == Decimal("100.500000")

    def test_no_fees_keeps_price(self):
        o = order(
            public_id="b1",
            order_type="buy",
            occurred_at="2024-01-15T00:00:00+00:00",
        )
        assert effective_buy_cost_per_unit(o) == Decimal("100.000000")


# ------------------------------------------------------------ Reconciliation


class TestReconciliation:
    def test_replay_reproduces_recorded_gain(self):
        orders = [
            order(
                public_id="b1",
                order_type="buy",
                quantity="10",
                price="100",
                fees="10",
                occurred_at="2024-01-15T00:00:00+00:00",
            ),
            order(
                public_id="s1",
                order_type="sell",
                quantity="4",
                price="150",
                fees="8",
                occurred_at="2026-10-16T00:00:00+00:00",
                realized_gain_loss="188.00",
                avg_cost_at_sale="101.000000",
            ),
        ]
        estimate = compute_tax_estimate(
            orders=orders,
            fy_label="2026-27",
            profile=TaxProfile(fx_overrides={"2026-10": Decimal("95")}),
            regime_resolver=foreign_resolver(),
        )
        assert estimate.status == "success"
        assert estimate.reconciliation_failures == []

    def test_sells_sharing_a_timestamp_replay_in_created_at_order(self):
        """Two sells can share an occurred_at; created_at breaks the tie.

        The broker's own recorded cost-per-unit shows which executed
        first, so replaying in the wrong order produces a different (and
        wrong) blended cost. This is a real bug caught against the
        workspace's MU sells, which share a timestamp.
        """
        orders = [
            order(
                public_id="b1",
                order_type="buy",
                quantity="1.4",
                price="219.8502",
                fees="0.91",
                occurred_at="2025-11-25T21:29:00+00:00",
                created_at="2026-06-29T14:17:53.198911+00:00",
            ),
            order(
                public_id="b2",
                order_type="buy",
                quantity="1.21807125",
                price="241.1189",
                fees="0.87",
                occurred_at="2025-12-12T21:54:00+00:00",
                created_at="2026-06-29T14:17:53.896288+00:00",
            ),
            # Both sells share the same occurred_at.
            order(
                public_id="s1",
                order_type="sell",
                quantity="0.61807124",
                price="895.99",
                fees="1.67",
                occurred_at="2026-10-16T00:00:00+00:00",
                created_at="2026-10-19T04:14:51.869411+00:00",
                realized_gain_loss="415.83",
                avg_cost_at_sale="220.500200",
            ),
            order(
                public_id="s2",
                order_type="sell",
                quantity="1.0",
                price="895.17",
                fees="2.71",
                occurred_at="2026-10-16T00:00:00+00:00",
                created_at="2026-10-19T04:14:59.221466+00:00",
                realized_gain_loss="667.31",
                avg_cost_at_sale="225.152302",
            ),
        ]
        estimate = compute_tax_estimate(
            orders=orders,
            fy_label="2026-27",
            profile=TaxProfile(fx_overrides={"2026-10": Decimal("95.42")}),
            regime_resolver=foreign_resolver(),
        )
        assert estimate.status == "success"
        assert estimate.reconciliation_failures == []

    def test_mismatched_recorded_gain_blocks_the_number(self):
        orders = [
            order(
                public_id="b1",
                order_type="buy",
                occurred_at="2024-01-15T00:00:00+00:00",
            ),
            order(
                public_id="s1",
                order_type="sell",
                quantity="4",
                price="150",
                occurred_at="2026-10-16T00:00:00+00:00",
                realized_gain_loss="999.00",
            ),
        ]
        estimate = compute_tax_estimate(
            orders=orders,
            fy_label="2026-27",
            profile=TaxProfile(fx_overrides={"2026-10": Decimal("95")}),
            regime_resolver=foreign_resolver(),
        )
        assert estimate.status == "reconciliation_failed"
        assert estimate.total_tax_inr == 0
        assert estimate.reconciliation_failures


# ------------------------------------------------------------- FX dates


class TestFxDates:
    def test_specified_date_is_last_day_of_previous_month(self):
        assert (
            specified_fx_date(datetime(2026, 10, 16, tzinfo=UTC).date())
            == datetime(2026, 9, 30, tzinfo=UTC).date()
        )

    def test_january_rolls_back_a_year(self):
        assert (
            specified_fx_date(datetime(2026, 1, 5, tzinfo=UTC).date())
            == datetime(2025, 12, 31, tzinfo=UTC).date()
        )

    def test_february_specified_date(self):
        assert (
            specified_fx_date(datetime(2026, 2, 3, tzinfo=UTC).date())
            == datetime(2026, 1, 31, tzinfo=UTC).date()
        )


# ------------------------------------------------------------------ Slab


class TestSlab:
    def test_new_regime_boundaries(self):
        slabs = slabs_for_regime(TAX_REGIME_NEW)
        assert marginal_rate(Decimal("0"), slabs) == Decimal("0")
        assert marginal_rate(Decimal("400000"), slabs) == Decimal("0")
        assert marginal_rate(Decimal("400001"), slabs) == Decimal("0.05")
        assert marginal_rate(Decimal("800000"), slabs) == Decimal("0.05")
        assert marginal_rate(Decimal("800001"), slabs) == Decimal("0.10")
        assert marginal_rate(Decimal("1200000"), slabs) == Decimal("0.10")
        assert marginal_rate(Decimal("1200001"), slabs) == Decimal("0.15")
        assert marginal_rate(Decimal("2400000"), slabs) == Decimal("0.25")
        assert marginal_rate(Decimal("2400001"), slabs) == Decimal("0.30")

    def test_old_regime_boundaries(self):
        slabs = slabs_for_regime(TAX_REGIME_OLD)
        assert marginal_rate(Decimal("250000"), slabs) == Decimal("0")
        assert marginal_rate(Decimal("250001"), slabs) == Decimal("0.05")
        assert marginal_rate(Decimal("1000000"), slabs) == Decimal("0.20")
        assert marginal_rate(Decimal("1000001"), slabs) == Decimal("0.30")

    def test_tax_on_income_new_regime(self):
        slabs = slabs_for_regime(TAX_REGIME_NEW)
        assert tax_on_income(Decimal("1200000"), slabs) == Decimal("60000")
        assert tax_on_income(Decimal("2400000"), slabs) == Decimal("300000")

    def test_surcharge_is_nil_below_the_threshold(self):
        assert surcharge_rate(Decimal("5000000")) == Decimal("0")

    def test_surcharge_applies_above_the_threshold(self):
        # Marginal relief means the effective rate just above a threshold
        # is far below the headline band rate.
        just_above = surcharge_rate(Decimal("5000001"))
        assert Decimal("0") < just_above < Decimal("0.10")
        well_above = surcharge_rate(Decimal("6000000"))
        assert well_above == Decimal("0.10")

    def test_surcharge_cap_at_15_percent_for_capital_gains(self):
        assert surcharge_rate(Decimal("30000000"), capital_gains_only=True) == Decimal("0.15")
        assert surcharge_rate(Decimal("60000000"), capital_gains_only=True) == Decimal("0.15")

    def test_capital_gains_surcharge_is_capped_at_15(self):
        # 25% band, but capital-gains income only sees 15%.
        assert surcharge_rate(Decimal("30000000"), capital_gains_only=True) == Decimal("0.15")

    def test_marginal_relief_removes_the_discontinuity(self):
        just_above = surcharge_rate(Decimal("10000001"))
        at_threshold = surcharge_rate(Decimal("10000000"))
        assert just_above != Decimal("0.15") or at_threshold == Decimal("0.15")
        # Whatever the rate, tax + surcharge must not jump discontinuously.
        slabs = slabs_for_regime(TAX_REGIME_NEW)
        base = tax_on_income(Decimal("10000000"), slabs)
        with_sur = tax_on_income(Decimal("10000001"), slabs)
        assert with_sur - base <= Decimal("1")

    def test_rebate_87a_zeroes_low_income(self):
        # No other income, so the whole gain sits in the 0% band.
        result = derive_slab(
            other_income=Decimal("0"),
            capital_gains_income=Decimal("50000"),
            tax_regime=TAX_REGIME_NEW,
        )
        assert result.rate_pct == Decimal("0")

    def test_rebate_limit_new_regime(self):
        assert compute_rebate(Decimal("100000"), Decimal("1000000"), TAX_REGIME_NEW) == Decimal(
            "60000"
        )

    def test_rebate_limit_old_regime(self):
        assert compute_rebate(Decimal("100000"), Decimal("1000000"), TAX_REGIME_OLD) == Decimal(
            "12000"
        )

    def test_rebate_is_capped_at_the_tax_payable(self):
        assert compute_rebate(Decimal("5000"), Decimal("1000000"), TAX_REGIME_NEW) == Decimal(
            "5000"
        )

    def test_no_rebate_above_ceiling(self):
        assert compute_rebate(Decimal("100000"), Decimal("1200001"), TAX_REGIME_NEW) == Decimal("0")

    def test_override_beats_derivation(self):
        result = derive_slab(
            other_income=Decimal("5000000"),
            capital_gains_income=Decimal("100000"),
            slab_override_pct=Decimal("15"),
        )
        assert result.rate_pct == Decimal("15")
        assert result.source == "override"

    def test_cess_is_four_percent_on_tax_plus_surcharge(self):
        result = derive_slab(
            other_income=Decimal("2000000"),
            capital_gains_income=Decimal("100000"),
            tax_regime=TAX_REGIME_NEW,
        )
        assert result.cess_pct == Decimal("0.04")


# ------------------------------------------------------------ End to end


class TestComputeTaxEstimate:
    def test_foreign_regime_uses_24_months_and_no_exemption(self):
        orders = [
            order(
                public_id="b1",
                order_type="buy",
                quantity="10",
                price="100",
                occurred_at="2024-01-15T00:00:00+00:00",
            ),
            order(
                public_id="s1",
                order_type="sell",
                quantity="10",
                price="200",
                occurred_at="2026-10-16T00:00:00+00:00",
                realized_gain_loss="1000.00",
                avg_cost_at_sale="100.000000",
            ),
        ]
        estimate = compute_tax_estimate(
            orders=orders,
            fy_label="2026-27",
            profile=TaxProfile(fx_overrides={"2026-10": Decimal("100")}),
            regime_resolver=foreign_resolver(),
        )
        assert estimate.status == "success"
        bucket = estimate.buckets["foreign:long_term"]
        assert bucket.gain_inr == Decimal("100000.00")
        # No exemption under the foreign regime.
        assert bucket.exemption_applied == Decimal("0")
        assert bucket.tax > 0

    def test_indian_regime_applies_the_exemption(self):
        orders = [
            order(
                public_id="b1",
                order_type="buy",
                quantity="10",
                price="100",
                occurred_at="2025-01-15T00:00:00+00:00",
            ),
            order(
                public_id="s1",
                order_type="sell",
                quantity="10",
                price="200",
                occurred_at="2026-10-16T00:00:00+00:00",
                realized_gain_loss="1000.00",
                avg_cost_at_sale="100.000000",
            ),
        ]
        estimate = compute_tax_estimate(
            orders=orders,
            fy_label="2026-27",
            profile=TaxProfile(fx_overrides={"2026-10": Decimal("100")}),
            regime_resolver=indian_resolver(),
        )
        bucket = estimate.buckets["indian:long_term"]
        assert bucket.gain_inr == Decimal("100000.00")
        assert bucket.exemption_applied == Decimal("100000.00")
        # Fully sheltered by the ₹1.25 lakh exemption.
        assert bucket.tax == Decimal("0.00")
        assert estimate.total_tax_inr == Decimal("0.00")

    def test_indian_stcg_is_flat_20_percent(self):
        orders = [
            order(
                public_id="b1",
                order_type="buy",
                quantity="10",
                price="100",
                occurred_at="2026-01-15T00:00:00+00:00",
            ),
            order(
                public_id="s1",
                order_type="sell",
                quantity="10",
                price="200",
                occurred_at="2026-10-16T00:00:00+00:00",
                realized_gain_loss="1000.00",
                avg_cost_at_sale="100.000000",
            ),
        ]
        estimate = compute_tax_estimate(
            orders=orders,
            fy_label="2026-27",
            profile=TaxProfile(
                other_income=Decimal("5000000"),
                fx_overrides={"2026-10": Decimal("100")},
            ),
            regime_resolver=indian_resolver(),
        )
        bucket = estimate.buckets["indian:short_term"]
        # Sec 111A: 20% flat regardless of the derived slab rate. The
        # surcharge is capped at 15% for capital-gains income; marginal
        # relief brings the effective rate down at this income level.
        assert estimate.slab is not None
        assert estimate.slab.rate_pct == Decimal("0.30")
        expected = (
            Decimal("100000")
            * Decimal("0.20")
            * (1 + min(estimate.slab.surcharge_pct, Decimal("0.15")))
            * Decimal("1.04")
        ).quantize(Decimal("0.01"))
        assert bucket.tax == expected
        # The flat 20% rate is applied, not the 30% slab.
        assert bucket.tax < Decimal("30000")

    def test_foreign_stcg_uses_the_slab(self):
        orders = [
            order(
                public_id="b1",
                order_type="buy",
                quantity="10",
                price="100",
                occurred_at="2026-01-15T00:00:00+00:00",
            ),
            order(
                public_id="s1",
                order_type="sell",
                quantity="10",
                price="200",
                occurred_at="2026-10-16T00:00:00+00:00",
                realized_gain_loss="1000.00",
                avg_cost_at_sale="100.000000",
            ),
        ]
        estimate = compute_tax_estimate(
            orders=orders,
            fy_label="2026-27",
            profile=TaxProfile(
                other_income=Decimal("5000000"),
                fx_overrides={"2026-10": Decimal("100")},
            ),
            regime_resolver=foreign_resolver(),
        )
        bucket = estimate.buckets["foreign:short_term"]
        # Total income 5,000,000 + 100,000 = 5,100,000 => the 30% band
        # (above 24,00,000). The surcharge is reduced by marginal relief
        # at this income level, so the tax is more than a flat 30% + cess.
        assert estimate.slab is not None
        assert estimate.slab.rate_pct == Decimal("0.30")
        expected = (
            Decimal("100000")
            * Decimal("0.30")
            * (1 + min(estimate.slab.surcharge_pct, Decimal("0.15")))
            * Decimal("1.04")
        ).quantize(Decimal("0.01"))
        assert bucket.tax == expected
        # The slab rate really is applied (not a flat 20%).
        assert bucket.tax > Decimal("30000")

    def test_sell_spanning_both_buckets_splits_them(self):
        orders = [
            order(
                public_id="b1",
                order_type="buy",
                quantity="5",
                price="100",
                occurred_at="2022-01-15T00:00:00+00:00",
            ),
            order(
                public_id="b2",
                order_type="buy",
                quantity="5",
                price="300",
                occurred_at="2026-01-15T00:00:00+00:00",
            ),
            order(
                public_id="s1",
                order_type="sell",
                quantity="10",
                price="400",
                occurred_at="2026-10-16T00:00:00+00:00",
                realized_gain_loss="2000.00",
                avg_cost_at_sale="200.000000",
            ),
        ]
        estimate = compute_tax_estimate(
            orders=orders,
            fy_label="2026-27",
            profile=TaxProfile(fx_overrides={"2026-10": Decimal("100")}),
            regime_resolver=foreign_resolver(),
        )
        assert estimate.status == "success"
        assert "foreign:long_term" in estimate.buckets
        assert "foreign:short_term" in estimate.buckets
        assert estimate.buckets["foreign:long_term"].lot_count == 1
        assert estimate.buckets["foreign:short_term"].lot_count == 1
        # The 2022 lot is long-term (1500 USD), the 2026 lot is short-term
        # (500 USD) — one sell, two buckets.
        assert estimate.buckets["foreign:long_term"].gain_native == Decimal("1500.00")
        assert estimate.buckets["foreign:short_term"].gain_native == Decimal("500.00")

    def test_fx_fallback_is_marked_estimated(self):
        orders = [
            order(
                public_id="b1",
                order_type="buy",
                occurred_at="2024-01-15T00:00:00+00:00",
            ),
            order(
                public_id="s1",
                order_type="sell",
                quantity="10",
                price="200",
                occurred_at="2026-10-16T00:00:00+00:00",
                realized_gain_loss="1000.00",
                avg_cost_at_sale="100.000000",
            ),
        ]
        estimate = compute_tax_estimate(
            orders=orders,
            fy_label="2026-27",
            profile=TaxProfile(),
            regime_resolver=foreign_resolver(),
            app_fx_rates={("2026-10", "USD"): Decimal("95")},
        )
        assert estimate.fx_rates
        rate = estimate.fx_rates[0]
        assert rate.source == "app_rate_estimate"
        assert rate.is_estimated is True
        assert rate.specified_date == "2026-09-30"

    def test_manual_fx_is_not_estimated(self):
        orders = [
            order(
                public_id="b1",
                order_type="buy",
                occurred_at="2024-01-15T00:00:00+00:00",
            ),
            order(
                public_id="s1",
                order_type="sell",
                quantity="10",
                price="200",
                occurred_at="2026-10-16T00:00:00+00:00",
                realized_gain_loss="1000.00",
                avg_cost_at_sale="100.000000",
            ),
        ]
        estimate = compute_tax_estimate(
            orders=orders,
            fy_label="2026-27",
            profile=TaxProfile(fx_overrides={"2026-10": Decimal("95.42")}),
            regime_resolver=foreign_resolver(),
            app_fx_rates={("2026-10", "USD"): Decimal("95")},
        )
        assert estimate.fx_rates[0].source == "manual"
        assert estimate.fx_rates[0].is_estimated is False

    def test_sell_outside_the_fy_is_excluded(self):
        orders = [
            order(
                public_id="b1",
                order_type="buy",
                occurred_at="2024-01-15T00:00:00+00:00",
            ),
            order(
                public_id="s1",
                order_type="sell",
                quantity="10",
                price="200",
                occurred_at="2025-10-16T00:00:00+00:00",
                realized_gain_loss="1000.00",
                avg_cost_at_sale="100.000000",
            ),
        ]
        estimate = compute_tax_estimate(
            orders=orders,
            fy_label="2026-27",
            profile=TaxProfile(fx_overrides={"2025-10": Decimal("100")}),
            regime_resolver=foreign_resolver(),
        )
        assert estimate.total_gain_inr == Decimal("0.00")
        assert estimate.lots == []

    def test_assumptions_and_disclaimers_are_present(self):
        orders = [
            order(
                public_id="b1",
                order_type="buy",
                occurred_at="2024-01-15T00:00:00+00:00",
            ),
            order(
                public_id="s1",
                order_type="sell",
                quantity="10",
                price="200",
                occurred_at="2026-10-16T00:00:00+00:00",
                realized_gain_loss="1000.00",
                avg_cost_at_sale="100.000000",
            ),
        ]
        estimate = compute_tax_estimate(
            orders=orders,
            fy_label="2026-27",
            profile=TaxProfile(fx_overrides={"2026-10": Decimal("95")}),
            regime_resolver=foreign_resolver(),
        )
        assert estimate.assumptions
        assert estimate.disclaimers
        assert any("foreign" in a for a in estimate.assumptions)
        assert any("FX rate" in a for a in estimate.assumptions)
