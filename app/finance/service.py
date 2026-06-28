import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.config import settings
from app.core.audit import AuditLogger
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.pagination import DEFAULT_LIMIT
from app.finance.models import Account, CapitalTransfer, Currency, CurrencyDisplayPreference
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
    ReconciliationSummary,
)
from app.investing.models import CashBalance as InvestingCashBalance


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
        await self.repository.ensure_workspace_defaults(workspace_id)
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

    async def delete_account(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        account = await self.get_account(workspace_id, public_id)
        if account.id is None:
            raise ValidationError(detail="Account ID is missing.")
        account_id = account.id
        if await self.account_repository.has_usage(workspace_id, account_id):
            raise ConflictError(
                detail=("Account is in use and cannot be deleted. Mark it inactive instead.")
            )
        before_snap = {
            "public_id": str(account.public_id),
            "name": account.name,
            "account_type": str(account.account_type),
            "default_currency_code": account.default_currency_code,
            "is_active": account.is_active,
        }
        await self.account_repository.delete(account)
        if audit_logger is not None and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="delete",
                module="finance",
                entity_type="account",
                entity_id=account_id,
                details={
                    "entity_public_id": str(public_id),
                    "before": before_snap,
                    "after": None,
                    "changed_fields": list(before_snap.keys()),
                },
            )

    async def get_spending_balance(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
    ) -> dict:
        """Return the transfer-inclusive projected balance for an account."""
        account = await self.get_account(workspace_id, public_id)
        if account.id is None:
            raise ValidationError(detail="Account ID is missing.")
        (
            balance,
            tx_count,
            transfer_count,
            first_at,
            last_at,
        ) = await self.account_repository.get_spending_balance(workspace_id, account.id)
        return {
            "account_public_id": account.public_id,
            "account_name": account.name,
            "account_type": account.account_type,
            "currency_code": account.default_currency_code,
            "spending_balance": balance,
            "transaction_count": tx_count,
            "transfer_count": transfer_count,
            "first_transaction_at": first_at,
            "last_transaction_at": last_at,
        }

    async def get_reconciliation_summary(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
    ) -> ReconciliationSummary:
        """Compare the projected ledger balance against the latest cash snapshot."""
        account = await self.get_account(workspace_id, public_id)
        if account.id is None:
            raise ValidationError(detail="Account ID is missing.")
        (
            projected,
            tx_count,
            transfer_count,
            snapshot_balance,
            snapshot_as_of,
        ) = await self.account_repository.get_reconciliation_summary(workspace_id, account.id)
        discrepancy: Decimal | None = None
        if snapshot_balance is not None:
            discrepancy = projected - snapshot_balance
        return ReconciliationSummary(
            account_public_id=account.public_id,
            account_name=account.name,
            currency_code=account.default_currency_code,
            projected_balance=projected,
            snapshot_balance=snapshot_balance,
            snapshot_as_of=snapshot_as_of,
            discrepancy=discrepancy,
            transaction_count=tx_count,
            transfer_count=transfer_count,
        )


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

    async def _validate_workspace_currency(
        self, workspace_id: int, currency_code: str, *, label: str
    ) -> None:
        currency = await self.currency_repository.get_by_code(currency_code)
        if not currency or not currency.is_active:
            raise ValidationError(detail=f"Unsupported {label} '{currency_code}'")
        await self.currency_repository.ensure_workspace_defaults(workspace_id)
        enabled = await self.currency_repository.is_enabled_for_workspace(
            workspace_id, currency_code
        )
        if not enabled:
            raise ValidationError(
                detail=f"{label.title()} '{currency_code}' is not enabled for this workspace"
            )

    async def update_workspace_settings(self, workspace_id: int, updates: dict):
        existing = await self.setting_repository.get_by_workspace(workspace_id)
        reporting_currency_code = existing.reporting_currency_code if existing else None
        currency_display_preference = (
            existing.currency_display_preference if existing else CurrencyDisplayPreference.symbol
        )
        lookthrough_min_weight_pct = (
            existing.lookthrough_min_weight_pct
            if existing
            else settings.LOOKTHROUGH_MIN_DISPLAY_WEIGHT_PCT
        )

        if "reporting_currency_code" in updates:
            reporting_currency_code = updates["reporting_currency_code"]
            if reporting_currency_code is not None:
                await self._validate_workspace_currency(
                    workspace_id,
                    reporting_currency_code,
                    label="reporting currency",
                )

        if "currency_display_preference" in updates:
            currency_display_preference = (
                updates["currency_display_preference"] or CurrencyDisplayPreference.symbol
            )
        if updates.get("lookthrough_min_weight_pct") is not None:
            lookthrough_min_weight_pct = updates["lookthrough_min_weight_pct"]

        return await self.setting_repository.upsert_workspace_settings(
            workspace_id,
            reporting_currency_code=reporting_currency_code,
            currency_display_preference=currency_display_preference,
            lookthrough_min_weight_pct=lookthrough_min_weight_pct,
        )

    async def get_user_settings(self, workspace_id: int, user_id: int) -> dict:
        workspace = await self.setting_repository.get_by_workspace(workspace_id)
        user_setting = await self.setting_repository.get_user_setting(workspace_id, user_id)

        workspace_reporting_currency_code = workspace.reporting_currency_code if workspace else None
        workspace_currency_display_preference = (
            workspace.currency_display_preference if workspace else CurrencyDisplayPreference.symbol
        )
        reporting_currency_override_code = (
            user_setting.reporting_currency_override_code if user_setting else None
        )
        currency_display_preference_override = (
            user_setting.currency_display_preference_override if user_setting else None
        )
        effective_reporting_currency_code = (
            reporting_currency_override_code or workspace_reporting_currency_code
        )
        effective_currency_display_preference = (
            currency_display_preference_override or workspace_currency_display_preference
        )

        updated_at = (
            user_setting.updated_at
            if user_setting
            else workspace.updated_at
            if workspace
            else datetime.now(UTC)
        )

        return {
            "reporting_currency_override_code": reporting_currency_override_code,
            "currency_display_preference_override": currency_display_preference_override,
            "workspace_reporting_currency_code": workspace_reporting_currency_code,
            "workspace_currency_display_preference": workspace_currency_display_preference,
            "effective_reporting_currency_code": effective_reporting_currency_code,
            "effective_currency_display_preference": effective_currency_display_preference,
            "updated_at": updated_at,
        }

    async def update_user_settings(self, workspace_id: int, user_id: int, updates: dict):
        existing = await self.setting_repository.get_user_setting(workspace_id, user_id)
        reporting_currency_override_code = (
            existing.reporting_currency_override_code if existing else None
        )
        currency_display_preference_override = (
            existing.currency_display_preference_override if existing else None
        )

        if "reporting_currency_override_code" in updates:
            reporting_currency_override_code = updates["reporting_currency_override_code"]
            if reporting_currency_override_code is not None:
                await self._validate_workspace_currency(
                    workspace_id,
                    reporting_currency_override_code,
                    label="override currency",
                )

        if "currency_display_preference_override" in updates:
            currency_display_preference_override = updates["currency_display_preference_override"]

        await self.setting_repository.upsert_user_settings(
            workspace_id,
            user_id,
            reporting_currency_override_code=reporting_currency_override_code,
            currency_display_preference_override=currency_display_preference_override,
        )
        return await self.get_user_settings(workspace_id, user_id)


class FxRateService:
    """
    Service managing FX rate lookup and updates.

    FX rates are globally scoped system reference data (market data) rather than
    workspace-scoped entities. Mutation capability (via `upsert`) is reserved
    exclusively for background tasks/cron ingestion jobs.
    """

    def __init__(
        self,
        repository: FxRateRepository,
        currency_repository: CurrencyRepository,
    ):
        self.repository = repository
        self.currency_repository = currency_repository
        self._currency_active_cache: dict[str, bool] = {}

    async def _is_active_currency(self, code: str) -> bool:
        if not code:
            return False
        normalized_code = code.upper()
        if normalized_code not in self._currency_active_cache:
            currency: Currency | None = await self.currency_repository.get_by_code(normalized_code)
            self._currency_active_cache[normalized_code] = bool(currency and currency.is_active)
        return self._currency_active_cache[normalized_code]

    async def upsert(self, payload: FxRateUpsert):
        """
        Upsert a globally scoped FX rate.

        This method is system-restricted and should only be invoked by internal scheduled
        jobs/workflows. It enforces validation on active currencies and same-currency constraints.
        """
        for code in {payload.base_currency_code, payload.quote_currency_code}:
            if not await self._is_active_currency(code):
                raise ValidationError(detail=f"Unsupported currency code '{code}'")
        # Same-currency transfers must use a rate of exactly 1.0
        if (
            payload.base_currency_code.upper() == payload.quote_currency_code.upper()
            and payload.rate != Decimal("1.0")
        ):
            raise ValidationError(detail="FX rate for same-currency pair must be 1.0")
        return await self.repository.upsert_rate(
            base_currency_code=payload.base_currency_code,
            quote_currency_code=payload.quote_currency_code,
            rate=payload.rate,
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
        cash_balance_repository=None,
    ):
        self.transfer_repository = transfer_repository
        self.account_repository = account_repository
        self.currency_repository = currency_repository
        self.cash_balance_repository = cash_balance_repository

    def _serialize_transfer(
        self,
        transfer: CapitalTransfer,
        from_account: Account | None,
        to_account: Account | None,
    ) -> dict[str, Any]:
        return {
            "public_id": transfer.public_id,
            "from_module": transfer.from_module,
            "to_module": transfer.to_module,
            "from_account_id": transfer.from_account_id,
            "to_account_id": transfer.to_account_id,
            "from_account_public_id": from_account.public_id if from_account else None,
            "to_account_public_id": to_account.public_id if to_account else None,
            "from_account_name": from_account.name if from_account else None,
            "to_account_name": to_account.name if to_account else None,
            "from_account_type": from_account.account_type if from_account else None,
            "to_account_type": to_account.account_type if to_account else None,
            "from_currency_code": transfer.from_currency_code,
            "to_currency_code": transfer.to_currency_code,
            "gross_amount": transfer.gross_amount,
            "fx_rate_used": transfer.fx_rate_used,
            "fx_fee_amount": transfer.fx_fee_amount,
            "platform_fee_amount": transfer.platform_fee_amount,
            "tax_amount": transfer.tax_amount,
            "net_amount_received": transfer.net_amount_received,
            "occurred_at": transfer.occurred_at,
            "notes": transfer.notes,
            "created_at": transfer.created_at,
            "updated_at": transfer.updated_at,
        }

    async def list_transfers(
        self, workspace_id: int, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> tuple[Sequence[dict[str, Any]], int]:
        transfers, total = await self.transfer_repository.list_workspace_transfers(
            workspace_id, limit, offset
        )
        account_ids = [
            *[transfer.from_account_id for transfer in transfers],
            *[transfer.to_account_id for transfer in transfers],
        ]
        accounts = await self.account_repository.list_by_ids(workspace_id, account_ids)
        account_by_id = {account.id: account for account in accounts}

        items = [
            self._serialize_transfer(
                transfer,
                account_by_id.get(transfer.from_account_id),
                account_by_id.get(transfer.to_account_id),
            )
            for transfer in transfers
        ]
        return items, total

    async def get_transfer(self, workspace_id: int, public_id: uuid.UUID) -> dict[str, Any]:
        transfer = await self.transfer_repository.get_by_public_id(workspace_id, public_id)
        if not transfer:
            raise NotFoundError(detail=f"Transfer with id {public_id} not found in this workspace")
        from_account = await self.account_repository.get_by_id(
            workspace_id, transfer.from_account_id
        )
        to_account = await self.account_repository.get_by_id(workspace_id, transfer.to_account_id)
        return self._serialize_transfer(transfer, from_account, to_account)

    async def create_transfer(
        self,
        workspace_id: int,
        actor_id: int,
        transfer_in: CapitalTransferCreate,
        audit_logger: AuditLogger | None = None,
    ) -> dict[str, Any]:
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

        await self.currency_repository.ensure_workspace_defaults(workspace_id)
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
            gross_amount=transfer_in.gross_amount,
            fx_rate_used=transfer_in.fx_rate_used,
            fx_fee_amount=transfer_in.fx_fee_amount,
            platform_fee_amount=transfer_in.platform_fee_amount,
            tax_amount=transfer_in.tax_amount,
            net_amount_received=transfer_in.net_amount_received,
            occurred_at=transfer_in.occurred_at,
            notes=transfer_in.notes,
        )

        # Validate arithmetic consistency with Decimal precision before persistence.
        if (
            transfer_in.from_currency_code == transfer_in.to_currency_code
            and transfer_in.fx_rate_used is not None
            and transfer_in.fx_rate_used != Decimal("1")
        ):
            raise ValidationError(
                detail="FX rate must be 1.0 when transferring between the same currency"
            )

        gross = transfer_in.gross_amount
        fx_rate = transfer_in.fx_rate_used if transfer_in.fx_rate_used is not None else Decimal("1")
        converted_gross = gross * fx_rate
        total_fees = (
            (transfer_in.fx_fee_amount or Decimal("0"))
            + (transfer_in.platform_fee_amount or Decimal("0"))
            + (transfer_in.tax_amount or Decimal("0"))
        )
        net = transfer_in.net_amount_received
        difference = abs(converted_gross - total_fees - net)
        if difference > Decimal("0.01"):
            raise ValidationError(
                detail=(
                    f"Transfer arithmetic inconsistent: "
                    f"gross ({gross:.2f}) * rate ({fx_rate:.4f}) - fees ({total_fees:.2f}) ≠ net ({net:.2f}). "
                    f"Difference: {difference:.4f}"
                )
            )
        transfer = await self.transfer_repository.create(transfer)

        # Auto-update brokerage cash balance when transferring TO investing
        if transfer_in.to_module == "investing" and self.cash_balance_repository is not None:
            latest_cash = await self.cash_balance_repository.get_latest_for_account_currency(
                workspace_id, to_account.id, transfer_in.to_currency_code
            )
            prev_balance = latest_cash.balance if latest_cash is not None else Decimal("0")
            new_balance = prev_balance + transfer.net_amount_received
            new_cash = InvestingCashBalance(
                workspace_id=workspace_id,
                user_id=actor_id,
                account_id=to_account.id,
                balance=new_balance,
                currency=transfer_in.to_currency_code,
                as_of=transfer.occurred_at,
                trigger_type="transfer",
                trigger_ref=transfer.public_id,
            )
            await self.cash_balance_repository.create(new_cash)

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

        return self._serialize_transfer(transfer, from_account, to_account)
