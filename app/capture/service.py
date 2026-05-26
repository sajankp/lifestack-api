import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.exceptions import ValidationError
from app.spending.models import TransactionType
from app.spending.schemas import TransactionCreate
from app.spending.service import CategoryService, TransactionService
from app.todo.schemas import TodoCreate
from app.todo.service import TodoService


class CaptureService:
    def __init__(
        self,
        todo_service: TodoService,
        tx_service: TransactionService,
        category_service: CategoryService,
    ):
        self.todo_service = todo_service
        self.tx_service = tx_service
        self.category_service = category_service

    def _route(self, text: str, module: str | None, amount_hint: str | None) -> str:
        if module in {"todo", "spending"}:
            return module
        t = text.lower()
        money_keywords = any(word in t for word in ["spent", "paid", "cost", "expense", "income"])
        has_money = bool(amount_hint or re.search(r"[$₹£€¥₩]\s*\d", t) or money_keywords)
        has_todo = any(
            w in t for w in ["todo", "task", "buy", "call", "email", "remind", "fix", "do"]
        )
        if has_money and has_todo:
            raise ValidationError(
                detail="Capture contained conflicting module signals. Please specify module or add stronger hints."
            )
        if has_money:
            return "spending"
        return "todo"

    def _parse_due_date(self, value: str | None):
        if not value:
            return None
        v = value.strip().lower()
        today = datetime.now(UTC).date()
        if v == "today":
            return today
        if v == "tomorrow":
            return today + timedelta(days=1)
        if v == "next week":
            return today + timedelta(days=7)
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise ValidationError(
                detail="Invalid due_date format. Use YYYY-MM-DD, today, tomorrow, or next week."
            ) from None

    async def capture(
        self, user_id: int, workspace_id: int, text: str, module: str | None, hints: dict | None
    ):
        hints = hints or {}
        route = self._route(text, module, hints.get("amount"))
        if route == "todo":
            payload = TodoCreate(
                title=text.strip(),
                due_date=self._parse_due_date(hints.get("due_date")),
                priority=hints.get("priority") or "medium",
            )
            todo = await self.todo_service.create_todo(user_id, workspace_id, payload)
            return {
                "captured": True,
                "module": "todo",
                "entity_public_id": todo.public_id,
                "entity_type": "task",
                "parsed": {
                    "title": todo.title,
                    "due_date": todo.due_date.isoformat() if todo.due_date else None,
                },
            }

        cats, _ = await self.category_service.list_categories(workspace_id, limit=200, offset=0)
        category = next(
            (c for c in cats if c.name.lower() == (hints.get("category") or "other").lower()), None
        )
        if category is None:
            category = next((c for c in cats if c.name.lower() == "other"), None)
        if category is None:
            raise ValidationError(detail="No spending category available for capture")
        try:
            amount = Decimal(hints.get("amount") or "0")
        except Exception as exc:
            raise ValidationError(detail="Invalid amount format in hints.amount") from exc
        if amount <= 0:
            clean_text = re.sub(r"\d{4}-\d{2}-\d{2}", "", text)
            m = re.search(r"(\d+(?:\.\d{1,2})?)", clean_text)
            if m:
                amount = Decimal(m.group(1))
        if amount <= 0:
            raise ValidationError(detail="Unable to infer spending amount from capture input")
        tx = await self.tx_service.create_transaction(
            user_id,
            workspace_id,
            TransactionCreate(
                category_id=category.public_id,
                amount=amount,
                type=TransactionType(hints.get("type") or "expense"),
                occurred_at=datetime.now(UTC),
                description=text.strip(),
            ),
        )
        return {
            "captured": True,
            "module": "spending",
            "entity_public_id": tx.public_id,
            "entity_type": "transaction",
            "parsed": {"description": tx.description, "amount": str(tx.amount)},
        }
