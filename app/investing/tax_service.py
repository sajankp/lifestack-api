"""Capital gains tax service (spec-099).

The DB-facing half of the feature: assembles the engine's inputs (orders,
instruments, accounts, reference securities, the tax profile), calls the
pure engine, and shapes the result for the API and the MCP tools.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.finance.models import Account, WorkspaceTaxProfile
from app.investing.models import Instrument, InvestingOrder
from app.investing.repository import (
    InvestingOrderRepository,
    ReferenceSecurityRepository,
)
from app.investing.tax.engine import (
    REPORTING_CURRENCY,
    TaxEstimate,
    compute_tax_estimate,
    sale_month_key,
    specified_fx_date,
)
from app.investing.tax.exchanges import resolve_exchange
from app.investing.tax.fy import current_fy_label, parse_fy_label
from app.investing.tax.gains import OrderView
from app.investing.tax.regime import RegimeType, classify_regime
from app.investing.tax.tax_profile import TaxProfile


@dataclass
class TaxProfileRecord:
    """A tax profile row plus its public id."""

    row: WorkspaceTaxProfile


class TaxProfileRepository:
    """Read/write access to the workspace tax profile."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(self, workspace_id: int) -> WorkspaceTaxProfile:
        result = await self.session.execute(
            select_workspace_tax_profile().where(
                WorkspaceTaxProfile.workspace_id == workspace_id
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            return row
        row = WorkspaceTaxProfile(workspace_id=workspace_id)
        self.session.add(row)
        await self.session.flush()
        return row

    async def update(self, row: WorkspaceTaxProfile, **changes) -> WorkspaceTaxProfile:
        for key, value in changes.items():
            if value is not None or key in ("slab_override_pct", "surcharge_override_pct", "explicit_regime", "notes"):
                setattr(row, key, value)
        row.updated_at = datetime.now(UTC)
        self.session.add(row)
        await self.session.flush()
        return row


def select_workspace_tax_profile():
    from sqlalchemy import select

    return select(WorkspaceTaxProfile)


def profile_to_domain(row: WorkspaceTaxProfile) -> TaxProfile:
    """Map a tax profile row to the engine's domain object."""
    overrides: dict[str, Decimal] = {}
    for month, rate in (row.fx_overrides or {}).items():
        try:
            overrides[month] = Decimal(str(rate))
        except Exception:
            continue
    explicit = None
    if row.explicit_regime:
        try:
            explicit = RegimeType(row.explicit_regime)
        except ValueError:
            explicit = None
    return TaxProfile(
        resident_status=row.resident_status,
        tax_regime=row.tax_regime,
        other_income=Decimal(row.other_income or 0),
        slab_override_pct=(
            Decimal(row.slab_override_pct) if row.slab_override_pct is not None else None
        ),
        surcharge_override_pct=(
            Decimal(row.surcharge_override_pct)
            if row.surcharge_override_pct is not None
            else None
        ),
        cess_pct=Decimal(row.cess_pct if row.cess_pct is not None else 4),
        explicit_regime=explicit,
        apply_carry_forward=bool(row.apply_carry_forward),
        fx_overrides=overrides,
        notes=row.notes,
    )


class TaxService:
    """Computes the capital gains tax estimate for a workspace."""

    def __init__(
        self,
        session: AsyncSession,
        order_repository: InvestingOrderRepository,
        reference_repository: ReferenceSecurityRepository,
        account_repository,
        instrument_repository,
        fx_rate_repository=None,
    ) -> None:
        self.session = session
        self.order_repository = order_repository
        self.reference_repository = reference_repository
        self.account_repository = account_repository
        self.instrument_repository = instrument_repository
        self.fx_rate_repository = fx_rate_repository
        self.profile_repository = TaxProfileRepository(session)

    # ------------------------------------------------------------ loading

    async def _load_orders(
        self,
        workspace_id: int,
        *,
        account_public_id: str | None = None,
    ) -> list[OrderView]:
        internal_account_id: int | None = None
        if account_public_id is not None:
            account = await self.account_repository.get_by_public_id(
                workspace_id, uuid.UUID(account_public_id)
            )
            if account is None or account.id is None:
                raise ValueError("Account not found")
            internal_account_id = account.id
        orders, _total = await self.order_repository.list_by_workspace(
            workspace_id, limit=100000, offset=0, account_id=internal_account_id
        )
        accounts = await self.account_repository.get_by_ids(
            workspace_id, {o.account_id for o in orders}
        )
        instruments = await self.instrument_repository.get_by_ids([
            o.instrument_id for o in orders if o.instrument_id is not None
        ])
        views: list[OrderView] = []
        for order in orders:
            account = accounts.get(order.account_id)
            instrument = (
                instruments.get(order.instrument_id) if order.instrument_id else None
            )
            views.append(
                OrderView(
                    public_id=str(order.public_id),
                    order_type=order.order_type,
                    symbol=order.symbol,
                    quantity=Decimal(order.quantity),
                    price_per_unit=Decimal(order.price_per_unit),
                    gross_amount=Decimal(order.gross_amount),
                    brokerage_fee=Decimal(order.brokerage_fee or 0),
                    tax_amount=Decimal(order.tax_amount or 0),
                    other_fees=Decimal(order.other_fees or 0),
                    occurred_at=order.occurred_at,
                    currency=order.currency,
                    created_at=order.created_at,
                    account_id=order.account_id,
                    account_public_id=str(account.public_id) if account else "",
                    account_name=account.name if account else "",
                    instrument_id=order.instrument_id,
                    instrument_type=(
                        instrument.instrument_type if instrument else "stock"
                    ),
                    realized_gain_loss=(
                        Decimal(order.realized_gain_loss)
                        if order.realized_gain_loss is not None
                        else None
                    ),
                    avg_cost_at_sale=(
                        Decimal(order.avg_cost_at_sale)
                        if order.avg_cost_at_sale is not None
                        else None
                    ),
                )
            )
        return views

    async def _security_country(self, symbol: str) -> str | None:
        rows = await self.reference_repository.list_by_ticker(symbol)
        if len(rows) != 1:
            # Unresolved or ambiguous — do not guess (spec-083 §6.1).
            return None
        return rows[0].country_code

    async def _app_fx_rates(
        self, months: list[tuple[str, str]]
    ) -> dict[tuple[str, str], Decimal]:
        """The app's stored rates, keyed ``(month, currency)``.

        Rule 115 uses the SBI TT buying rate on the last day of the month
        before the month of transfer. The app stores daily market rates,
        so for each (month, currency) we take the latest rate on or before
        that specified date. A pair with no stored rate is simply absent —
        the engine then reports it as missing rather than inventing one.
        """
        if self.fx_rate_repository is None:
            return {}
        by_key: dict[tuple[str, str], Decimal] = {}
        for month, currency in months:
            if currency == REPORTING_CURRENCY:
                continue
            specified = specified_fx_date(
                datetime.strptime(month, "%Y-%m").date().replace(day=1)
            )
            found = await self.fx_rate_repository.get_latest_rate(
                base_currency_code=currency,
                quote_currency_code=REPORTING_CURRENCY,
                as_of=specified,
            )
            if found is not None:
                by_key[(month, currency)] = Decimal(str(found.rate))
        return by_key

    # ------------------------------------------------------------- compute

    async def compute(
        self,
        workspace_id: int,
        *,
        fy: str | None = None,
        account_id: str | None = None,
    ) -> TaxEstimate:
        """Compute the tax estimate for a workspace."""
        fy_label = (fy or current_fy_label()).strip()
        # Validate the label so a bad value fails loudly rather than
        # silently computing nothing.
        parse_fy_label(fy_label)

        profile_row = await self.profile_repository.get_or_create(workspace_id)
        profile = profile_to_domain(profile_row)

        orders = await self._load_orders(workspace_id, account_public_id=account_id)
        if not orders:
            return TaxEstimate(status="success", fy_label=fy_label)

        accounts = await self.account_repository.get_by_ids(
            workspace_id, {o.account_id for o in orders}
        )
        # Look the instruments up by symbol, not by the order's instrument_id:
        # an order may reference a symbol that has no instrument row (or whose
        # instrument was created after the order), and the regime resolution
        # keys off the symbol.
        instruments = await self.instrument_repository.get_by_symbols(
            workspace_id, [o.symbol for o in orders]
        )

        # Build a symbol -> country cache; the reference lookup is per ticker.
        country_cache: dict[str, str | None] = {}
        instrument_by_symbol: dict[str, Instrument] = {}
        for instrument in instruments.values():
            instrument_by_symbol[instrument.symbol.upper()] = instrument

        async def resolve(order):
            symbol = order.symbol.upper()
            if symbol not in country_cache:
                country_cache[symbol] = await self._security_country(symbol)
            instrument = instrument_by_symbol.get(symbol)
            account = accounts.get(order.account_id)
            classification = classify_regime(
                explicit_regime=profile.explicit_regime,
                instrument_country_override=(
                    instrument.exchange_country_override if instrument else None
                ),
                security_country_code=country_cache.get(symbol),
                account_market_scope=(account.market_scope if account else None),
                holding_currency=order.currency,
            )
            return (
                classification.regime,
                classification.source.value,
                classification.detail,
            )

        # The engine is pure and synchronous (spec-099 §2.1), so resolve
        # every sell's classification here, where the awaits are allowed,
        # and hand the engine a plain dict lookup.
        resolved: dict[str, tuple[RegimeType, str, str]] = {}
        for order in orders:
            if order.order_type != "sell":
                continue
            resolved[order.public_id] = await resolve(order)

        def sync_resolver(order):
            return resolved[order.public_id]

        # The Rule 115 rate is keyed by the month of transfer, so collect
        # the (month, currency) pairs the sell orders need and resolve one
        # rate for each.
        sell_pairs = sorted(
            {
                (
                    sale_month_key(o.occurred_at.date()),
                    o.currency.upper(),
                )
                for o in orders
                if o.order_type == "sell"
            }
        )
        app_fx = await self._app_fx_rates(sell_pairs)
        return compute_tax_estimate(
            orders=orders,
            fy_label=fy_label,
            profile=profile,
            regime_resolver=sync_resolver,
            app_fx_rates=app_fx,
        )

    # ------------------------------------------------------------- profile

    async def get_profile(self, workspace_id: int) -> WorkspaceTaxProfile:
        return await self.profile_repository.get_or_create(workspace_id)

    async def update_profile(self, workspace_id: int, changes: dict) -> WorkspaceTaxProfile:
        row = await self.profile_repository.get_or_create(workspace_id)
        return await self.profile_repository.update(row, **changes)

    # ------------------------------------------------- account / holding flags

    async def set_account_market_scope(
        self,
        workspace_id: int,
        account_public_id: str,
        market_scope: str | None,
    ) -> Account:
        account = await self.account_repository.get_by_public_id(
            workspace_id, uuid.UUID(account_public_id)
        )
        if account is None:
            raise ValueError("Account not found")
        if account.account_type != "brokerage":
            raise ValueError(
                f"Account '{account.name}' is type '{account.account_type}'; "
                "market_scope only applies to brokerage accounts."
            )
        account.market_scope = market_scope
        account.updated_at = datetime.now(UTC)
        self.session.add(account)
        await self.session.flush()
        return account

    async def set_instrument_country_override(
        self,
        workspace_id: int,
        instrument_public_id: str,
        country: str | None,
    ) -> Instrument:
        instrument = await self.instrument_repository.get_by_public_id(
            workspace_id, uuid.UUID(instrument_public_id)
        )
        if instrument is None:
            raise ValueError("Instrument not found")
        instrument.exchange_country_override = country
        instrument.updated_at = datetime.now(UTC)
        self.session.add(instrument)
        await self.session.flush()
        return instrument

    async def list_account_instruments(
        self,
        workspace_id: int,
        account_public_id: str,
    ) -> list[Instrument]:
        """Instruments held in an account, for the MCP status tool."""
        account = await self.account_repository.get_by_public_id(
            workspace_id, uuid.UUID(account_public_id)
        )
        if account is None or account.id is None:
            return []
        orders, _ = await self.order_repository.list_by_workspace(
            workspace_id, limit=100000, offset=0, account_id=account.id
        )
        symbols = {
            o.symbol.upper()
            for o in orders
            if o.account_id == account.id
        }
        if not symbols:
            return []
        # Resolve by symbol, not by the order's instrument_id: the
        # workspace-scoped copy (which carries the override) shadows the
        # global reference row, and the two can have different ids.
        rows = await self.instrument_repository.get_by_symbols(workspace_id, symbols)
        return sorted(rows.values(), key=lambda i: i.symbol.upper())


def exchange_country_for(name: str | None) -> str | None:
    """The country for an exchange name, or None if unrecognised."""
    return resolve_exchange(name)[1]
