from app.platform.models import Workspace, WorkspaceMembership, WorkspaceRole
from app.platform.repository import MembershipRepository, WorkspaceRepository


class WorkspaceService:
    def __init__(self, workspace_repo: WorkspaceRepository, membership_repo: MembershipRepository):
        self.workspace_repo = workspace_repo
        self.membership_repo = membership_repo

    async def create_workspace(self, name: str, user_id: int) -> Workspace:
        """Create a new workspace and assign the user as OWNER."""
        workspace = Workspace(name=name)
        new_workspace = await self.workspace_repo.create(workspace)

        membership = WorkspaceMembership(
            workspace_id=new_workspace.id, user_id=user_id, role=WorkspaceRole.OWNER
        )
        await self.membership_repo.create(membership)

        return new_workspace

    async def get_user_workspaces(self, user_id: int) -> list[Workspace]:
        return await self.workspace_repo.list_user_workspaces(user_id)

    async def ensure_default_workspace(self, user_id: int, username: str) -> Workspace:
        """Ensure a user has at least one workspace. Create one if not."""
        workspaces = await self.get_user_workspaces(user_id)
        if workspaces:
            return workspaces[0]

        return await self.create_workspace(name=f"{username}'s Workspace", user_id=user_id)
