import uuid
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditLogger
from app.finance.repository import AccountRepository, CurrencyRepository, FinanceSettingRepository
from app.investing.order_service import InvestingOrderService
from app.investing.repository import (
    CashBalanceRepository,
    CompanyRepository,
    CorporateActionRepository,
    HoldingRepository,
    InstrumentRepository,
    InvestingOrderRepository,
    LotRepository,
)
from app.investing.schemas import CashBalanceCreate, InvestingOrderCreate
from app.investing.service import CashBalanceService, InstrumentService
from app.spending.models import TransactionType
from app.spending.repository import CategoryRepository, TransactionRepository
from app.spending.schemas import TransactionCreate
from app.spending.service import CategoryService, TransactionService
from app.todo.repository import TodoRepository
from app.todo.schemas import TodoCreate, TodoUpdate
from app.todo.service import TodoService

logger = structlog.get_logger(__name__)


def _parse_due_datetime(value: str) -> datetime:
    normalized = value.strip()
    if len(normalized) == 10:
        return datetime.strptime(normalized, "%Y-%m-%d").replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class AgentTools:
    def __init__(
        self,
        session: AsyncSession,
        user_id: int,
        workspace_id: int,
        user_timezone: str = "UTC",
    ):
        self.session = session
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.user_timezone = user_timezone

        # Instantiate repositories and services directly with session
        self.todo_repo = TodoRepository(session)
        self.todo_service = TodoService(self.todo_repo)

        self.account_repo = AccountRepository(session)
        self.tx_repo = TransactionRepository(session)
        self.cat_repo = CategoryRepository(session)
        self.setting_repo = FinanceSettingRepository(session)
        self.tx_service = TransactionService(
            self.tx_repo, self.cat_repo, self.account_repo, self.setting_repo
        )
        self.category_service = CategoryService(self.cat_repo)

        self.cash_repo = CashBalanceRepository(session)
        self.currency_repo = CurrencyRepository(session)
        self.cash_service = CashBalanceService(
            self.cash_repo, self.account_repo, self.currency_repo
        )

        self.holding_repo = HoldingRepository(session)
        self.order_repo = InvestingOrderRepository(session)
        self.lot_repo = LotRepository(session)
        self.corporate_action_repo = CorporateActionRepository(session)
        self.instrument_service = InstrumentService(
            InstrumentRepository(session), CompanyRepository(session)
        )
        self.order_service = InvestingOrderService(
            order_repository=self.order_repo,
            holding_repository=self.holding_repo,
            cash_balance_repository=self.cash_repo,
            account_repository=self.account_repo,
            currency_repository=self.currency_repo,
            instrument_service=self.instrument_service,
            lot_repository=self.lot_repo,
            corporate_action_repository=self.corporate_action_repo,
        )

        self.audit_logger = AuditLogger(session)

    async def create_todo_task(
        self, title: str, due_date: str | None = None, priority: str = "medium"
    ) -> dict:
        """Create a new todo task/item for the user.

        Args:
            title: The title or text of the todo task.
            due_date: Optional ISO 8601 due date/time (e.g. '2026-05-29T16:00:00+05:30').
            priority: The priority, one of 'low', 'medium', or 'high'.
        """
        parsed_due = None
        if due_date:
            try:
                parsed_due = _parse_due_datetime(due_date)
            except ValueError:
                return {
                    "status": "error",
                    "message": "Invalid due_date format. Use an ISO 8601 date or date-time.",
                }

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

    async def list_todos(
        self, completed: bool | None = None, limit: int = 50, offset: int = 0
    ) -> dict:
        """List todos in the workspace. Returns a dict with items and total count."""
        todos, total = await self.todo_service.list_todos(
            self.workspace_id, completed, limit, offset
        )
        items = [
            {
                "public_id": str(t.public_id),
                "title": t.title,
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "priority": t.priority,
                "completed": t.completed,
            }
            for t in todos
        ]
        return {"status": "success", "total": total, "items": items}

    async def list_next_due_items(self, limit: int = 5) -> dict:
        """Return the next due todo items (title, due_date, public_id)."""
        todos = await self.todo_service.get_next_due_items(
            self.workspace_id, datetime.now(UTC), limit
        )
        items = [
            {
                "public_id": str(t.public_id),
                "title": t.title,
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "priority": t.priority,
                "completed": t.completed,
            }
            for t in todos
        ]
        return {"status": "success", "total": len(items), "items": items}

    async def get_todo(self, public_id: str) -> dict:
        """Retrieve a single todo by public_id."""
        try:
            pid = uuid.UUID(public_id)
        except Exception:
            return {"status": "error", "message": "Invalid public_id"}

        try:
            todo = await self.todo_service.get_todo(self.workspace_id, pid)
        except Exception as e:
            logger.error("get_todo_failed", public_id=public_id, error=str(e))
            return {
                "status": "error",
                "message": "An internal error occurred while executing the tool.",
            }

        return {
            "status": "success",
            "entity_public_id": str(todo.public_id),
            "title": todo.title,
            "description": todo.description,
            "due_date": todo.due_date.isoformat() if todo.due_date else None,
            "priority": todo.priority,
            "completed": todo.completed,
        }

    async def update_todo(
        self,
        public_id: str,
        title: str | None = None,
        description: str | None = None,
        due_date: str | None = None,
        priority: str | None = None,
        completed: bool | None = None,
    ) -> dict:
        """Update fields on an existing todo. due_date should be ISO 8601 if provided."""
        try:
            pid = uuid.UUID(public_id)
        except Exception:
            return {"status": "error", "message": "Invalid public_id"}

        parsed_due = None
        if due_date:
            try:
                parsed_due = _parse_due_datetime(due_date)
            except ValueError:
                return {
                    "status": "error",
                    "message": "Invalid due_date format. Use an ISO 8601 date or date-time.",
                }

        supplied_fields = {
            key: value
            for key, value in {
                "title": title,
                "description": description,
                "due_date": parsed_due if due_date is not None else None,
                "priority": priority,
                "completed": completed,
            }.items()
            if value is not None
        }
        update_payload = TodoUpdate.model_validate(supplied_fields)

        try:
            todo = await self.todo_service.update_todo(
                self.workspace_id,
                pid,
                update_payload,
                actor_id=self.user_id,
                audit_logger=self.audit_logger,
            )
        except Exception as e:
            await self.session.rollback()
            logger.error("update_todo_failed", public_id=public_id, error=str(e))
            return {
                "status": "error",
                "message": "An internal error occurred while executing the tool.",
            }

        return {
            "status": "success",
            "entity_public_id": str(todo.public_id),
            "title": todo.title,
            "due_date": todo.due_date.isoformat() if todo.due_date else None,
            "priority": todo.priority,
            "completed": todo.completed,
        }

    async def delete_todo(self, public_id: str) -> dict:
        """Delete a todo by public_id."""
        try:
            pid = uuid.UUID(public_id)
        except Exception:
            return {"status": "error", "message": "Invalid public_id"}
        try:
            await self.todo_service.delete_todo(
                self.workspace_id,
                pid,
                actor_id=self.user_id,
                audit_logger=self.audit_logger,
            )
        except Exception as e:
            logger.error("delete_todo_failed", public_id=public_id, error=str(e))
            return {
                "status": "error",
                "message": "An internal error occurred while executing the tool.",
            }
        return {"status": "success", "entity_public_id": public_id}

    async def log_spending_transaction(
        self,
        amount: str,
        category_name: str,
        description: str,
        account_name: str | None = None,
    ) -> dict:
        """Record/log a new spending transaction (expense).

        Args:
            amount: The transaction amount as a string (e.g., '14.99').
            category_name: The name of the spending category (e.g., 'food', 'utilities', 'shopping').
            description: Description of what the money was spent on.
            account_name: Optional account name to attach to the transaction.
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

        account_public_id = None
        resolved_account_name = None
        if account_name:
            account = await self.account_repo.get_by_name(self.workspace_id, account_name)
            if not account or not account.is_active:
                return {
                    "status": "error",
                    "message": f"Account '{account_name}' is not found or is inactive in this workspace",
                }
            account_public_id = account.public_id
            resolved_account_name = account.name

        payload = TransactionCreate(
            category_id=category.public_id,
            account_id=account_public_id,
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
            "account_name": resolved_account_name,
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

        account = await self.account_repo.get_by_name(self.workspace_id, account_name)
        if not account or not account.is_active:
            return {
                "status": "error",
                "message": f"Account '{account_name}' is not found or is inactive in this workspace",
            }

        payload = CashBalanceCreate(
            account_id=account.public_id,
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
            "account_name": account.name,
            "balance": str(cash.balance),
            "currency": cash.currency,
        }

    async def place_stock_order(
        self,
        order_type: str,
        symbol: str,
        quantity: str,
        price_per_unit: str,
        account_name: str,
        currency: str = "USD",
        brokerage_fee: str = "0",
    ) -> dict:
        """Place a buy or sell order for a stock in a brokerage account.

        Args:
            order_type: Either 'buy' or 'sell'.
            symbol: The stock ticker symbol (e.g., 'AAPL', 'INFY.NS').
            quantity: Number of shares as a string (e.g., '10').
            price_per_unit: Price per share as a string (e.g., '150.00').
            account_name: Name of the brokerage account to use.
            currency: Currency code (default 'USD').
            brokerage_fee: Brokerage commission as a string (default '0').
        """
        order_type_lower = order_type.strip().lower()
        if order_type_lower not in {"buy", "sell"}:
            return {"status": "error", "message": "order_type must be 'buy' or 'sell'"}

        try:
            qty = Decimal(quantity)
            if qty <= 0:
                raise ValueError
        except Exception:
            return {"status": "error", "message": "quantity must be a positive number"}

        try:
            price = Decimal(price_per_unit)
            if price <= 0:
                raise ValueError
        except Exception:
            return {"status": "error", "message": "price_per_unit must be a positive number"}

        try:
            fee = Decimal(brokerage_fee)
            if fee < 0:
                raise ValueError
        except Exception:
            return {"status": "error", "message": "brokerage_fee must be a non-negative number"}

        account = await self.account_repo.get_by_name(self.workspace_id, account_name)
        if not account or not account.is_active:
            return {
                "status": "error",
                "message": f"Account '{account_name}' not found or inactive",
            }

        order_in = InvestingOrderCreate(
            account_id=account.public_id,
            order_type=order_type_lower,  # type: ignore[arg-type]
            symbol=symbol.upper(),
            quantity=qty,
            price_per_unit=price,
            currency=currency.upper(),
            brokerage_fee=fee,
            occurred_at=datetime.now(UTC),
        )

        try:
            order = await self.order_service.place_order(
                workspace_id=self.workspace_id,
                user_id=self.user_id,
                order_in=order_in,
                audit_logger=self.audit_logger,
                source_type="voice_agent",
            )
        except Exception as exc:
            detail = getattr(exc, "detail", str(exc))
            return {"status": "error", "message": detail}

        response: dict = {
            "status": "success",
            "entity_public_id": str(order.public_id),
            "entity_type": "investing_order",
            "order_type": order.order_type,
            "symbol": order.symbol,
            "quantity": str(order.quantity),
            "price_per_unit": str(order.price_per_unit),
            "gross_amount": str(order.gross_amount),
            "net_amount": str(order.net_amount),
            "currency": order.currency,
            "account_name": account_name,
        }
        if order.realized_gain_loss is not None:
            response["realized_gain_loss"] = str(order.realized_gain_loss)
            response["avg_cost_at_sale"] = str(order.avg_cost_at_sale)
        return response
