"""Workspace discovery, reference data (tool + resources) and the audit trail."""

import decimal
import json
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from pydantic import ValidationError as PydanticValidationError

from app.auth.models import User
from app.auth.repository import UserRepository
from app.core.audit import AuditLogRepository, redact_details
from app.core.database import postgres
from app.finance.models import AccountType
from app.finance.repository import AccountRepository, FinanceSettingRepository
from app.health.repository import MedicationRepository
from app.mcp import security
from app.mcp.support import common
from app.platform.repository import MembershipRepository, WorkspaceRepository
from app.spending.repository import CategoryRepository, TagRepository


async def _load_workspace_reference_data(
    session: Any, workspace_id: int, user_id: int
) -> dict[str, Any]:
    """Load the bounded, read-only vocabulary shared by MCP and voice clients."""
    user = await session.get(User, user_id)
    categories, _ = await CategoryRepository(session).get_all(workspace_id, limit=50, offset=0)
    spending_tags, _ = await TagRepository(session).get_all(workspace_id, limit=50, offset=0)
    accounts, _ = await AccountRepository(session).list_workspace_accounts(
        workspace_id, limit=20, offset=0
    )
    setting = await FinanceSettingRepository(session).get_by_workspace(workspace_id)
    medications = await MedicationRepository(session).get_active(workspace_id)
    default_account_id = setting.default_spending_account_id if setting else None
    return {
        "workspace_id": workspace_id,
        "timezone": user.timezone if user and user.timezone else "UTC",
        "categories": [
            {"public_id": str(category.public_id), "name": category.name} for category in categories
        ],
        "accounts": [
            {
                "public_id": str(account.public_id),
                "name": account.name,
                # Stored as a plain string column, so it is not always an Enum instance.
                "account_type": AccountType(account.account_type).value,
                "currency_code": account.default_currency_code,
                "is_default_spending_account": account.id == default_account_id,
            }
            for account in accounts
            if account.is_active and account.account_type != AccountType.brokerage
        ],
        "tags": [{"public_id": str(tag.public_id), "name": tag.name} for tag in spending_tags],
        "medications": [
            {"public_id": str(medication.public_id), "name": medication.name}
            for medication in medications[:30]
        ],
    }


async def _load_workspace_choices(session: Any, user_id: int) -> list[dict[str, Any]]:
    """List only active workspaces the authenticated MCP user can use."""
    workspaces = await WorkspaceRepository(session).list_user_workspaces(user_id)
    memberships = await MembershipRepository(session).list_user_memberships(user_id)
    roles = {
        membership.workspace_id: (
            membership.role.value if hasattr(membership.role, "value") else str(membership.role)
        )
        for membership in memberships
    }

    token = get_access_token()
    selected_workspace_id: int | None = None
    if token is not None:
        raw_selected = (token.claims or {}).get("default_workspace_id")
        try:
            selected_workspace_id = int(raw_selected) if raw_selected is not None else None
        except (TypeError, ValueError, decimal.InvalidOperation, PydanticValidationError):
            selected_workspace_id = None

    active_workspaces = [workspace for workspace in workspaces if workspace.is_active]
    if selected_workspace_id is None and len(active_workspaces) == 1:
        selected_workspace_id = active_workspaces[0].id

    return [
        {
            "workspace_id": workspace.id,
            "public_id": str(workspace.public_id),
            "name": workspace.name,
            "description": workspace.description,
            "role": roles.get(workspace.id),
            "is_current": workspace.id == selected_workspace_id,
        }
        for workspace in active_workspaces
    ]


def register(mcp: FastMCP) -> None:
    """Register the workspace tools and resources on ``mcp``."""

    @mcp.tool
    async def get_workspace_reference_data(workspace_id: int) -> dict[str, Any]:
        """Get bounded workspace vocabulary for safe follow-up tool calls.

        This is read-only and excludes brokerage accounts. It gives MCP clients
        the same categories, spending accounts, tags, active medications, and
        persisted timezone that the voice surface uses for disambiguation.
        """
        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="get_workspace_reference_data",
            )
            return await _load_workspace_reference_data(session, workspace_id, user_id)

    @mcp.tool
    async def list_workspaces() -> dict[str, Any]:
        """List active workspaces available to the authenticated MCP user.

        Call this before workspace-scoped tools when the workspace is not already
        supplied by the host application. Never guess a workspace identifier.
        """
        user_id = security.authorize_user(required_scope="mcp:read", tool="list_workspaces")
        async with postgres.async_session_maker() as session:
            workspaces = await _load_workspace_choices(session, user_id)
            return {"workspaces": workspaces}

    @mcp.resource(
        "lifestack://me/workspaces",
        name="my-workspaces",
        description="Active workspaces available to the authenticated MCP user.",
        mime_type="application/json",
    )
    async def my_workspaces() -> str:
        """Expose workspace discovery as a passive authenticated resource."""
        user_id = security.authorize_user(required_scope="mcp:read", tool="my_workspaces")
        async with postgres.async_session_maker() as session:
            workspaces = await _load_workspace_choices(session, user_id)
            return json.dumps({"workspaces": workspaces}, separators=(",", ":"))

    @mcp.resource(
        "lifestack://workspaces/{workspace_id}/reference-data",
        name="workspace-reference-data",
        description=(
            "Bounded read-only workspace vocabulary for categories, spending accounts, "
            "tags, active medications, and timezone."
        ),
        mime_type="application/json",
    )
    async def workspace_reference_data(workspace_id: int) -> str:
        """Return authenticated workspace context as a JSON MCP resource."""
        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session,
                workspace_id,
                required_scope="mcp:read",
                tool="workspace_reference_data",
            )
            data = await _load_workspace_reference_data(session, workspace_id, user_id)
            return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @mcp.tool
    async def list_recent_changes(
        workspace_id: int,
        limit: int = 25,
        offset: int = 0,
        module: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        entity_public_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Paginated, filterable read of the append-only audit log (newest first).

        Each row has the actor's name, the entity, and the redacted
        ``details.before`` / ``details.after`` / ``details.changed_fields`` --
        enough to verify a write or to reconstruct an undo. Secrets (passwords,
        tokens, API keys, hashes, ...) are redacted. ``limit`` is capped at 100.
        ``since``/``until`` are ISO-8601 (naive values are read as UTC).
        Read-only.
        """
        security.validate_page(limit, offset)
        try:
            since_dt = common.parse_iso_datetime(since, "since")
            until_dt = common.parse_iso_datetime(until, "until")
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        if since_dt and until_dt and since_dt > until_dt:
            return {"status": "error", "message": "since must not be after until."}
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_recent_changes"
            )
            rows, total = await AuditLogRepository(session).list_by_workspace(
                workspace_id,
                module=module,
                action=action,
                entity_type=entity_type,
                entity_public_id=entity_public_id,
                since=since_dt,
                until=until_dt,
                limit=limit,
                offset=offset,
            )
            actors = await UserRepository(session).get_summaries_by_ids([
                r.actor_id for r in rows if r.actor_id is not None
            ])
        items = []
        for r in rows:
            details = redact_details(r.details or {})
            actor = actors.get(r.actor_id)
            items.append({
                "public_id": str(r.public_id),
                "timestamp": r.timestamp.isoformat(),
                "module": r.module,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_public_id": details.get("entity_public_id"),
                "actor_public_id": str(actor[0]) if actor else None,
                "actor_name": actor[1] if actor else None,
                "details": details,
            })
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }
