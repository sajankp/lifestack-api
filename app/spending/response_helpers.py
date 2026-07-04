import uuid

from app.core.exceptions import NotFoundError
from app.imports.models import ImportBatch, ImportModule
from app.spending.models import (
    RecurringTransaction,
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
)
from app.spending.schemas import (
    BudgetResponse,
    CategoryResponse,
    RecurringTransactionResponse,
    SourceMetadataResponse,
    TransactionResponse,
)


def category_response(cat: SpendingCategory) -> CategoryResponse:
    return CategoryResponse.model_validate(cat)


def parse_import_row_number(source_ref: str | None) -> int | None:
    if not source_ref or ":" not in source_ref:
        return None
    row_value = source_ref.rsplit(":", 1)[-1]
    try:
        return int(row_value)
    except ValueError:
        return None


def source_metadata_response(
    source_type: str,
    source_ref: str | None,
    import_batch: ImportBatch | None = None,
) -> SourceMetadataResponse:
    if source_type == "imported":
        rollback_supported = import_batch is not None and import_batch.module in (
            ImportModule.spending_transactions,
            ImportModule.spending_budgets,
        )
        return SourceMetadataResponse(
            source_type=source_type,
            source_ref=source_ref,
            origin="bulk_import",
            label="Bulk import",
            import_public_id=import_batch.public_id if import_batch else None,
            import_module=import_batch.module if import_batch else None,
            import_row_number=parse_import_row_number(source_ref),
            rollback_supported=rollback_supported,
        )
    if source_type == "synced":
        return SourceMetadataResponse(
            source_type=source_type,
            source_ref=source_ref,
            origin="external_sync",
            label="External sync",
        )
    if source_type == "assistant":
        return SourceMetadataResponse(
            source_type=source_type,
            source_ref=source_ref,
            origin="assistant_action",
            label="Assistant action",
        )
    if source_type == "order":
        return SourceMetadataResponse(
            source_type=source_type,
            source_ref=source_ref,
            origin="manual_entry",
            label="Order-derived",
        )
    return SourceMetadataResponse(
        source_type=source_type,
        source_ref=source_ref,
        origin="manual_entry",
        label="Manual entry",
    )


def transaction_response(
    tx: SpendingTransaction,
    category_public_id: uuid.UUID,
    account_public_id: uuid.UUID | None = None,
    import_batch: ImportBatch | None = None,
) -> TransactionResponse:
    data = tx.model_dump()
    data["category_id"] = category_public_id
    data["account_id"] = account_public_id
    data["source_metadata"] = source_metadata_response(tx.source_type, tx.source_ref, import_batch)
    return TransactionResponse.model_validate(data)


def budget_response(
    budget: SpendingBudget,
    category_public_id: uuid.UUID,
    import_batch: ImportBatch | None = None,
) -> BudgetResponse:
    data = budget.model_dump()
    data["category_id"] = category_public_id
    data["source_metadata"] = source_metadata_response(
        budget.source_type, budget.source_ref, import_batch
    )
    return BudgetResponse.model_validate(data)


def recurring_response(
    recurring: RecurringTransaction,
    category_public_id: uuid.UUID,
) -> RecurringTransactionResponse:
    data = recurring.model_dump()
    data["category_id"] = category_public_id
    return RecurringTransactionResponse.model_validate(data)


def category_public_id_or_404(cat_cache: dict[int, uuid.UUID], category_id: int) -> uuid.UUID:
    category_public_id = cat_cache.get(category_id)
    if category_public_id is None:
        raise NotFoundError(detail="Transaction category was not found")
    return category_public_id
