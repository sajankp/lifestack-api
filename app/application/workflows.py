from app.auth.schemas import UserCreate
from app.auth.service import AuthService
from app.platform.service import WorkspaceService


class UserRegistrationWorkflow:
    def __init__(self, auth_service: AuthService, workspace_service: WorkspaceService):
        self.auth_service = auth_service
        self.workspace_service = workspace_service

    async def register_user_with_workspace(self, user_in: UserCreate) -> bool:
        """Register a user and provision their default workspace."""
        user = await self.auth_service.register_user(user_in)

        # Provision default workspace
        await self.workspace_service.ensure_default_workspace(
            user_id=user.id, username=user.username
        )

        return True
