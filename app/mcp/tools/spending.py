"""Spending budgets and spending transactions (single and batch)."""

from typing import Any

from fastmcp import FastMCP

from app.core.database import postgres
from app.mcp import security
from app.mcp.support import common
from app.spending.repository import BudgetRepository, CategoryGroupRepository, CategoryRepository
from app.spending.service import BudgetService


def register(mcp: FastMCP) -> None:
    """Register the spending tools on ``mcp``."""

    @mcp.tool
    async def get_spending_budgets(workspace_id: int) -> dict[str, Any]:
        """Get budgets and spending details for an authorized workspace."""
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_spending_budgets"
            )
            service = BudgetService(
                BudgetRepository(session),
                CategoryRepository(session),
                CategoryGroupRepository(session),
            )
            budgets, total = await service.list_budgets_with_details(workspace_id, limit=100)
            return {
                "workspace_id": workspace_id,
                "budgets": [b.model_dump() for b in budgets],
                "total": total,
            }

    @mcp.tool
    async def find_spending_transactions(
        workspace_id: int,
        from_day: str | None = None,
        to_day: str | None = None,
        category_name: str | None = None,
        amount: str | None = None,
        search: str | None = None,
        account_name: str | None = None,
        transaction_type: str | None = "expense",
        limit: int = 10,
        from_date: str | None = None,
        to_date: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Find transaction candidates in an authorized workspace.

        ``from_date``/``to_date`` (YYYY-MM-DD) are inclusive workspace-local calendar
        days (``from_day``/``to_day`` are aliases). Returns ``total_count`` and
        ``next_cursor``; pass it back as ``cursor`` for the next page.
        """
        return await common.run_capture_tool(
            workspace_id,
            "mcp:read",
            "find_spending_transactions",
            {
                "from_date": from_date,
                "to_date": to_date,
                "cursor": cursor,
                "from_day": from_day,
                "to_day": to_day,
                "category_name": category_name,
                "amount": amount,
                "search": search,
                "account_name": account_name,
                "transaction_type": transaction_type,
                "limit": limit,
            },
        )

    @mcp.tool
    async def update_spending_transaction(
        workspace_id: int,
        public_id: str,
        amount: str | None = None,
        category_name: str | None = None,
        description: str | None = None,
        account_name: str | None = None,
        occurred_at: str | None = None,
        tags: list[str] | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Update one spending transaction after explicit confirmation."""
        return await common.run_capture_tool(
            workspace_id,
            "mcp:write",
            "update_spending_transaction",
            {
                "public_id": public_id,
                "amount": amount,
                "category_name": category_name,
                "description": description,
                "account_name": account_name,
                "occurred_at": occurred_at,
                "tags": tags,
                "confirmed": confirmed,
            },
        )

    @mcp.tool
    async def delete_spending_transaction(
        workspace_id: int, public_id: str, confirmed: bool = False
    ) -> dict[str, Any]:
        """Delete one spending transaction after explicit confirmation."""
        return await common.run_capture_tool(
            workspace_id,
            "mcp:write",
            "delete_spending_transaction",
            {"public_id": public_id, "confirmed": confirmed},
        )

    @mcp.tool
    async def log_spending_transaction(
        workspace_id: int,
        amount: str,
        category_name: str,
        description: str | None = None,
        account_name: str | None = None,
        occurred_at: str | None = None,
        tags: list[str] | None = None,
        allow_duplicate: bool = False,
        transaction_type: str = "expense",
        source_ref: str | None = None,
        dry_run: bool = False,
        skip_duplicates: bool = False,
    ) -> dict[str, Any]:
        """Create a transaction (expense or income) in an authorized workspace.

        A repeated ``source_ref`` returns the existing transaction (idempotent).
        ``dry_run=true`` writes nothing and returns ``{counts, items:[{index,
        status, match_reason, existing_id, errors}]}``. ``skip_duplicates=true``
        reports a same-day/amount/account/description match as a duplicate
        instead of refusing.
        """
        return await common.run_capture_tool(
            workspace_id,
            "mcp:write",
            "log_spending_transaction",
            {
                "dry_run": dry_run,
                "skip_duplicates": skip_duplicates,
                "amount": amount,
                "category_name": category_name,
                "description": description,
                "account_name": account_name,
                "occurred_at": occurred_at,
                "tags": tags,
                "allow_duplicate": allow_duplicate,
                "transaction_type": transaction_type,
                "source_ref": source_ref,
            },
        )

    @mcp.tool
    async def log_spending_transactions_batch(
        workspace_id: int,
        items: list[dict[str, Any]],
        atomic: bool = False,
        dry_run: bool = False,
        skip_duplicates: bool = False,
    ) -> dict[str, Any]:
        """Create up to 50 transactions idempotently in an authorized workspace.

        Each item: {amount, category_name, description?, account_name?, occurred_at?,
        tags?, transaction_type?, source_ref?, allow_duplicate?}. An exact
        ``source_ref`` match returns the existing record. Items without one are matched
        on local day + amount + account + normalised description, multiplicity-aware
        (the ledger already holding 1 of 2 identical purchases creates 1, skips 1):
        ``duplicate`` with ``skip_duplicates=true``, else ``possible_duplicate`` (not
        written). Default is per-item partial success; ``atomic=true`` is all-or-nothing.
        ``dry_run=true`` writes nothing and returns the same structure with
        ``would_create``. Returns ``{counts, items:[{index, status, match_reason,
        existing_id, errors, warnings}]}``.
        """
        return await common.run_capture_tool(
            workspace_id,
            "mcp:write",
            "log_spending_transactions_batch",
            {
                "items": items,
                "atomic": atomic,
                "dry_run": dry_run,
                "skip_duplicates": skip_duplicates,
            },
        )

    @mcp.tool
    async def list_spending_transactions(
        workspace_id: int,
        day: str | None = None,
        category_name: str | None = None,
        amount: str | None = None,
        search: str | None = None,
        account_name: str | None = None,
        transaction_type: str | None = "expense",
        limit: int = 10,
        from_date: str | None = None,
        to_date: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List transactions for a local day or inclusive date range in an authorized workspace.

        ``from_date``/``to_date`` (YYYY-MM-DD) are inclusive workspace-local calendar
        days. Returns ``total_count`` and ``next_cursor``; pass it back as ``cursor``
        for the next page.
        """
        return await common.run_capture_tool(
            workspace_id,
            "mcp:read",
            "list_spending_transactions",
            {
                "from_date": from_date,
                "to_date": to_date,
                "cursor": cursor,
                "day": day,
                "category_name": category_name,
                "amount": amount,
                "search": search,
                "account_name": account_name,
                "transaction_type": transaction_type,
                "limit": limit,
            },
        )
