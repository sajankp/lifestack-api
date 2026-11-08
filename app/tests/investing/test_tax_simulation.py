"""Unit tests for the read-only tax what-if simulations.

These tests need no database: they build ``OrderView`` fixtures (mirroring
``app/tests/investing/test_tax_engine.py``), run ``simulate_sale`` (which
prices through ``compute_tax_estimate``), and cross-check the result
against a direct ``compute_tax_estimate`` call on the same orders.

The functions under test are pure and touch no DB, so they run regardless
of the pre-existing ``app.investing.router`` import blocker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.investing.tax.engine import compute_tax_estimate
from app.investing.tax.fy import add_months
from app.investing.tax.gains import OrderView
from app.investing.tax.regime import (
    FOREIGN_REGIME,
    INDIAN_REGIME,
    RegimeType,
)
from app.investing.tax.simulation import (
    HoldingSnapshot,
    LotSnapshot,
    harvest_candidates,
    list_tax_lots,
    simulate_sale,
)
from app.investing.tax.tax_profile import TaxProfile

UTC = UTC


def lot(
    lot_id: int,
    quantity: str,
    cost: str,
    acquired_at: str,
) -> LotSnapshot:
    return LotSnapshot(
        lot_id=lot_id,
        quantity=Decimal(quantity),
        cost_per_unit=Decimal(cost),
        acquired_at=datetime.fromisoformat(acquired_at),
    )


def holding(
    holding_id: int = 1,
    symbol: str = "AAPL",
    currency: str = "USD",
    regime: RegimeType = FOREIGN_REGIME.key,
    lots: tuple[LotSnapshot, ...] = (),
) -> HoldingSnapshot:
    return HoldingSnapshot(
        holding_id=holding_id,
        symbol=symbol,
        account_id="acct-1",
        account_name="IND Money",
        currency=currency,
        regime=regime,
        regime_source="security_master",
        lots=lots,
    )


def profile(
    *,
    fx_overrides: dict[str, Decimal] | None = None,
    other_income: Decimal = Decimal("0"),
) -> TaxProfile:
    return TaxProfile(fx_overrides=fx_overrides or {}, other_income=other_income)


def sell_view(
    public_id: str,
    quantity: str,
    price: str,
    occurred_at: str,
    fees: str = "0",
    symbol: str = "AAPL",
    currency: str = "USD",
) -> OrderView:
    qty = Decimal(quantity)
    px = Decimal(price)
    fee = Decimal(fees)
    return OrderView(
        public_id=public_id,
        order_type="sell",
        symbol=symbol,
        quantity=qty,
        price_per_unit=px,
        gross_amount=(qty * px).quantize(Decimal("0.01")),
        brokerage_fee=fee,
        tax_amount=Decimal("0"),
        other_fees=Decimal("0"),
        occurred_at=datetime.fromisoformat(occurred_at),
        currency=currency,
        account_id="acct-1",
        account_name="IND Money",
        instrument_type="stock",
        realized_gain_loss=None,
        avg_cost_at_sale=None,
    )


def buy_view(
    public_id: str,
    quantity: str,
    price: str,
    occurred_at: str,
    symbol: str = "AAPL",
    currency: str = "USD",
) -> OrderView:
    qty = Decimal(quantity)
    px = Decimal(price)
    return OrderView(
        public_id=public_id,
        order_type="buy",
        symbol=symbol,
        quantity=qty,
        price_per_unit=px,
        gross_amount=(qty * px).quantize(Decimal("0.01")),
        brokerage_fee=Decimal("0"),
        tax_amount=Decimal("0"),
        other_fees=Decimal("0"),
        occurred_at=datetime.fromisoformat(occurred_at),
        currency=currency,
        account_id="acct-1",
        account_name="IND Money",
        instrument_type="stock",
    )


class TestSimulateSale:
    def test_foreign_long_term_sale(self):
        h = holding(lots=(lot(1, "10", "100", "2024-01-15T00:00:00+00:00"),))
        result = simulate_sale(
            h,
            qty=Decimal("10"),
            sale_date=datetime(2026, 10, 16, tzinfo=UTC),
            sale_price=Decimal("200"),
            profile=profile(fx_overrides={"2026-10": Decimal("100")}),
        )
        assert result.status == "success"
        assert result.quantity_consumed == Decimal("10")
        assert result.gain_native == Decimal("1000.00")
        assert result.ltcg_gain_native == Decimal("1000.00")
        assert result.stcg_gain_native == Decimal("0.00")
        assert result.gain_inr == Decimal("100000.00")
        assert result.fx_rate == Decimal("100")
        assert result.fx_source == "manual"
        assert result.fy_label == "2026-27"

        [consumed] = result.consumed_lots
        assert consumed.quantity == Decimal("10")
        assert consumed.is_long_term is True
        assert consumed.days_to_long_term == 0
        assert consumed.holding_months == 33
        assert consumed.gain_inr == Decimal("100000.00")
        assert result.assumptions
        assert any("Rule 115 FX rate" in a for a in result.assumptions)

    def test_foreign_short_term_uses_slab_and_counts_down(self):
        h = holding(lots=(lot(1, "10", "100", "2026-01-15T00:00:00+00:00"),))
        mark = add_months(datetime(2026, 1, 15, tzinfo=UTC), 24)
        result = simulate_sale(
            h,
            qty=Decimal("10"),
            sale_date=datetime(2026, 10, 16, tzinfo=UTC),
            sale_price=Decimal("200"),
            profile=profile(
                other_income=Decimal("5000000"),
                fx_overrides={"2026-10": Decimal("100")},
            ),
        )
        assert result.status == "success"
        assert result.stcg_gain_native == Decimal("1000.00")
        assert result.ltcg_gain_native == Decimal("0.00")
        # Foreign STCG is taxed at the marginal slab (30% band at this
        # income): the rebate does not apply above ₹12 lakh income.
        assert result.tax_inr is not None
        assert result.tax_inr > Decimal("30000.00")

        [consumed] = result.consumed_lots
        assert consumed.is_long_term is False
        assert (
            consumed.days_to_long_term
            == (mark.date() - datetime(2026, 10, 16, tzinfo=UTC).date()).days
        )
        assert consumed.days_to_long_term > 0

    def test_partial_sale_consumes_lots_fifo(self):
        h = holding(
            lots=(
                lot(1, "10", "100", "2024-01-15T00:00:00+00:00"),
                lot(2, "10", "300", "2024-02-15T00:00:00+00:00"),
            )
        )
        result = simulate_sale(
            h,
            qty=Decimal("15"),
            sale_date=datetime(2026, 10, 16, tzinfo=UTC),
            sale_price=Decimal("400"),
            profile=profile(fx_overrides={"2026-10": Decimal("100")}),
            sell_fees=Decimal("70"),
        )
        assert result.status == "success"
        assert result.quantity_consumed == Decimal("15")
        assert result.gain_native == Decimal("3500.00")
        assert result.ltcg_gain_native == Decimal("3500.00")

        consumed = result.consumed_lots
        assert [c.quantity for c in consumed] == [Decimal("10"), Decimal("5")]
        assert [c.lot_id for c in consumed] == [1, 2]
        assert [c.gain_native for c in consumed] == [Decimal("3000.00"), Decimal("500.00")]
        # Sell fees spread pro-rata by gross gain: 70 * 3000/3500 = 60, 70 * 500/3500 = 10.
        assert [c.fees_native for c in consumed] == [Decimal("60.00"), Decimal("10.00")]

        # Cross-check against the engine run directly on the same orders.
        estimate = compute_tax_estimate(
            orders=[
                buy_view("b1", "10", "100", "2024-01-15T00:00:00+00:00"),
                buy_view("b2", "10", "300", "2024-02-15T00:00:00+00:00"),
                sell_view("s1", "15", "400", "2026-10-16T00:00:00+00:00", fees="70"),
            ],
            fy_label="2026-27",
            profile=profile(fx_overrides={"2026-10": Decimal("100")}),
            regime_resolver=lambda o: (
                FOREIGN_REGIME,
                "security_master",
                "US",
            ),
        )
        assert estimate.status == "success"
        assert estimate.total_gain_inr == result.gain_inr
        assert estimate.total_tax_inr == result.tax_inr
        engine_gains = sorted(t.gain_native for t in estimate.lots)
        sim_gains = sorted(c.gain_native for c in consumed)
        assert engine_gains == sim_gains
        engine_fees = sorted(t.fees_native for t in estimate.lots)
        sim_fees = sorted(c.fees_native for c in consumed)
        assert engine_fees == sim_fees

    def test_insufficient_quantity_simulates_maximum_sellable(self):
        h = holding(lots=(lot(1, "10", "100", "2024-01-15T00:00:00+00:00"),))
        result = simulate_sale(
            h,
            qty=Decimal("15"),
            sale_date=datetime(2026, 10, 16, tzinfo=UTC),
            sale_price=Decimal("200"),
            profile=profile(fx_overrides={"2026-10": Decimal("100")}),
        )
        assert result.status == "insufficient_quantity"
        assert result.quantity_sold == Decimal("15")
        assert result.quantity_consumed == Decimal("10")
        assert result.gain_native == Decimal("1000.00")
        assert result.message is not None
        assert "10" in result.message

    def test_missing_rule115_rate_blocks_the_tax_figure(self):
        h = holding(lots=(lot(1, "10", "100", "2024-01-15T00:00:00+00:00"),))
        result = simulate_sale(
            h,
            qty=Decimal("10"),
            sale_date=datetime(2026, 10, 16, tzinfo=UTC),
            sale_price=Decimal("200"),
            profile=profile(),  # no FX override, no app rate
        )
        assert result.status == "missing_fx_rate"
        assert result.quantity_consumed == Decimal("10")
        assert result.gain_native == Decimal("1000.00")
        assert result.gain_inr is None
        assert result.tax_inr is None
        assert result.fx_rate is None
        assert result.fx_source == ""
        assert result.message is not None
        assert "No Rule 115 exchange rate" in result.message

    def test_fx_source_chain(self):
        sale_date = datetime(2026, 10, 16, tzinfo=UTC)
        sale_price = Decimal("200")
        lots = (lot(1, "10", "100", "2024-01-15T00:00:00+00:00"),)

        def run(
            *,
            fx_overrides: dict[str, Decimal] | None = None,
            app_fx_rates: dict[tuple[str, str], Decimal] | None = None,
        ):
            return simulate_sale(
                holding(lots=lots),
                qty=Decimal("10"),
                sale_date=sale_date,
                sale_price=sale_price,
                profile=profile(fx_overrides=fx_overrides),
                app_fx_rates=app_fx_rates,
            )

        # Exact Rule 115 date key (last day of September) wins.
        r = run(fx_overrides={"2026-09-30:USD": Decimal("95.42")})
        assert r.status == "success"
        assert r.fx_rate == Decimal("95.42")
        assert r.fx_source == "manual"
        # Legacy month key still works.
        r = run(fx_overrides={"2026-10": Decimal("94.50")})
        assert r.fx_rate == Decimal("94.50")
        assert r.fx_source == "manual"
        # Nearest same-currency daily override within 35 days.
        r = run(fx_overrides={"2026-09-28:USD": Decimal("95.10"), "2026-09-29:GBP": Decimal("120")})
        assert r.fx_rate == Decimal("95.10")
        assert r.fx_source == "manual_nearest:2026-09-28"
        # App-stored estimate when nothing is entered manually.
        r = run(app_fx_rates={("2026-10", "USD"): Decimal("95")})
        assert r.fx_rate == Decimal("95")
        assert r.fx_source == "app_rate_estimate"
        # INR holdings need no conversion.
        r = simulate_sale(
            holding(currency="INR", regime=INDIAN_REGIME.key, lots=lots),
            qty=Decimal("10"),
            sale_date=sale_date,
            sale_price=sale_price,
            profile=profile(),
        )
        assert r.fx_rate == Decimal("1")
        assert r.fx_source == "reporting_currency"

    def test_indian_regime_applies_exemption_in_isolation(self):
        h = holding(
            currency="INR",
            regime=INDIAN_REGIME.key,
            lots=(lot(1, "10", "100", "2025-01-15T00:00:00+00:00"),),
        )
        result = simulate_sale(
            h,
            qty=Decimal("10"),
            sale_date=datetime(2026, 10, 16, tzinfo=UTC),
            sale_price=Decimal("200"),
            profile=profile(),
        )
        assert result.status == "success"
        assert result.ltcg_gain_native == Decimal("1000.00")
        assert result.gain_inr == Decimal("1000.00")
        # ₹1,000 gain is fully sheltered by the ₹1.25 lakh exemption.
        assert result.tax_inr == Decimal("0.00")
        assert any("isolation" in a for a in result.assumptions)
        assert any("1.25 lakh" in a for a in result.assumptions)

    def test_non_positive_quantity_rejected(self):
        h = holding(lots=(lot(1, "10", "100", "2024-01-15T00:00:00+00:00"),))
        with pytest.raises(ValueError, match="qty must be positive"):
            simulate_sale(
                h,
                qty=Decimal("0"),
                sale_date=datetime(2026, 10, 16, tzinfo=UTC),
                sale_price=Decimal("200"),
                profile=profile(fx_overrides={"2026-10": Decimal("100")}),
            )


class TestSimulateSaleValidation:
    def test_negative_sale_price_rejected(self):
        h = holding(lots=(lot(1, "10", "100", "2024-01-15T00:00:00+00:00"),))
        with pytest.raises(ValueError, match="sale_price"):
            simulate_sale(
                h,
                qty=Decimal("1"),
                sale_date=datetime(2026, 10, 16, tzinfo=UTC),
                sale_price=Decimal("-1"),
                profile=TaxProfile(),
            )


class TestListTaxLots:
    def test_foreign_24_month_countdown(self):
        h = holding(lots=(lot(1, "10", "100", "2024-01-15T00:00:00+00:00"),))
        mark = add_months(datetime(2024, 1, 15, tzinfo=UTC), 24)
        before = datetime(2026, 1, 14, tzinfo=UTC)
        on_mark = datetime(2026, 1, 15, tzinfo=UTC)
        after = datetime(2026, 1, 16, tzinfo=UTC)

        views = list_tax_lots(h, as_of=before)
        assert views[0].is_long_term is False
        assert views[0].days_to_long_term == (mark.date() - before.date()).days

        views = list_tax_lots(h, as_of=on_mark)
        # Exactly 24 months is still short-term ("more than 24 months").
        assert views[0].is_long_term is False
        assert views[0].days_to_long_term == 0

        views = list_tax_lots(h, as_of=after)
        assert views[0].is_long_term is True
        assert views[0].days_to_long_term == 0
        assert views[0].holding_months == 24

    def test_indian_12_month_boundary(self):
        h = holding(
            currency="INR",
            regime=INDIAN_REGIME.key,
            lots=(lot(1, "10", "100", "2025-01-15T00:00:00+00:00"),),
        )
        views = list_tax_lots(h, as_of=datetime(2026, 1, 15, tzinfo=UTC))
        assert views[0].is_long_term is False
        assert views[0].days_to_long_term == 0
        views = list_tax_lots(h, as_of=datetime(2026, 1, 16, tzinfo=UTC))
        assert views[0].is_long_term is True
        assert views[0].days_to_long_term == 0

    def test_unrealised_pnl_with_price_and_fx(self):
        h = holding(lots=(lot(1, "10", "100", "2024-01-15T00:00:00+00:00"),))
        views = list_tax_lots(
            h,
            as_of=datetime(2026, 10, 16, tzinfo=UTC),
            current_price=Decimal("150"),
            fx_rate=Decimal("90"),
        )
        [v] = views
        assert v.unrealised_pnl_native == Decimal("500.00")
        assert v.unrealised_pnl_inr == Decimal("45000.00")

    def test_no_price_leaves_pnl_none(self):
        h = holding(lots=(lot(1, "10", "100", "2024-01-15T00:00:00+00:00"),))
        views = list_tax_lots(h, as_of=datetime(2026, 10, 16, tzinfo=UTC))
        [v] = views
        assert v.unrealised_pnl_native is None
        assert v.unrealised_pnl_inr is None

    def test_empty_holding(self):
        assert list_tax_lots(holding()) == []


class TestHarvestCandidates:
    def test_ranks_by_tax_saved(self):
        h1 = holding(
            holding_id=1,
            symbol="AAPL",
            lots=(lot(1, "10", "200", "2022-01-15T00:00:00+00:00"),),  # long-term
        )
        h2 = holding(
            holding_id=2,
            symbol="MSFT",
            lots=(
                lot(2, "10", "300", "2026-01-15T00:00:00+00:00"),  # short-term
                lot(3, "5", "400", "2026-01-15T00:00:00+00:00"),  # short-term, same 1000 loss
            ),
        )
        candidates = harvest_candidates(
            [h1, h2],
            {1: Decimal("100"), 2: Decimal("200")},
            as_of=datetime(2026, 10, 16, tzinfo=UTC),
            stcg_rate_pct=Decimal("0.30"),
        )
        assert [c.holding_id for c in candidates] == [2, 2, 1]
        # h2 lot 2: loss 1000 @ 30% = 300; lot 3: loss 1000 @ 30% = 300
        # (tie broken by input order); h1 lot 1: loss 1000 @ 12.5% = 125.
        assert [c.tax_saved_native for c in candidates] == [
            Decimal("300.00"),
            Decimal("300.00"),
            Decimal("125.00"),
        ]
        assert candidates[0].rate_pct == Decimal("0.30")
        assert candidates[2].rate_pct == Decimal("0.125")
        assert candidates[0].reason.startswith("Short-term loss of 1000.00 at 30%")
        assert candidates[2].reason.startswith("Long-term loss of 1000.00 at 12.5%")

    def test_profitable_lots_are_excluded(self):
        h = holding(lots=(lot(1, "10", "100", "2024-01-15T00:00:00+00:00"),))
        assert (
            harvest_candidates([h], {1: Decimal("150")}, as_of=datetime(2026, 10, 16, tzinfo=UTC))
            == []
        )

    def test_holding_without_price_is_skipped(self):
        h = holding(lots=(lot(1, "10", "100", "2024-01-15T00:00:00+00:00"),))
        assert harvest_candidates([h], {}, as_of=datetime(2026, 10, 16, tzinfo=UTC)) == []

    def test_foreign_short_term_is_slab_dependent_and_ranks_last(self):
        h = holding(
            lots=(lot(1, "10", "100", "2026-01-15T00:00:00+00:00"),),  # short-term at sale
        )
        h_lt = holding(
            holding_id=2,
            lots=(lot(2, "10", "200", "2022-01-15T00:00:00+00:00"),),  # long-term
        )
        candidates = harvest_candidates(
            [h, h_lt],
            {1: Decimal("50"), 2: Decimal("100")},
            as_of=datetime(2026, 10, 16, tzinfo=UTC),
        )
        assert [c.holding_id for c in candidates] == [2, 1]
        assert candidates[0].rate_pct == Decimal("0.125")
        assert candidates[0].tax_saved_native == Decimal("125.00")
        assert candidates[1].rate_pct is None
        assert candidates[1].tax_saved_native is None
        assert "slab" in candidates[1].reason

    def test_fx_converts_tax_saved(self):
        h = holding(lots=(lot(1, "10", "200", "2022-01-15T00:00:00+00:00"),))
        candidates = harvest_candidates(
            [h],
            {1: Decimal("100")},
            as_of=datetime(2026, 10, 16, tzinfo=UTC),
            fx_rates={"USD": Decimal("90")},
        )
        [c] = candidates
        assert c.tax_saved_native == Decimal("125.00")
        assert c.tax_saved_inr == Decimal("11250.00")

    def test_mixed_currencies_rank_by_inr_not_native_amount(self):
        # USD lot: loss 1000 USD long-term @12.5% -> 125 USD = INR 11,250 at 90.
        # INR lot:  loss 5000 INR long-term @12.5% -> INR 625.
        # Ranked on raw native numbers the INR lot (625) would beat the USD
        # lot (125) even though it saves 18x less tax.
        usd = holding(
            holding_id=1,
            currency="USD",
            lots=(lot(1, "10", "200", "2022-01-15T00:00:00+00:00"),),
        )
        inr = holding(
            holding_id=2,
            symbol="NIFTYBEES",
            currency="INR",
            regime=INDIAN_REGIME.key,
            lots=(lot(2, "100", "250", "2022-01-15T00:00:00+00:00"),),
        )
        candidates = harvest_candidates(
            [inr, usd],
            {1: Decimal("100"), 2: Decimal("200")},
            as_of=datetime(2026, 10, 16, tzinfo=UTC),
            fx_rates={"USD": Decimal("90")},
        )
        assert [c.holding_id for c in candidates] == [1, 2]
        assert candidates[0].tax_saved_inr == Decimal("11250.00")
        # INR holdings are never converted with the USD rate.
        assert candidates[1].tax_saved_native == Decimal("625.00")
        assert candidates[1].tax_saved_inr == Decimal("625.00")

    def test_currency_without_rate_ranks_after_priced_lots(self):
        usd = holding(
            holding_id=1,
            currency="USD",
            lots=(lot(1, "10", "200", "2022-01-15T00:00:00+00:00"),),
        )
        inr = holding(
            holding_id=2,
            symbol="NIFTYBEES",
            currency="INR",
            regime=INDIAN_REGIME.key,
            lots=(lot(2, "100", "250", "2022-01-15T00:00:00+00:00"),),
        )
        candidates = harvest_candidates(
            [usd, inr],
            {1: Decimal("100"), 2: Decimal("200")},
            as_of=datetime(2026, 10, 16, tzinfo=UTC),
        )
        # No USD rate supplied: the USD lot's INR value is unknown, so it
        # cannot be ranked against the INR lot and sorts after it.
        assert [c.holding_id for c in candidates] == [2, 1]
        assert candidates[0].tax_saved_inr == Decimal("625.00")
        assert candidates[1].tax_saved_inr is None
