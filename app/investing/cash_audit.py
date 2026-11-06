"""Read-only audit of brokerage cash snapshots against the ledger.

Brokerage cash is an append-only chain of absolute balances in
``investing_cash_balances`` (each row = previous + delta). If a row is removed or
edited mid-chain -- or a transfer predates the snapshot behaviour -- the latest
balance silently drifts from what the underlying transfers, orders and dividends
imply. This module recomputes the implied balance from first principles and
reports the difference. It never writes.

Method, per (workspace, brokerage account, currency):

    anchor   = newest snapshot that is NOT derived (trigger_type NULL / "manual"),
               i.e. a balance a human asserted; else zero at the beginning of time
    expected = anchor.balance
               + transfers in (net received) - transfers out (gross)
               + sell net - buy net
               + dividends/interest net credited to this account
               counting only entities created after the anchor
    drift    = latest snapshot balance - expected

Entities created after the anchor are used (not occurred_at) because the anchor
is a statement of what the balance was when it was recorded.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance.models import Account, CapitalTransfer
from app.investing.models import CashBalance, Dividend, InvestingOrder

ZERO = Decimal("0")
DERIVED_TRIGGERS = frozenset({"transfer", "order", "dividend"})


@dataclass(frozen=True)
class CashDrift:
    workspace_id: int
    account_id: int
    account_name: str
    currency: str
    latest_balance: Decimal
    expected_balance: Decimal
    anchor_balance: Decimal
    anchor_created_at: datetime | None

    @property
    def drift(self) -> Decimal:
        return self.latest_balance - self.expected_balance


async def _sum(session: AsyncSession, stmt) -> Decimal:
    return (await session.execute(stmt)).scalar_one() or ZERO


async def audit_brokerage_cash(
    session: AsyncSession,
    workspace_id: int | None = None,
    account_id: int | None = None,
    currency: str | None = None,
) -> Sequence[CashDrift]:
    """Return one CashDrift per (account, currency) that has snapshots.

    Includes zero-drift rows so callers can show the full picture; filter on
    ``.drift != 0`` for the stale set.
    """
    pairs_stmt = (
        select(CashBalance.workspace_id, CashBalance.account_id, CashBalance.currency, Account.name)
        .join(Account, Account.id == CashBalance.account_id)
        .where(Account.account_type == "brokerage")
        .group_by(
            CashBalance.workspace_id, CashBalance.account_id, CashBalance.currency, Account.name
        )
        .order_by(CashBalance.workspace_id, Account.name, CashBalance.currency)
    )
    if workspace_id is not None:
        pairs_stmt = pairs_stmt.where(CashBalance.workspace_id == workspace_id)
    if account_id is not None:
        pairs_stmt = pairs_stmt.where(CashBalance.account_id == account_id)
    if currency is not None:
        pairs_stmt = pairs_stmt.where(CashBalance.currency == currency)
    pairs = (await session.execute(pairs_stmt)).all()

    results: list[CashDrift] = []
    for ws_id, acct_id, currency, acct_name in pairs:
        chain = (
            select(CashBalance)
            .where(
                CashBalance.workspace_id == ws_id,
                CashBalance.account_id == acct_id,
                CashBalance.currency == currency,
            )
            .order_by(CashBalance.created_at.desc(), CashBalance.id.desc())
        )
        latest = (await session.execute(chain.limit(1))).scalar_one()
        anchor = (
            await session.execute(
                chain.where(
                    or_(
                        CashBalance.trigger_type.is_(None),
                        CashBalance.trigger_type.not_in(DERIVED_TRIGGERS),
                    )
                ).limit(1)
            )
        ).scalar_one_or_none()
        anchor_balance = anchor.balance if anchor else ZERO
        anchor_at = anchor.created_at if anchor else None

        def after_anchor(column, anchor_at=anchor_at):
            return column > anchor_at if anchor_at is not None else column.is_not(None)

        transfers_in = await _sum(
            session,
            select(func.coalesce(func.sum(CapitalTransfer.net_amount_received), 0)).where(
                CapitalTransfer.workspace_id == ws_id,
                CapitalTransfer.to_account_id == acct_id,
                CapitalTransfer.to_currency_code == currency,
                CapitalTransfer.to_module == "investing",
                after_anchor(CapitalTransfer.created_at),
            ),
        )
        transfers_out = await _sum(
            session,
            select(func.coalesce(func.sum(CapitalTransfer.gross_amount), 0)).where(
                CapitalTransfer.workspace_id == ws_id,
                CapitalTransfer.from_account_id == acct_id,
                CapitalTransfer.from_currency_code == currency,
                CapitalTransfer.from_module == "investing",
                after_anchor(CapitalTransfer.created_at),
            ),
        )
        sells = await _sum(
            session,
            select(func.coalesce(func.sum(InvestingOrder.net_amount), 0)).where(
                InvestingOrder.workspace_id == ws_id,
                InvestingOrder.account_id == acct_id,
                InvestingOrder.currency == currency,
                InvestingOrder.order_type == "sell",
                after_anchor(InvestingOrder.created_at),
            ),
        )
        buys = await _sum(
            session,
            select(func.coalesce(func.sum(InvestingOrder.net_amount), 0)).where(
                InvestingOrder.workspace_id == ws_id,
                InvestingOrder.account_id == acct_id,
                InvestingOrder.currency == currency,
                InvestingOrder.order_type == "buy",
                after_anchor(InvestingOrder.created_at),
            ),
        )
        dividends = await _sum(
            session,
            select(func.coalesce(func.sum(Dividend.net_amount), 0)).where(
                Dividend.workspace_id == ws_id,
                func.coalesce(Dividend.credit_account_id, Dividend.account_id) == acct_id,
                Dividend.currency == currency,
                after_anchor(Dividend.created_at),
            ),
        )

        expected = anchor_balance + transfers_in - transfers_out + sells - buys + dividends
        results.append(
            CashDrift(
                workspace_id=ws_id,
                account_id=acct_id,
                account_name=acct_name,
                currency=currency,
                latest_balance=latest.balance,
                expected_balance=expected,
                anchor_balance=anchor_balance,
                anchor_created_at=anchor_at,
            )
        )
    return results


class CorrectionRefusedError(Exception):
    """The reviewed numbers no longer match the data; re-run the audit."""


async def apply_correction(
    session: AsyncSession,
    *,
    workspace_id: int,
    account_id: int,
    currency: str,
    reviewed_expected: Decimal,
    source_ref: str,
) -> CashBalance:
    """Append ONE manual snapshot that sets the account to its recomputed balance.

    Append-only: history is never edited or deleted, so it is reversible by
    appending another snapshot. Refuses unless the recomputed expected balance
    equals what the operator reviewed, so a stale dry-run cannot be applied.
    Does not commit; the caller owns the transaction.
    """
    rows = await audit_brokerage_cash(session, workspace_id, account_id, currency)
    if len(rows) != 1:
        raise CorrectionRefusedError("no unique brokerage cash series for that account/currency")
    row = rows[0]
    if row.drift == 0:
        raise CorrectionRefusedError("no drift: stored balance already matches the ledger")
    if row.expected_balance != reviewed_expected:
        raise CorrectionRefusedError(
            f"expected balance is now {row.expected_balance}, not the reviewed {reviewed_expected}"
        )
    latest = (
        await session.execute(
            select(CashBalance)
            .where(
                CashBalance.workspace_id == workspace_id,
                CashBalance.account_id == account_id,
                CashBalance.currency == currency,
            )
            .order_by(CashBalance.created_at.desc(), CashBalance.id.desc())
            .limit(1)
        )
    ).scalar_one()
    correction = CashBalance(
        workspace_id=workspace_id,
        user_id=latest.user_id,
        account_id=account_id,
        balance=row.expected_balance,
        currency=currency,
        as_of=datetime.now(UTC),
        source_type="manual",
        source_ref=source_ref,
    )
    session.add(correction)
    await session.flush()
    return correction
