import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.pagination import DEFAULT_LIMIT
from app.finance.models import Account
from app.finance.repository import AccountRepository, CurrencyRepository
from app.finance.schemas import AccountCreate, AccountUpdate


class CurrencyService:
    def __init__(self, repository: CurrencyRepository):
        self.repository = repository

    async def list_workspace_currencies(self, workspace_id: int) -> Sequence:
        return await self.repository.list_workspace_enabled(workspace_id)

    async def validate_supported_code(self, code: str) -> None:
        currency = await self.repository.get_by_code(code)
        if not currency or not currency.is_active:
            raise ValidationError(detail=f"Unsupported currency code '{code}'")


class AccountService:
    def __init__(
        self,
        account_repository: AccountRepository,
        currency_repository: CurrencyRepository,
    ):
        self.account_repository = account_repository
        self.currency_repository = currency_repository

    async def list_accounts(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[Sequence[Account], int]:
        return await self.account_repository.list_workspace_accounts(workspace_id, limit, offset)

    async def get_account(self, workspace_id: int, public_id: uuid.UUID) -> Account:
        account = await self.account_repository.get_by_public_id(workspace_id, public_id)
        if not account:
            raise NotFoundError(detail=f"Account with id {public_id} not found in this workspace")
        return account

    async def create_account(self, workspace_id: int, account_in: AccountCreate) -> Account:
        existing = await self.account_repository.get_by_name(workspace_id, account_in.name)
        if existing:
            raise ConflictError(detail=f"Account named '{account_in.name}' already exists")

        currency = await self.currency_repository.get_by_code(account_in.default_currency_code)
        if not currency or not currency.is_active:
            raise ValidationError(
                detail=f"Unsupported default currency '{account_in.default_currency_code}'"
            )

        account = Account(
            workspace_id=workspace_id,
            name=account_in.name,
            account_type=account_in.account_type,
            default_currency_code=account_in.default_currency_code,
        )
        return await self.account_repository.create(account)

    async def update_account(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        account_in: AccountUpdate,
    ) -> Account:
        account = await self.get_account(workspace_id, public_id)
        update_data = account_in.model_dump(exclude_unset=True)
        if not update_data:
            return account

        new_name = update_data.get("name")
        if new_name and new_name != account.name:
            existing = await self.account_repository.get_by_name(workspace_id, new_name)
            if existing:
                raise ConflictError(detail=f"Account named '{new_name}' already exists")

        new_currency = update_data.get("default_currency_code")
        if new_currency:
            currency = await self.currency_repository.get_by_code(new_currency)
            if not currency or not currency.is_active:
                raise ValidationError(detail=f"Unsupported default currency '{new_currency}'")

        for key, value in update_data.items():
            setattr(account, key, value)
        account.updated_at = datetime.now(UTC)
        return await self.account_repository.save(account)
