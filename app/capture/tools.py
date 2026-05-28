from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditLogger
from app.finance.repository import AccountRepository, CurrencyRepository
from app.investing.repository import CashBalanceRepository
from app.investing.schemas import CashBalanceCreate
from app.investing.service import CashBalanceService
from app.spending.models import TransactionType
from app.spending.repository import CategoryRepository, TransactionRepository
from app.spending.schemas import TransactionCreate
from app.spending.service import CategoryService, TransactionService
from app.todo.repository import TodoRepository
from app.todo.schemas import TodoCreate
from app.todo.service import TodoService


class AgentTools:
    def __init__(self, session: AsyncSession, user_id: int, workspace_id: int):
        self.session = session
        self.user_id = user_id
        self.workspace_id = workspace_id

        # Instantiate repositories and services directly with session
        self.todo_repo = TodoRepository(session)
        self.todo_service = TodoService(self.todo_repo)

        self.tx_repo = TransactionRepository(session)
        self.cat_repo = CategoryRepository(session)
        self.tx_service = TransactionService(self.tx_repo, self.cat_repo)
        self.category_service = CategoryService(self.cat_repo)

        self.cash_repo = CashBalanceRepository(session)
        self.account_repo = AccountRepository(session)
        self.currency_repo = CurrencyRepository(session)
        self.cash_service = CashBalanceService(
            self.cash_repo, self.account_repo, self.currency_repo
        )

        self.audit_logger = AuditLogger(session)

    async def create_todo_task(
        self, title: str, due_date: str | None = None, priority: str = "medium"
    ) -> dict:
        """Create a new todo task/item for the user.

        Args:
            title: The title or text of the todo task.
            due_date: Optional due date in YYYY-MM-DD format (e.g. '2026-05-29').
            priority: The priority, one of 'low', 'medium', or 'high'.
        """
        parsed_due = None
        if due_date:
            try:
                parsed_due = datetime.strptime(due_date.strip(), "%Y-%m-%d").date()
            except ValueError:
                return {"status": "error", "message": "Invalid due_date format. Use YYYY-MM-DD."}

        payload = TodoCreate(
            title=title,
            due_date=parsed_due,
            priority=priority,
        )
        todo = await self.todo_service.create_todo(
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            todo_in=payload,
            audit_logger=self.audit_logger,
        )
        return {
            "status": "success",
            "entity_public_id": str(todo.public_id),
            "entity_type": "todo",
            "title": todo.title,
            "due_date": todo.due_date.isoformat() if todo.due_date else None,
            "priority": todo.priority,
        }

    async def log_spending_transaction(
        self, amount: str, category_name: str, description: str
    ) -> dict:
        """Record/log a new spending transaction (expense).

        Args:
            amount: The transaction amount as a string (e.g., '14.99').
            category_name: The name of the spending category (e.g., 'food', 'utilities', 'shopping').
            description: Description of what the money was spent on.
        """
        try:
            amt = Decimal(amount)
        except Exception:
            return {
                "status": "error",
                "message": "Invalid amount format. Must be a decimal number.",
            }

        # Resolve category
        cats, _ = await self.category_service.list_categories(
            self.workspace_id, limit=200, offset=0
        )
        category = next((c for c in cats if c.name.lower() == category_name.lower()), None)
        if not category:
            # Try fallback to 'other'
            category = next((c for c in cats if c.name.lower() == "other"), None)
        if not category:
            return {
                "status": "error",
                "message": "No suitable spending category found in this workspace.",
            }

        payload = TransactionCreate(
            category_id=category.public_id,
            amount=amt,
            type=TransactionType.expense,
            occurred_at=datetime.now(UTC),
            description=description,
        )
        tx = await self.tx_service.create_transaction(
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            tx_in=payload,
            audit_logger=self.audit_logger,
        )
        return {
            "status": "success",
            "entity_public_id": str(tx.public_id),
            "entity_type": "transaction",
            "amount": str(tx.amount),
            "category": category.name,
            "description": tx.description,
        }

    async def log_cash_balance(self, account_name: str, balance: str, currency: str) -> dict:
        """Record/update cash balance for an investing account.

        Args:
            account_name: The name of the brokerage or bank account (e.g., 'Brokerage Cash').
            balance: The cash balance amount as a string (e.g. '1200.50').
            currency: The currency code (e.g. 'USD', 'EUR', 'GBP').
        """
        try:
            val = Decimal(balance)
        except Exception:
            return {
                "status": "error",
                "message": "Invalid balance format. Must be a decimal number.",
            }

        payload = CashBalanceCreate(
            account_name=account_name,
            balance=val,
            currency=currency.upper(),
            as_of=datetime.now(UTC),
        )
        cash = await self.cash_service.create_cash_balance(
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            cash_in=payload,
            audit_logger=self.audit_logger,
        )
        return {
            "status": "success",
            "entity_public_id": str(cash.public_id),
            "entity_type": "cash_balance",
            "account_name": cash.account_name,
            "balance": str(cash.balance),
            "currency": cash.currency,
        }
