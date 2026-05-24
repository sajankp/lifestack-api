import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from app.core.audit import AuditLogger
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.pagination import DEFAULT_LIMIT
from app.finance.models import Account, CapitalTransfer
from app.finance.repository import (
    AccountRepository,
    CapitalTransferRepository,
    CurrencyRepository,
    FinanceSettingRepository,
    FxRateRepository,
)
from app.finance.schemas import (
    AccountCreate,
    AccountUpdate,
    CapitalTransferCreate,
    FxRateUpsert,
)


class CurrencyService:
    def __init__(self, repository: CurrencyRepository):
        self.repository = repository

    async def list_workspace_currencies(self, workspace_id: int) -> Sequence:
        currencies = await self.repository.list_workspace_enabled(workspace_id)
        if currencies:
            return currencies

        # Bootstrap default workspace currency mappings for new workspaces.
        active = await self.repository.list_active()
        if active:
            await self.repository.add_workspace_currencies(
                workspace_id, [currency.code for currency in active]
            )
            currencies = await self.repository.list_workspace_enabled(workspace_id)
        return currencies

    async def validate_supported_code(self, code: str) -> None:
        currency = await self.repository.get_by_code(code)
        if not currency or not currency.is_active:
            raise ValidationError(detail=f"Unsupported currency code '{code}'")

    async def validate_workspace_enabled(self, workspace_id: int, code: str) -> None:
        await self.validate_supported_code(code)
        enabled = await self.repository.is_enabled_for_workspace(workspace_id, code)
        if not enabled:
            raise ValidationError(
                detail=f"Currency '{code.upper()}' is not enabled for this workspace"
            )


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


class FinanceSettingService:
    def __init__(
        self,
        setting_repository: FinanceSettingRepository,
        currency_repository: CurrencyRepository,
    ):
        self.setting_repository = setting_repository
        self.currency_repository = currency_repository

    async def get_setting(self, workspace_id: int):
        return await self.setting_repository.get_by_workspace(workspace_id)

    async def set_reporting_currency(self, workspace_id: int, reporting_currency_code: str | None):
        if reporting_currency_code is not None:
            currency = await self.currency_repository.get_by_code(reporting_currency_code)
            if not currency or not currency.is_active:
                raise ValidationError(
                    detail=f"Unsupported reporting currency '{reporting_currency_code}'"
                )
            enabled = await self.currency_repository.is_enabled_for_workspace(
                workspace_id, reporting_currency_code
            )
            if not enabled:
                raise ValidationError(
                    detail=(
                        f"Reporting currency '{reporting_currency_code}' is not enabled "
                        "for this workspace"
                    )
                )
        return await self.setting_repository.upsert_reporting_currency(
            workspace_id, reporting_currency_code
        )


class FxRateService:
    def __init__(
        self,
        repository: FxRateRepository,
        currency_repository: CurrencyRepository,
    ):
        self.repository = repository
        self.currency_repository = currency_repository

    async def upsert(self, payload: FxRateUpsert):
        for code in [payload.base_currency_code, payload.quote_currency_code]:
            currency = await self.currency_repository.get_by_code(code)
            if not currency or not currency.is_active:
                raise ValidationError(detail=f"Unsupported currency code '{code}'")
        return await self.repository.upsert_rate(
            base_currency_code=payload.base_currency_code,
            quote_currency_code=payload.quote_currency_code,
            rate=float(payload.rate),
            as_of=payload.as_of,
            fetched_at=payload.fetched_at,
            source=payload.source,
        )

    async def resolve_rate(
        self, base_currency: str, quote_currency: str, as_of: datetime | None = None
    ) -> Decimal | None:
        base = base_currency.upper()
        quote = quote_currency.upper()
        if base == quote:
            return Decimal("1")

        direct = await self.repository.get_latest_rate(base, quote, as_of=as_of)
        if direct:
            return Decimal(str(direct.rate))

        # One-hop triangulation via USD for V1.1 fallback.
        via = "USD"
        if base == via or quote == via:
            return None

        base_to_usd = await self.repository.get_latest_rate(base, via, as_of=as_of)
        usd_to_quote = await self.repository.get_latest_rate(via, quote, as_of=as_of)
        if base_to_usd and usd_to_quote:
            return Decimal(str(base_to_usd.rate)) * Decimal(str(usd_to_quote.rate))
        return None

    async def get_latest_pair(
        self, base_currency: str, quote_currency: str, as_of: datetime | None = None
    ):
        return await self.repository.get_latest_rate(
            base_currency.upper(), quote_currency.upper(), as_of=as_of
        )


class CapitalTransferService:
    def __init__(
        self,
        transfer_repository: CapitalTransferRepository,
        account_repository: AccountRepository,
        currency_repository: CurrencyRepository,
    ):
        self.transfer_repository = transfer_repository
        self.account_repository = account_repository
        self.currency_repository = currency_repository

    async def list_transfers(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[CapitalTransfer], int]:
        return await self.transfer_repository.list_workspace_transfers(workspace_id, limit, offset)

    async def get_transfer(self, workspace_id: int, public_id: uuid.UUID) -> CapitalTransfer:
        transfer = await self.transfer_repository.get_by_public_id(workspace_id, public_id)
        if not transfer:
            raise NotFoundError(detail=f"Transfer with id {public_id} not found in this workspace")
        return transfer

    async def create_transfer(
        self,
        workspace_id: int,
        actor_id: int,
        transfer_in: CapitalTransferCreate,
        audit_logger: AuditLogger | None = None,
    ) -> CapitalTransfer:
        from_account = await self.account_repository.get_by_public_id(
            workspace_id, transfer_in.from_account_id
        )
        if not from_account:
            raise ValidationError(detail="from_account_id is invalid for this workspace")
        to_account = await self.account_repository.get_by_public_id(
            workspace_id, transfer_in.to_account_id
        )
        if not to_account:
            raise ValidationError(detail="to_account_id is invalid for this workspace")

        for code in [transfer_in.from_currency_code, transfer_in.to_currency_code]:
            currency = await self.currency_repository.get_by_code(code)
            if not currency or not currency.is_active:
                raise ValidationError(detail=f"Unsupported currency code '{code}'")
            enabled = await self.currency_repository.is_enabled_for_workspace(workspace_id, code)
            if not enabled:
                raise ValidationError(detail=f"Currency '{code}' is not enabled for this workspace")

        transfer = CapitalTransfer(
            workspace_id=workspace_id,
            actor_id=actor_id,
            from_module=transfer_in.from_module,
            to_module=transfer_in.to_module,
            from_account_id=from_account.id,  # type: ignore[arg-type]
            to_account_id=to_account.id,  # type: ignore[arg-type]
            from_currency_code=transfer_in.from_currency_code,
            to_currency_code=transfer_in.to_currency_code,
            gross_amount=float(transfer_in.gross_amount),
            fx_rate_used=float(transfer_in.fx_rate_used) if transfer_in.fx_rate_used else None,
            fx_fee_amount=float(transfer_in.fx_fee_amount),
            platform_fee_amount=float(transfer_in.platform_fee_amount),
            tax_amount=float(transfer_in.tax_amount),
            net_amount_received=float(transfer_in.net_amount_received),
            occurred_at=transfer_in.occurred_at,
            notes=transfer_in.notes,
        )
        transfer = await self.transfer_repository.create(transfer)

        if audit_logger:
            from_module = (
                transfer.from_module.value
                if hasattr(transfer.from_module, "value")
                else str(transfer.from_module)
            )
            to_module = (
                transfer.to_module.value
                if hasattr(transfer.to_module, "value")
                else str(transfer.to_module)
            )
            after_snap = {
                "public_id": str(transfer.public_id),
                "from_module": from_module,
                "to_module": to_module,
                "from_account_id": str(from_account.public_id),
                "to_account_id": str(to_account.public_id),
                "from_currency_code": transfer.from_currency_code,
                "to_currency_code": transfer.to_currency_code,
                "gross_amount": str(transfer.gross_amount),
                "fx_rate_used": str(transfer.fx_rate_used)
                if transfer.fx_rate_used is not None
                else None,
                "fx_fee_amount": str(transfer.fx_fee_amount),
                "platform_fee_amount": str(transfer.platform_fee_amount),
                "tax_amount": str(transfer.tax_amount),
                "net_amount_received": str(transfer.net_amount_received),
                "occurred_at": transfer.occurred_at.isoformat(),
                "notes": transfer.notes,
            }
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="create",
                module="finance",
                entity_type="capital_transfer",
                entity_id=transfer.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(transfer.public_id),
                    "before": None,
                    "after": after_snap,
                    "changed_fields": list(after_snap.keys()),
                },
            )

        return transfer
