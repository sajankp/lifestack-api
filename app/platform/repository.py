import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.platform.models import Workspace, WorkspaceMembership


class WorkspaceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, workspace: Workspace) -> Workspace:
        self.session.add(workspace)
        await self.session.flush()
        await self.session.refresh(workspace)
        return workspace

    async def get_by_id(self, workspace_id: int) -> Workspace | None:
        result = await self.session.execute(select(Workspace).where(Workspace.id == workspace_id))
        return result.scalar_one_or_none()

    async def get_by_public_id(self, public_id: uuid.UUID) -> Workspace | None:
        result = await self.session.execute(
            select(Workspace).where(Workspace.public_id == public_id)
        )
        return result.scalar_one_or_none()

    async def list_user_workspaces(self, user_id: int) -> list[Workspace]:
        result = await self.session.execute(
            select(Workspace)
            .join(WorkspaceMembership)
            .where(WorkspaceMembership.user_id == user_id)
        )
        return list(result.scalars().all())


class MembershipRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, membership: WorkspaceMembership) -> WorkspaceMembership:
        self.session.add(membership)
        await self.session.flush()
        await self.session.refresh(membership)
        return membership

    async def get_membership(self, workspace_id: int, user_id: int) -> WorkspaceMembership | None:
        result = await self.session.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()
