"""Investment return metrics: XIRR, annualized %, realized/unrealized split,
open vs closed positions, drawdown (spec-071).

Metrics computed overall, per brokerage account, and per currency. The
cross-currency aggregate (``overall`` when the workspace holds more than one
currency) converts each cash flow at its own historical FX rate (INV-1) via
FxRateService.resolve_historical_rate (spec-072); when a required rate is
missing the aggregate is returned with ``valuation_status='conversion_required'``
while per-currency/per-account (single-currency) blocks still compute.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from app.finance.repository import (
    AccountRepository,
    FinanceSettingRepository,
    NetWorthSnapshotRepository,
)
from app.finance.service import FxRateService
from app.investing.models import Dividend, Holding, InvestingOrder
from app.investing.repository import (
    DividendRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InvestingOrderRepository,
)
from app.investing.schemas import (
    AccountReturnMetrics,
    CurrencyReturnMetrics,
    MaxDrawdown,
    OverallReturnMetrics,
    PositionMetrics,
    ReturnMetricsResponse,
)
from app.investing.xirr import CashFlow, solve_xirr

MONEY_QUANT = Decimal("0.01")
ANNUALIZATION_MIN_DAYS = 365


@dataclass
class _Flow:
    when: date
    amount: Decimal
    is_contribution: bool  # False for dividend income (spec-073 INV-6)


@dataclass
class _Position:
    account_id: int
    symbol: str
    currency: str
    is_open: bool
    flows: list[_Flow] = field(default_factory=list)
    realized: Decimal = Decimal("0")
    market_value: Decimal = Decimal("0")
    book_value: Decimal = Decimal("0")


def _annualize(total_return_pct: Decimal, holding_days: int) -> Decimal | None:
    if holding_days <= 0:
        return None
    try:
        base = Decimal("1") + (total_return_pct / Decimal("100"))
        if base <= 0:
            return None
        exponent = Decimal("365") / Decimal(holding_days)
        result = Decimal(str(float(base) ** float(exponent))) - Decimal("1")
        return (result * Decimal("100")).quantize(MONEY_QUANT)
    except (ArithmeticError, ValueError):
        # ArithmeticError covers Overflow/ZeroDivision AND
        # decimal.InvalidOperation (quantize of an infinite float round-trip).
        return None


def _position_metrics(
    flows: list[_Flow], realized: Decimal, market_value: Decimal, book_value: Decimal
) -> PositionMetrics:
    invested = sum((f.amount for f in flows if f.is_contribution and f.amount < 0), Decimal("0"))
    invested = abs(invested)
    unrealized = market_value - book_value

    xirr_flows = [CashFlow(when=f.when, amount=f.amount) for f in flows]
    xirr = solve_xirr(xirr_flows)

    holding_days = None
    total_return_pct = None
    annualized = None
    reliable = False
    if flows:
        first_date = min(f.when for f in flows)
        last_date = max(f.when for f in flows)
        holding_days = (last_date - first_date).days
        # flows already carry the terminal market-value flow for open
        # positions -- adding market_value again would double-count it and
        # render a flat position as +100%.
        net_flow = sum((f.amount for f in flows), Decimal("0"))
        if invested > 0:
            total_return_pct = ((net_flow / invested) * Decimal("100")).quantize(MONEY_QUANT)
            reliable = holding_days >= ANNUALIZATION_MIN_DAYS
            if reliable:
                annualized = _annualize(total_return_pct, holding_days)

    return PositionMetrics(
        xirr=xirr.quantize(Decimal("0.0001")) if xirr is not None else None,
        annualized_return_pct=annualized,
        annualization_reliable=reliable,
        holding_days=holding_days,
        total_return_pct=total_return_pct,
        realized=realized.quantize(MONEY_QUANT),
        unrealized=unrealized.quantize(MONEY_QUANT),
        market_value=market_value.quantize(MONEY_QUANT),
        invested=invested.quantize(MONEY_QUANT),
    )


def _scope_metrics(positions: Sequence[_Position]) -> dict:
    open_positions = [p for p in positions if p.is_open]
    closed_positions = [p for p in positions if not p.is_open]

    open_flows = [f for p in open_positions for f in p.flows]
    closed_flows = [f for p in closed_positions for f in p.flows]

    open_realized = sum((p.realized for p in open_positions), Decimal("0"))
    closed_realized = sum((p.realized for p in closed_positions), Decimal("0"))
    open_market_value = sum((p.market_value for p in open_positions), Decimal("0"))
    open_book_value = sum((p.book_value for p in open_positions), Decimal("0"))

    open_metrics = _position_metrics(open_flows, open_realized, open_market_value, open_book_value)
    closed_metrics = _position_metrics(closed_flows, closed_realized, Decimal("0"), Decimal("0"))

    all_flows = open_flows + closed_flows
    all_realized = open_realized + closed_realized
    overall_metrics = _position_metrics(all_flows, all_realized, open_market_value, open_book_value)

    return {
        "xirr": overall_metrics.xirr,
        "annualized_return_pct": overall_metrics.annualized_return_pct,
        "annualization_reliable": overall_metrics.annualization_reliable,
        "holding_days": overall_metrics.holding_days,
        "total_return_pct": overall_metrics.total_return_pct,
        "realized": overall_metrics.realized,
        "unrealized": overall_metrics.unrealized,
        "open": open_metrics,
        "closed": closed_metrics,
    }


class ReturnMetricsService:
    def __init__(
        self,
        order_repo: InvestingOrderRepository,
        holding_repo: HoldingRepository,
        holding_price_repo: HoldingPriceRepository,
        dividend_repo: DividendRepository,
        account_repo: AccountRepository,
        net_worth_snapshot_repo: NetWorthSnapshotRepository,
        fx_rate_service: FxRateService,
        finance_setting_repo: FinanceSettingRepository | None = None,
    ):
        self.order_repo = order_repo
        self.holding_repo = holding_repo
        self.holding_price_repo = holding_price_repo
        self.dividend_repo = dividend_repo
        self.account_repo = account_repo
        self.net_worth_snapshot_repo = net_worth_snapshot_repo
        self.fx_rate_service = fx_rate_service
        self.finance_setting_repo = finance_setting_repo

    async def _build_positions(
        self, workspace_id: int
    ) -> tuple[list[_Position], dict[int, Decimal]]:
        orders, _ = await self.order_repo.list_by_workspace(workspace_id, limit=100000, offset=0)
        holdings, _ = await self.holding_repo.get_all(workspace_id, limit=100000, offset=0)
        dividends, _ = await self.dividend_repo.list_by_workspace(
            workspace_id, limit=100000, offset=0
        )

        today = datetime.now(UTC).date()
        holding_ids = [h.id for h in holdings if h.id is not None]
        latest_prices = await self.holding_price_repo.latest_prices_on_or_before_bulk(
            workspace_id, holding_ids, today
        )

        holding_by_key: dict[tuple[int, str], Holding] = {
            (h.account_id, h.symbol.upper()): h for h in holdings
        }

        orders_by_key: dict[tuple[int, str], list[InvestingOrder]] = {}
        for o in orders:
            key = (o.account_id, o.symbol.upper())
            orders_by_key.setdefault(key, []).append(o)

        dividends_by_key: dict[tuple[int, str], list[Dividend]] = {}
        account_level_dividends: dict[int, list[Dividend]] = {}
        for d in dividends:
            if d.symbol:
                key = (d.account_id, d.symbol.upper())
                dividends_by_key.setdefault(key, []).append(d)
            else:
                account_level_dividends.setdefault(d.account_id, []).append(d)

        positions: list[_Position] = []
        for key, symbol_orders in orders_by_key.items():
            account_id, symbol = key
            currency = symbol_orders[0].currency
            holding = holding_by_key.get(key)
            is_open = holding is not None and holding.quantity > 0

            flows: list[_Flow] = []
            realized = Decimal("0")
            for o in symbol_orders:
                occurred_date = (
                    o.occurred_at.date() if isinstance(o.occurred_at, datetime) else o.occurred_at
                )
                if o.order_type == "buy":
                    flows.append(
                        _Flow(when=occurred_date, amount=-o.net_amount, is_contribution=True)
                    )
                else:
                    flows.append(
                        _Flow(when=occurred_date, amount=o.net_amount, is_contribution=True)
                    )
                    if o.realized_gain_loss is not None:
                        realized += o.realized_gain_loss

            for d in dividends_by_key.get(key, []):
                # Dividends are income, not contribution (spec-073 INV-6):
                # a positive flow at pay date, excluded from invested capital.
                flows.append(_Flow(when=d.pay_date, amount=d.net_amount, is_contribution=False))
                realized += d.net_amount

            market_value = Decimal("0")
            book_value = Decimal("0")
            if is_open and holding is not None:
                price_row = latest_prices.get(holding.id) if holding.id is not None else None
                current_price = price_row.unit_price if price_row is not None else holding.avg_cost
                market_value = holding.quantity * current_price
                book_value = holding.quantity * holding.avg_cost
                flows.append(_Flow(when=today, amount=market_value, is_contribution=False))

            flows.sort(key=lambda f: f.when)
            positions.append(
                _Position(
                    account_id=account_id,
                    symbol=symbol,
                    currency=currency,
                    is_open=is_open,
                    flows=flows,
                    realized=realized,
                    market_value=market_value,
                    book_value=book_value,
                )
            )

        account_level_flows: dict[int, list[_Flow]] = {}
        for account_id, divs in account_level_dividends.items():
            account_level_flows[account_id] = [
                _Flow(when=d.pay_date, amount=d.net_amount, is_contribution=False) for d in divs
            ]

        return positions, account_level_flows

    async def get_return_metrics(self, workspace_id: int) -> ReturnMetricsResponse:
        positions, account_level_dividend_flows = await self._build_positions(workspace_id)

        accounts, _ = await self.account_repo.list_workspace_accounts(
            workspace_id, limit=10000, offset=0
        )
        account_by_id = {a.id: a for a in accounts if a.id is not None}

        # Account-level income (interest -- no symbol) becomes a synthetic
        # closed "position" merged into the main list so it flows into ALL
        # scopes (overall, by_currency, by_account) consistently; otherwise
        # overall realized would disagree with the sum of the account blocks.
        # It never has an open/closed identity of its own (interest isn't a
        # security), so is_open is False and it carries no market value.
        for account_id, extra_flows in account_level_dividend_flows.items():
            account = account_by_id.get(account_id)
            if account is None or not extra_flows:
                continue
            positions.append(
                _Position(
                    account_id=account_id,
                    symbol="__account_income__",
                    currency=account.default_currency_code,
                    is_open=False,
                    flows=extra_flows,
                    realized=sum((f.amount for f in extra_flows), Decimal("0")),
                )
            )

        currencies_present = {p.currency for p in positions}

        # --- by_account (single currency per account, spec-050) ---
        by_account: list[AccountReturnMetrics] = []
        positions_by_account: dict[int, list[_Position]] = {}
        for p in positions:
            positions_by_account.setdefault(p.account_id, []).append(p)

        for account_id, account_positions in positions_by_account.items():
            account = account_by_id.get(account_id)
            if account is None:
                continue
            metrics = _scope_metrics(account_positions)
            by_account.append(
                AccountReturnMetrics(
                    account_id=account.public_id,
                    account_name=account.name,
                    currency=account.default_currency_code,
                    data_quality="complete",
                    **metrics,
                )
            )

        # --- by_currency ---
        by_currency: list[CurrencyReturnMetrics] = []
        for currency in sorted(currencies_present):
            currency_positions = [p for p in positions if p.currency == currency]
            metrics = _scope_metrics(currency_positions)
            by_currency.append(
                CurrencyReturnMetrics(currency=currency, data_quality="complete", **metrics)
            )

        # --- overall (single currency: no conversion; multi-currency: INV-1) ---
        reporting_currency = None
        if self.finance_setting_repo is not None:
            setting = await self.finance_setting_repo.get_by_workspace(workspace_id)
            if setting is not None:
                reporting_currency = setting.reporting_currency_code

        valuation_status = "current"
        if len(currencies_present) <= 1:
            overall_positions = positions
        elif reporting_currency is None:
            valuation_status = "conversion_required"
            overall_positions = []
        else:
            converted_positions, conversion_ok = await self._convert_positions(
                workspace_id, positions, reporting_currency
            )
            if not conversion_ok:
                valuation_status = "conversion_required"
                overall_positions = []
            else:
                overall_positions = converted_positions

        overall_metrics_dict = (
            _scope_metrics(overall_positions)
            if overall_positions or len(currencies_present) <= 1
            else _scope_metrics([])
        )

        drawdown = await self._compute_drawdown(workspace_id)

        overall = OverallReturnMetrics(
            data_quality="complete",
            max_drawdown=drawdown,
            **overall_metrics_dict,
        )

        currency_label = (
            next(iter(currencies_present)) if len(currencies_present) == 1 else reporting_currency
        )

        return ReturnMetricsResponse(
            currency=currency_label,
            valuation_status=valuation_status,
            overall=overall,
            by_account=by_account,
            by_currency=by_currency,
        )

    async def _convert_positions(
        self, workspace_id: int, positions: list[_Position], reporting_currency: str
    ) -> tuple[list[_Position], bool]:
        converted: list[_Position] = []
        for p in positions:
            if p.currency.upper() == reporting_currency.upper():
                converted.append(p)
                continue
            new_flows: list[_Flow] = []
            for f in p.flows:
                as_of_dt = datetime.combine(f.when, datetime.min.time(), tzinfo=UTC)
                result = await self.fx_rate_service.resolve_historical_rate(
                    workspace_id, p.currency, reporting_currency, as_of_dt
                )
                if result is None:
                    return [], False
                rate, _source = result
                new_flows.append(
                    _Flow(when=f.when, amount=f.amount * rate, is_contribution=f.is_contribution)
                )
            as_of_today = datetime.now(UTC)
            rate_today = await self.fx_rate_service.resolve_historical_rate(
                workspace_id, p.currency, reporting_currency, as_of_today
            )
            if rate_today is None:
                return [], False
            rate, _source = rate_today
            converted.append(
                _Position(
                    account_id=p.account_id,
                    symbol=p.symbol,
                    currency=reporting_currency,
                    is_open=p.is_open,
                    flows=new_flows,
                    realized=p.realized * rate,
                    market_value=p.market_value * rate,
                    book_value=p.book_value * rate,
                )
            )
        return converted, True

    async def _compute_drawdown(self, workspace_id: int) -> MaxDrawdown | None:
        today = datetime.now(UTC).date()
        history = await self.net_worth_snapshot_repo.get_history(
            workspace_id, date(2000, 1, 1), today
        )
        live_points = [
            (
                h.snapshot_date,
                (h.holdings_value or Decimal("0")) + (h.investing_cash or Decimal("0")),
            )
            for h in history
            if h.source == "live" and h.holdings_value is not None and h.investing_cash is not None
        ]
        if len(live_points) < 2:
            return None

        live_points.sort(key=lambda x: x[0])
        peak_date, peak_value = live_points[0]
        max_drawdown_pct = Decimal("0")
        max_peak_date = peak_date
        max_trough_date = peak_date

        for snapshot_date, value in live_points:
            if value > peak_value:
                peak_value = value
                peak_date = snapshot_date
            if peak_value > 0:
                drawdown_pct = ((peak_value - value) / peak_value) * Decimal("100")
                if drawdown_pct > max_drawdown_pct:
                    max_drawdown_pct = drawdown_pct
                    max_peak_date = peak_date
                    max_trough_date = snapshot_date

        if max_drawdown_pct == 0:
            return None
        return MaxDrawdown(
            pct=max_drawdown_pct.quantize(MONEY_QUANT),
            peak_date=max_peak_date,
            trough_date=max_trough_date,
        )
