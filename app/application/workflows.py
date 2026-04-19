from app.auth.schemas import UserCreate
from app.auth.service import AuthService
from app.platform.service import WorkspaceService
from app.spending.service import CategoryService


class UserRegistrationWorkflow:
    def __init__(
        self,
        auth_service: AuthService,
        workspace_service: WorkspaceService,
        category_service: CategoryService,
    ):
        self.auth_service = auth_service
        self.workspace_service = workspace_service
        self.category_service = category_service

    async def register_user_with_workspace(self, user_in: UserCreate) -> bool:
        """Register a user, provision their default workspace, and seed system spending categories."""
        user = await self.auth_service.register_user(user_in)

        # Provision default workspace
        workspace = await self.workspace_service.ensure_default_workspace(
            user_id=user.id, username=user.username
        )

        # Atomically seed default spending categories for the new workspace
        await self.category_service.provision_default_categories(workspace.id)  # type: ignore[arg-type]

        return True
