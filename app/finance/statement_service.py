"""Spec-078: wallet ledger reconciliation — match engine + reconciliation view.

INV-1 (matching is metadata, never mutation): every write here touches only
`statement_lines.matched_*` columns. Nothing in this module ever creates,
edits, or deletes a `spending_transactions` or `capital_transfers` row.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.finance.models import Account, CapitalTransfer
from app.finance.statement_models import AccountStatement, StatementLine, StatementLineMatchLeg
from app.spending.models import SpendingTransaction, TransactionType

MATCH_WINDOW_DAYS = 3  # ±3 days default (owner decision, spec-078)


def _signed_transaction_amount(tx: SpendingTransaction) -> Decimal:
    return tx.amount if tx.type == TransactionType.income else -tx.amount


def _signed_transfer_amount(transfer: CapitalTransfer, leg: StatementLineMatchLeg) -> Decimal:
    return (
        -transfer.gross_amount
        if leg == StatementLineMatchLeg.from_leg
        else transfer.net_amount_received
    )


class StatementService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _get_account(self, workspace_id: int, account_public_id: uuid.UUID) -> Account:
        account = (
            await self.session.execute(
                select(Account).where(
                    Account.workspace_id == workspace_id, Account.public_id == account_public_id
                )
            )
        ).scalar_one_or_none()
        if account is None:
            raise NotFoundError(
                detail=f"Account with id {account_public_id} not found in this workspace"
            )
        return account

    async def _get_statement(
        self, workspace_id: int, account_id: int, statement_public_id: uuid.UUID
    ) -> AccountStatement:
        statement = (
            await self.session.execute(
                select(AccountStatement).where(
                    AccountStatement.workspace_id == workspace_id,
                    AccountStatement.account_id == account_id,
                    AccountStatement.public_id == statement_public_id,
                )
            )
        ).scalar_one_or_none()
        if statement is None:
            raise NotFoundError(
                detail=f"Statement with id {statement_public_id} not found for this account"
            )
        return statement

    async def list_statements(
        self, workspace_id: int, account_public_id: uuid.UUID
    ) -> list[AccountStatement]:
        account = await self._get_account(workspace_id, account_public_id)
        rows = (
            (
                await self.session.execute(
                    select(AccountStatement)
                    .where(
                        AccountStatement.workspace_id == workspace_id,
                        AccountStatement.account_id == account.id,
                    )
                    .order_by(AccountStatement.period_start.desc())
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def _candidate_transactions(
        self, workspace_id: int, account_id: int, occurred_at: date, amount: Decimal
    ) -> list[SpendingTransaction]:
        window_start = occurred_at - timedelta(days=MATCH_WINDOW_DAYS)
        window_end = occurred_at + timedelta(days=MATCH_WINDOW_DAYS)
        tx_type = TransactionType.income if amount > 0 else TransactionType.expense
        rows = (
            (
                await self.session.execute(
                    select(SpendingTransaction).where(
                        SpendingTransaction.workspace_id == workspace_id,
                        SpendingTransaction.account_id == account_id,
                        SpendingTransaction.type == tx_type,
                        SpendingTransaction.amount == abs(amount),
                        SpendingTransaction.occurred_at
                        >= datetime.combine(window_start, datetime.min.time(), tzinfo=UTC),
                        SpendingTransaction.occurred_at
                        < datetime.combine(
                            window_end + timedelta(days=1), datetime.min.time(), tzinfo=UTC
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def _candidate_transfers(
        self, workspace_id: int, account_id: int, occurred_at: date, amount: Decimal
    ) -> list[tuple[CapitalTransfer, StatementLineMatchLeg]]:
        window_start = datetime.combine(
            occurred_at - timedelta(days=MATCH_WINDOW_DAYS), datetime.min.time(), tzinfo=UTC
        )
        window_end = datetime.combine(
            occurred_at + timedelta(days=MATCH_WINDOW_DAYS + 1), datetime.min.time(), tzinfo=UTC
        )
        results: list[tuple[CapitalTransfer, StatementLineMatchLeg]] = []
        if amount < 0:
            rows = (
                (
                    await self.session.execute(
                        select(CapitalTransfer).where(
                            CapitalTransfer.workspace_id == workspace_id,
                            CapitalTransfer.from_account_id == account_id,
                            CapitalTransfer.gross_amount == abs(amount),
                            CapitalTransfer.occurred_at >= window_start,
                            CapitalTransfer.occurred_at < window_end,
                        )
                    )
                )
                .scalars()
                .all()
            )
            results.extend((t, StatementLineMatchLeg.from_leg) for t in rows)
        if amount > 0:
            rows = (
                (
                    await self.session.execute(
                        select(CapitalTransfer).where(
                            CapitalTransfer.workspace_id == workspace_id,
                            CapitalTransfer.to_account_id == account_id,
                            CapitalTransfer.net_amount_received == amount,
                            CapitalTransfer.occurred_at >= window_start,
                            CapitalTransfer.occurred_at < window_end,
                        )
                    )
                )
                .scalars()
                .all()
            )
            results.extend((t, StatementLineMatchLeg.to_leg) for t in rows)
        return results

    async def line_to_dict(self, line: StatementLine) -> dict:
        """Resolve a StatementLine's internal `matched_*_id` FKs (int) to the
        public UUIDs the API surfaces — never expose internal ids."""
        matched_transaction_public_id = None
        if line.matched_transaction_id is not None:
            matched_transaction_public_id = (
                await self.session.execute(
                    select(SpendingTransaction.public_id).where(
                        SpendingTransaction.id == line.matched_transaction_id
                    )
                )
            ).scalar_one_or_none()
        matched_transfer_public_id = None
        if line.matched_transfer_id is not None:
            matched_transfer_public_id = (
                await self.session.execute(
                    select(CapitalTransfer.public_id).where(
                        CapitalTransfer.id == line.matched_transfer_id
                    )
                )
            ).scalar_one_or_none()
        return {
            "public_id": line.public_id,
            "occurred_at": line.occurred_at,
            "description": line.description,
            "amount": line.amount,
            "balance": line.balance,
            "matched_transaction_id": matched_transaction_public_id,
            "matched_transfer_id": matched_transfer_public_id,
            "matched_transfer_leg": line.matched_transfer_leg,
            "matched_at": line.matched_at,
        }

    async def get_reconciliation_view(
        self, workspace_id: int, account_public_id: uuid.UUID, statement_public_id: uuid.UUID
    ) -> dict:
        account = await self._get_account(workspace_id, account_public_id)
        statement = await self._get_statement(workspace_id, account.id, statement_public_id)

        lines = (
            (
                await self.session.execute(
                    select(StatementLine)
                    .where(StatementLine.statement_id == statement.id)
                    .order_by(StatementLine.occurred_at)
                )
            )
            .scalars()
            .all()
        )

        # Workspace-wide, not just this statement's lines: an event already
        # matched on a different statement must not be suggested/counted as
        # unmatched again here. A transfer is keyed by (id, leg) since each
        # leg can legitimately match a different statement line.
        already_matched_tx_ids = set(
            (
                await self.session.execute(
                    select(StatementLine.matched_transaction_id).where(
                        StatementLine.workspace_id == workspace_id,
                        StatementLine.matched_transaction_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        already_matched_transfer_legs = set(
            (
                await self.session.execute(
                    select(
                        StatementLine.matched_transfer_id, StatementLine.matched_transfer_leg
                    ).where(
                        StatementLine.workspace_id == workspace_id,
                        StatementLine.matched_transfer_id.is_not(None),
                    )
                )
            ).all()
        )

        matched_lines = [
            await self.line_to_dict(line)
            for line in lines
            if line.matched_transaction_id or line.matched_transfer_id
        ]
        unmatched_lines_out = []
        for line in lines:
            if line.matched_transaction_id or line.matched_transfer_id:
                continue
            candidates = []
            for tx in await self._candidate_transactions(
                workspace_id, account.id, line.occurred_at, line.amount
            ):
                if tx.id in already_matched_tx_ids:
                    continue
                candidates.append({
                    "kind": "transaction",
                    "id": tx.public_id,
                    "occurred_at": tx.occurred_at.astimezone(UTC).date(),
                    "amount": _signed_transaction_amount(tx),
                    "description": tx.description or "",
                    "leg": None,
                })
            for transfer, leg in await self._candidate_transfers(
                workspace_id, account.id, line.occurred_at, line.amount
            ):
                if (transfer.id, leg) in already_matched_transfer_legs:
                    continue
                candidates.append({
                    "kind": "transfer",
                    "id": transfer.public_id,
                    "occurred_at": transfer.occurred_at.astimezone(UTC).date(),
                    "amount": _signed_transfer_amount(transfer, leg),
                    "description": transfer.notes or "",
                    "leg": leg,
                })
            unmatched_lines_out.append({"line": line, "candidates": candidates})

        # Unmatched ledger rows in the period: events that could plausibly
        # appear on a statement but no statement line references them.
        period_start_dt = datetime.combine(statement.period_start, datetime.min.time(), tzinfo=UTC)
        period_end_dt = datetime.combine(
            statement.period_end + timedelta(days=1), datetime.min.time(), tzinfo=UTC
        )
        tx_rows = (
            (
                await self.session.execute(
                    select(SpendingTransaction).where(
                        SpendingTransaction.workspace_id == workspace_id,
                        SpendingTransaction.account_id == account.id,
                        SpendingTransaction.occurred_at >= period_start_dt,
                        SpendingTransaction.occurred_at < period_end_dt,
                    )
                )
            )
            .scalars()
            .all()
        )
        unmatched_ledger_rows = [
            {
                "kind": "transaction",
                "id": tx.public_id,
                "occurred_at": tx.occurred_at.astimezone(UTC).date(),
                "amount": _signed_transaction_amount(tx),
                "description": tx.description or "",
                "leg": None,
            }
            for tx in tx_rows
            if tx.id not in already_matched_tx_ids
        ]
        transfer_rows = (
            (
                await self.session.execute(
                    select(CapitalTransfer).where(
                        CapitalTransfer.workspace_id == workspace_id,
                        (CapitalTransfer.from_account_id == account.id)
                        | (CapitalTransfer.to_account_id == account.id),
                        CapitalTransfer.occurred_at >= period_start_dt,
                        CapitalTransfer.occurred_at < period_end_dt,
                    )
                )
            )
            .scalars()
            .all()
        )
        for transfer in transfer_rows:
            if (
                transfer.from_account_id == account.id
                and (transfer.id, StatementLineMatchLeg.from_leg)
                not in already_matched_transfer_legs
            ):
                unmatched_ledger_rows.append({
                    "kind": "transfer",
                    "id": transfer.public_id,
                    "occurred_at": transfer.occurred_at.astimezone(UTC).date(),
                    "amount": _signed_transfer_amount(transfer, StatementLineMatchLeg.from_leg),
                    "description": transfer.notes or "",
                    "leg": StatementLineMatchLeg.from_leg,
                })
            if (
                transfer.to_account_id == account.id
                and (transfer.id, StatementLineMatchLeg.to_leg) not in already_matched_transfer_legs
            ):
                unmatched_ledger_rows.append({
                    "kind": "transfer",
                    "id": transfer.public_id,
                    "occurred_at": transfer.occurred_at.astimezone(UTC).date(),
                    "amount": _signed_transfer_amount(transfer, StatementLineMatchLeg.to_leg),
                    "description": transfer.notes or "",
                    "leg": StatementLineMatchLeg.to_leg,
                })

        return {
            "statement": statement,
            "matched_lines": matched_lines,
            "unmatched_lines": unmatched_lines_out,
            "unmatched_ledger_rows": unmatched_ledger_rows,
        }

    async def _maybe_mark_reconciled(self, statement: AccountStatement) -> None:
        remaining = (
            await self.session.execute(
                select(StatementLine.id).where(
                    StatementLine.statement_id == statement.id,
                    StatementLine.matched_transaction_id.is_(None),
                    StatementLine.matched_transfer_id.is_(None),
                )
            )
        ).first()
        statement.reconciled_through = statement.period_end if remaining is None else None
        statement.updated_at = datetime.now(UTC)
        self.session.add(statement)

    async def confirm_match(
        self,
        workspace_id: int,
        account_public_id: uuid.UUID,
        statement_public_id: uuid.UUID,
        line_public_id: uuid.UUID,
        *,
        transaction_id: uuid.UUID | None,
        transfer_id: uuid.UUID | None,
        leg: StatementLineMatchLeg | None,
    ) -> StatementLine:
        if (transaction_id is None) == (transfer_id is None):
            raise ValidationError(detail="Exactly one of transaction_id/transfer_id is required")
        if transfer_id is not None and leg is None:
            raise ValidationError(detail="leg is required when matching a transfer")

        account = await self._get_account(workspace_id, account_public_id)
        statement = await self._get_statement(workspace_id, account.id, statement_public_id)
        line = await self._get_line(statement.id, line_public_id)
        if line.matched_transaction_id or line.matched_transfer_id:
            raise ValidationError(detail="Statement line is already matched")

        if transaction_id is not None:
            tx = (
                await self.session.execute(
                    select(SpendingTransaction).where(
                        SpendingTransaction.workspace_id == workspace_id,
                        SpendingTransaction.public_id == transaction_id,
                        SpendingTransaction.account_id == account.id,
                    )
                )
            ).scalar_one_or_none()
            if tx is None:
                raise NotFoundError(detail=f"Transaction with id {transaction_id} not found")
            await self._check_not_already_matched(matched_transaction_id=tx.id)
            line.matched_transaction_id = tx.id
        else:
            transfer = (
                await self.session.execute(
                    select(CapitalTransfer).where(
                        CapitalTransfer.workspace_id == workspace_id,
                        CapitalTransfer.public_id == transfer_id,
                        (CapitalTransfer.from_account_id == account.id)
                        | (CapitalTransfer.to_account_id == account.id),
                    )
                )
            ).scalar_one_or_none()
            if transfer is None:
                raise NotFoundError(detail=f"Transfer with id {transfer_id} not found")
            expected_account_id = (
                transfer.from_account_id
                if leg == StatementLineMatchLeg.from_leg
                else transfer.to_account_id
            )
            if expected_account_id != account.id:
                raise ValidationError(
                    detail=f"Transfer does not have a {leg.value}-leg on this account"
                )
            await self._check_not_already_matched(
                matched_transfer_id=transfer.id, matched_transfer_leg=leg
            )
            line.matched_transfer_id = transfer.id
            line.matched_transfer_leg = leg

        line.matched_at = datetime.now(UTC)
        line.updated_at = datetime.now(UTC)
        self.session.add(line)
        await self.session.flush()

        await self._maybe_mark_reconciled(statement)
        await self.session.flush()
        await self.session.refresh(line)
        return line

    async def unmatch_line(
        self,
        workspace_id: int,
        account_public_id: uuid.UUID,
        statement_public_id: uuid.UUID,
        line_public_id: uuid.UUID,
    ) -> StatementLine:
        account = await self._get_account(workspace_id, account_public_id)
        statement = await self._get_statement(workspace_id, account.id, statement_public_id)
        line = await self._get_line(statement.id, line_public_id)
        self._clear_match(line)
        self.session.add(line)
        await self.session.flush()
        await self._maybe_mark_reconciled(statement)
        await self.session.flush()
        await self.session.refresh(line)
        return line

    async def _check_not_already_matched(
        self,
        *,
        matched_transaction_id: int | None = None,
        matched_transfer_id: int | None = None,
        matched_transfer_leg: StatementLineMatchLeg | None = None,
    ) -> None:
        """Workspace-wide, not statement-scoped: a transaction must match at
        most one statement line, even across different statements. A
        transfer may legitimately match twice — once per leg (from-side on
        one account's statement, to-side on the other's) — so the transfer
        check is scoped to the same leg only."""
        if matched_transaction_id is not None:
            existing = (
                await self.session.execute(
                    select(StatementLine.id).where(
                        StatementLine.matched_transaction_id == matched_transaction_id
                    )
                )
            ).first()
            if existing is not None:
                raise ValidationError(
                    detail="Transaction is already matched to another statement line"
                )
        if matched_transfer_id is not None:
            existing = (
                await self.session.execute(
                    select(StatementLine.id).where(
                        StatementLine.matched_transfer_id == matched_transfer_id,
                        StatementLine.matched_transfer_leg == matched_transfer_leg,
                    )
                )
            ).first()
            if existing is not None:
                raise ValidationError(
                    detail=f"Transfer's {matched_transfer_leg} leg is already matched to another statement line"
                )

    async def _get_line(self, statement_id: int, line_public_id: uuid.UUID) -> StatementLine:
        line = (
            await self.session.execute(
                select(StatementLine).where(
                    StatementLine.statement_id == statement_id,
                    StatementLine.public_id == line_public_id,
                )
            )
        ).scalar_one_or_none()
        if line is None:
            raise NotFoundError(detail=f"Statement line with id {line_public_id} not found")
        return line

    @staticmethod
    def _clear_match(line: StatementLine) -> None:
        line.matched_transaction_id = None
        line.matched_transfer_id = None
        line.matched_transfer_leg = None
        line.matched_at = None
        line.updated_at = datetime.now(UTC)

    async def break_matches_for_transaction(self, workspace_id: int, transaction_id: int) -> None:
        """Owner decision (spec-078): a breaking edit to a matched ledger row
        clears the match link and flags the period unreconciled. Called from
        `SpendingTransactionService` on update/delete of a matched
        transaction — never the reverse (INV-1: this module never writes to
        spending_transactions)."""
        if transaction_id is None:
            # `matched_transaction_id == None` would compile to `IS NULL` and
            # match every unmatched line — fail fast instead of bulk-clearing.
            raise ValueError("transaction_id cannot be None")
        lines = (
            (
                await self.session.execute(
                    select(StatementLine).where(
                        StatementLine.workspace_id == workspace_id,
                        StatementLine.matched_transaction_id == transaction_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        await self._break_lines(lines)

    async def break_matches_for_transfer(self, workspace_id: int, transfer_id: int) -> None:
        if transfer_id is None:
            raise ValueError("transfer_id cannot be None")
        lines = (
            (
                await self.session.execute(
                    select(StatementLine).where(
                        StatementLine.workspace_id == workspace_id,
                        StatementLine.matched_transfer_id == transfer_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        await self._break_lines(lines)

    async def _break_lines(self, lines: list[StatementLine]) -> None:
        if not lines:
            return
        statement_ids = {line.statement_id for line in lines}
        for line in lines:
            self._clear_match(line)
            self.session.add(line)
        await self.session.flush()
        statements = (
            (
                await self.session.execute(
                    select(AccountStatement).where(AccountStatement.id.in_(statement_ids))
                )
            )
            .scalars()
            .all()
        )
        for statement in statements:
            statement.reconciled_through = None
            statement.updated_at = datetime.now(UTC)
            self.session.add(statement)
        await self.session.flush()
