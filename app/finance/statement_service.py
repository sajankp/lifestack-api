"""Spec-078: statement lookup and match-reference maintenance.

The statement *import* (CSV bank statement -> transactions) writes the
`account_statements` / `statement_lines` rows; this module reads them back
and clears a statement line's match reference when the ledger row it
points at is edited or deleted.

INV-1 (matching is metadata, never mutation): every write here touches only
`statement_lines.matched_*` columns. Nothing in this module ever creates,
edits, or deletes a `spending_transactions` or `capital_transfers` row.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.finance.models import Account
from app.finance.statement_models import AccountStatement, StatementLine


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
