import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.models import McpGrant


class McpGrantRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_user_and_client(self, user_id: int, client_id: str) -> McpGrant | None:
        result = await self.session.execute(
            select(McpGrant).where(McpGrant.user_id == user_id, McpGrant.client_id == client_id)
        )
        return result.scalar_one_or_none()

    async def upsert(
        self, user_id: int, client_id: str, client_name: str, scopes: list[str]
    ) -> McpGrant:
        grant = await self.get_by_user_and_client(user_id, client_id)
        now = datetime.now(UTC)
        if grant is None:
            grant = McpGrant(
                user_id=user_id,
                client_id=client_id,
                client_name=client_name,
                scopes=scopes,
                last_used_at=now,
            )
            self.session.add(grant)
        else:
            grant.client_name = client_name
            grant.scopes = scopes
            grant.revoked_at = None
            grant.last_used_at = now
            grant.updated_at = now
            self.session.add(grant)
        await self.session.flush()
        await self.session.refresh(grant)
        return grant

    async def list_active_for_user(self, user_id: int) -> list[McpGrant]:
        result = await self.session.execute(
            select(McpGrant)
            .where(McpGrant.user_id == user_id, McpGrant.revoked_at.is_(None))
            .order_by(McpGrant.last_used_at.desc().nullslast(), McpGrant.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_active_by_public_id(self, user_id: int, public_id: uuid.UUID) -> McpGrant | None:
        result = await self.session.execute(
            select(McpGrant).where(
                McpGrant.user_id == user_id,
                McpGrant.public_id == public_id,
                McpGrant.revoked_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_active_by_public_id_unscoped(self, public_id: uuid.UUID) -> McpGrant | None:
        result = await self.session.execute(
            select(McpGrant).where(
                McpGrant.public_id == public_id,
                McpGrant.revoked_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def touch(self, grant: McpGrant) -> None:
        grant.last_used_at = datetime.now(UTC)
        grant.updated_at = grant.last_used_at
        self.session.add(grant)
        await self.session.flush()

    async def revoke(self, grant: McpGrant) -> None:
        grant.revoked_at = datetime.now(UTC)
        grant.updated_at = grant.revoked_at
        self.session.add(grant)
        await self.session.flush()

    async def revoke_all_for_user(self, user_id: int) -> int:
        grants = await self.list_active_for_user(user_id)
        for grant in grants:
            await self.revoke(grant)
        return len(grants)
