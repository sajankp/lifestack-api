import uuid
from datetime import UTC, date, datetime, time, tzinfo
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditLogger
from app.core.exceptions import APIError, NotFoundError
from app.finance.models import Account, AccountType
from app.finance.repository import (
    AccountRepository,
    CurrencyRepository,
    FinanceSettingRepository,
    FxRateRepository,
)
from app.health.repository import (
    MedicationEventRepository,
    MedicationRepository,
    WeightEntryRepository,
)
from app.health.schemas import MedicationEventUpsert, WeightEntryCreate
from app.health.service import HealthService
from app.investing.performance_service import InvestingSummaryService
from app.investing.repository import (
    CashBalanceRepository,
    HoldingPriceRepository,
    HoldingRepository,
    PortfolioSnapshotRepository,
)
from app.spending.models import TransactionType
from app.spending.repository import CategoryRepository, TransactionRepository
from app.spending.schemas import TransactionCreate
from app.spending.service import CategoryService, TransactionService
from app.todo.repository import TodoRepository
from app.todo.schemas import RecurringTodoRuleCreate, TodoCreate, TodoUpdate
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


def _parse_due_time(value: str) -> time:
    """Parse an 'HH:MM' (or 'HH:MM:SS') clock time for a recurring rule."""
    return time.fromisoformat(value.strip())


def _parse_occurred_at(value: str, user_timezone: str) -> datetime:
    """Resolve a spoken/ISO occurrence date for a spending transaction into a
    UTC datetime (spec-061). A bare date is anchored to **noon in the
    user's timezone** — not midnight-UTC — so it never drifts onto the previous
    local calendar day for negative-offset users (which would corrupt local
    day-grouping in analytics). A naive date-time is interpreted in the user's
    timezone; an offset-aware one is converted to UTC. Raises ValueError on
    anything unparseable so the caller can surface a structured error.
    """
    normalized = value.strip()
    try:
        tz: tzinfo = ZoneInfo(user_timezone) if user_timezone else UTC
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        tz = UTC

    if len(normalized) == 10:  # bare ISO date, e.g. "2026-07-03"
        day = date.fromisoformat(normalized)
        local_noon = datetime(day.year, day.month, day.day, 12, 0, tzinfo=tz)
        return local_noon.astimezone(UTC)

    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(UTC)


# Voice spending targets: every account type except brokerage (spec-059) —
# investing is read-only on the voice surface.
_SPENDING_ACCOUNT_TYPES = frozenset(t for t in AccountType if t is not AccountType.brokerage)


def _normalize_name(value: str) -> str:
    return " ".join(value.strip().lower().split())


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

        # Investing is read-only on the voice surface (spec-059): mirror the
        # REST /investing/summary wiring so voice answers match the dashboard.
        self.summary_service = InvestingSummaryService(
            HoldingRepository(session),
            CashBalanceRepository(session),
            self.setting_repo,
            FxRateRepository(session),
            HoldingPriceRepository(session),
            PortfolioSnapshotRepository(session),
            self.account_repo,
        )

        self.medication_repo = MedicationRepository(session)
        self.medication_event_repo = MedicationEventRepository(session)
        self.weight_repo = WeightEntryRepository(session)
        self.health_service = HealthService(
            self.medication_repo, self.medication_event_repo, self.weight_repo
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
            "summary": f"Added todo '{todo.title}'",
        }

    async def create_recurring_todo(
        self,
        title: str,
        frequency: str = "weekly",
        interval: int = 1,
        due_time: str | None = None,
        timezone: str | None = None,
        end_date: str | None = None,
        monthly_mode: str = "day_of_month",
        by_weekday: int | None = None,
        by_ordinal: int | None = None,
        priority: str = "medium",
    ) -> dict:
        """Create a recurring todo rule for reminders that repeat on a schedule.

        Use this (not `create_todo_task`) whenever the user's reminder repeats —
        e.g. "every other day", "every Monday", "on the 1st of each month".

        Args:
            title: English title for the recurring reminder.
            frequency: One of 'daily', 'weekly', 'monthly', 'yearly'.
            interval: Repeat every N periods (e.g. 2 with 'daily' = every other day).
            due_time: Optional 'HH:MM' clock time in the user's timezone (e.g. '09:00').
            timezone: IANA timezone (defaults to the user's session timezone).
            end_date: Optional ISO date (YYYY-MM-DD) after which the rule stops.
            monthly_mode: For monthly rules: 'day_of_month', 'last_day', or 'nth_weekday'.
            by_weekday: For 'nth_weekday' monthly rules, 0=Monday … 6=Sunday.
            by_ordinal: For 'nth_weekday' monthly rules, 1-4 or -1 (last).
        """
        rule_timezone = timezone or self.user_timezone
        try:
            # ZoneInfo raises ZoneInfoNotFoundError for unknown zones but
            # ValueError for malformed input (empty string, path traversal).
            anchor = datetime.now(ZoneInfo(rule_timezone)).date()
        except (ZoneInfoNotFoundError, ValueError):
            anchor = datetime.now(UTC).date()

        parsed_due_time: time | None = None
        if due_time:
            try:
                parsed_due_time = _parse_due_time(due_time)
            except ValueError:
                return {
                    "status": "error",
                    "message": "Invalid due_time format. Use 'HH:MM' (24-hour), e.g. '09:00'.",
                }

        parsed_end_date: date | None = None
        if end_date:
            try:
                parsed_end_date = date.fromisoformat(end_date.strip())
            except ValueError:
                return {
                    "status": "error",
                    "message": "Invalid end_date format. Use an ISO date, e.g. '2026-12-31'.",
                }

        # Validation (frequency/monthly-mode combinations, timezone, anchor vs
        # end_date) stays in the todo service and schema — the tool only
        # translates the arguments and surfaces the service's error message.
        try:
            rule_in = RecurringTodoRuleCreate(
                title=title,
                priority=priority,  # type: ignore[arg-type]
                frequency=frequency,  # type: ignore[arg-type]
                interval=interval,
                anchor_date=anchor,
                due_time=parsed_due_time,
                timezone=rule_timezone,
                end_date=parsed_end_date,
                monthly_mode=monthly_mode,  # type: ignore[arg-type]
                by_weekday=by_weekday,
                by_ordinal=by_ordinal,  # type: ignore[arg-type]
            )
        except (ValueError, PydanticValidationError) as exc:
            return {"status": "error", "message": f"Invalid recurring reminder: {exc}"}

        try:
            rule = await self.todo_service.create_recurring_rule(
                user_id=self.user_id,
                workspace_id=self.workspace_id,
                rule_in=rule_in,
                audit_logger=self.audit_logger,
            )
        except APIError as exc:
            await self.session.rollback()
            return {"status": "error", "message": exc.detail}

        return {
            "status": "success",
            "entity_public_id": str(rule.public_id),
            "entity_type": "recurring_todo",
            "title": rule.title,
            "frequency": rule.frequency,
            "interval": rule.interval,
            "due_time": rule.due_time.isoformat() if rule.due_time else None,
            "timezone": rule.timezone,
            "next_due_date": rule.next_due_date.isoformat() if rule.next_due_date else None,
            "summary": f"Added recurring todo '{rule.title}'",
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

        summary = f"Updated todo '{todo.title}'"
        if "completed" in supplied_fields:
            if completed:
                summary = f"Completed todo '{todo.title}'"
            else:
                summary = f"Reopened todo '{todo.title}'"

        return {
            "status": "success",
            "entity_public_id": str(todo.public_id),
            "entity_type": "todo",
            "title": todo.title,
            "due_date": todo.due_date.isoformat() if todo.due_date else None,
            "priority": todo.priority,
            "completed": todo.completed,
            "summary": summary,
        }

    async def delete_todo(self, public_id: str) -> dict:
        """Delete a todo by public_id."""
        try:
            pid = uuid.UUID(public_id)
        except Exception:
            return {"status": "error", "message": "Invalid public_id"}

        # Pre-fetch the todo to obtain its title for the summary before deletion
        try:
            todo = await self.todo_service.get_todo(self.workspace_id, pid)
            todo_title = todo.title
        except NotFoundError:
            return {"status": "error", "message": "Todo not found."}

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
        return {
            "status": "success",
            "entity_public_id": public_id,
            "entity_type": "todo",
            "summary": f"Deleted todo '{todo_title}'",
        }

    async def _resolve_spending_account(
        self, account_name: str
    ) -> tuple[Account | None, dict | None]:
        """Resolve a spoken account reference against active spending-eligible
        accounts (spec-059). Voice transcripts rarely reproduce stored casing,
        so match in loosening order: normalized exact → unique containment →
        unique account-type word ("wallet", "card"). Ambiguity or a miss
        returns a structured `needs_account` error so the agent asks one short
        question instead of failing on the exact name.
        """
        accounts, _ = await self.account_repo.list_workspace_accounts(
            self.workspace_id, limit=200, offset=0
        )
        candidates = [
            a for a in accounts if a.is_active and a.account_type in _SPENDING_ACCOUNT_TYPES
        ]
        query = _normalize_name(account_name)

        matched = [a for a in candidates if _normalize_name(a.name) == query]
        if not matched:
            matched = [
                a
                for a in candidates
                if query in _normalize_name(a.name) or _normalize_name(a.name) in query
            ]
        if not matched:
            # Whole-phrase type match first ("gift card"), then a type word
            # anywhere in the reference ("the card", "my wallet").
            tokens = set(query.split())
            type_matches = {
                t for t in _SPENDING_ACCOUNT_TYPES if t.value.replace("_", " ") == query
            }
            if not type_matches:
                type_matches = {t for t in _SPENDING_ACCOUNT_TYPES if t.value in tokens}
            matched = [a for a in candidates if a.account_type in type_matches]

        if len(matched) == 1:
            return matched[0], None
        if matched:
            names = sorted(a.name for a in matched)
            return None, {
                "status": "error",
                "needs_account": True,
                "candidates": names,
                "message": (
                    f"Multiple accounts match '{account_name}'. "
                    f"Ask the user to pick one of: {', '.join(names)}."
                ),
            }
        names = sorted(a.name for a in candidates)
        return None, {
            "status": "error",
            "needs_account": True,
            "available_accounts": names,
            "message": (
                f"No spending account matches '{account_name}'. "
                f"Ask the user to pick one of: {', '.join(names) or '(no spending accounts exist)'}."
            ),
        }

    async def log_spending_transaction(
        self,
        amount: str,
        category_name: str,
        description: str,
        account_name: str | None = None,
        occurred_at: str | None = None,
    ) -> dict:
        """Record/log a new spending transaction (expense).

        Args:
            amount: The transaction amount as a string (e.g., '14.99').
            category_name: The name of the spending category (e.g., 'food', 'utilities', 'shopping').
            description: Description of what the money was spent on.
            account_name: Optional account name to attach to the transaction.
            occurred_at: Optional occurrence date (ISO date or date-time); defaults to now.
        """
        try:
            amt = Decimal(amount)
        except Exception:
            return {
                "status": "error",
                "message": "Invalid amount format. Must be a decimal number.",
            }

        # Resolve the occurrence date (spec-061). Omitted → now. A bare date is
        # anchored to noon in the user's timezone; future *days* are refused,
        # while a same-day future instant clamps to now so "log X today" never
        # errors on a morning-vs-noon-local skew.
        now = datetime.now(UTC)
        if occurred_at and occurred_at.strip():
            try:
                resolved_occurred_at = _parse_occurred_at(occurred_at, self.user_timezone)
            except ValueError:
                return {
                    "status": "error",
                    "message": "Invalid date. Use a day like 'yesterday' or an ISO date.",
                }
            try:
                tz: tzinfo = ZoneInfo(self.user_timezone) if self.user_timezone else UTC
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                tz = UTC
            if resolved_occurred_at.astimezone(tz).date() > now.astimezone(tz).date():
                return {
                    "status": "error",
                    "message": "I can't log a spend for a future date.",
                }
            if resolved_occurred_at > now:
                resolved_occurred_at = now
        else:
            resolved_occurred_at = now

        # Resolve category — exact case/whitespace-insensitive match against the
        # workspace's real categories (which are now injected into the system
        # prompt, so the agent can pick a real name). A miss falls back to
        # "other" but is reported via category_matched=False so the agent can
        # confirm rather than silently mislabel (spec-055).
        cats, _ = await self.category_service.list_categories(
            self.workspace_id, limit=200, offset=0
        )
        normalized_name = category_name.strip().lower()
        category = next((c for c in cats if c.name.strip().lower() == normalized_name), None)
        category_matched = category is not None
        if not category:
            category = next((c for c in cats if c.name.strip().lower() == "other"), None)
        if not category:
            return {
                "status": "error",
                "message": "No suitable spending category found in this workspace.",
            }

        # Resolve account in spec-054 order: named account → workspace default →
        # ask the user. A missing account is a structured `needs_account` error,
        # not a silently account-less row. Named references match fuzzily
        # against spending-eligible accounts (spec-059).
        account_public_id = None
        resolved_account_name = None
        account_obj = None
        # Whitespace-only names count as omitted — an empty normalized query
        # would containment-match every account into a bogus ambiguity error.
        if account_name and account_name.strip():
            account, resolution_error = await self._resolve_spending_account(account_name)
            if resolution_error is not None or account is None:
                return resolution_error
            account_public_id = account.public_id
            resolved_account_name = account.name
            account_obj = account
        else:
            setting = await self.setting_repo.get_by_workspace(self.workspace_id)
            default_account_id = setting.default_spending_account_id if setting else None
            default_account = (
                await self.account_repo.get_by_id(self.workspace_id, default_account_id)
                if default_account_id is not None
                else None
            )
            if default_account and default_account.is_active:
                account_public_id = default_account.public_id
                resolved_account_name = default_account.name
                account_obj = default_account
            else:
                return {
                    "status": "error",
                    "needs_account": True,
                    "message": (
                        "This workspace has no default spending account, so I need to "
                        "know which account this spend belongs to. Ask the user to name one."
                    ),
                }

        payload = TransactionCreate(
            category_id=category.public_id,
            account_id=account_public_id,
            amount=amt,
            type=TransactionType.expense,
            occurred_at=resolved_occurred_at,
            description=description,
        )
        tx = await self.tx_service.create_transaction(
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            tx_in=payload,
            audit_logger=self.audit_logger,
        )

        # Resolve currency symbol for the transaction summary
        symbol = account_obj.default_currency_code
        currency_repo = CurrencyRepository(self.session)
        currency = await currency_repo.get_by_code(account_obj.default_currency_code)
        if currency and currency.symbol:
            symbol = currency.symbol

        return {
            "status": "success",
            "entity_public_id": str(tx.public_id),
            "entity_type": "transaction",
            "amount": str(tx.amount),
            "category": category.name,
            "category_matched": category_matched,
            "description": tx.description,
            "account_name": resolved_account_name,
            "occurred_at": tx.occurred_at.isoformat(),
            "summary": f"Added {symbol}{tx.amount} '{tx.description}' to Spending",
        }

    async def get_investing_summary(self) -> dict:
        """Read-only portfolio summary (spec-059): total value, holdings count,
        cash total, and reporting currency — the same numbers as the dashboard's
        /investing/summary endpoint."""
        summary = await self.summary_service.get_summary(self.workspace_id)

        def _dec(value) -> str | None:
            return str(value) if value is not None else None

        return {
            "status": "success",
            "portfolio_value": _dec(summary.portfolio_value),
            "holdings_count": summary.holdings_count,
            "cash_total": _dec(summary.cash_total),
            "daily_change": _dec(summary.daily_change),
            "reporting_currency": summary.reporting_currency,
            "valuation_status": summary.valuation_status,
        }

    async def log_weight(self, weight_kg: str, note: str | None = None) -> dict:
        """Log a body weight measurement (spec-069). Kilograms only in v1.

        Args:
            weight_kg: The weight in kilograms as a string (e.g., '72.4').
            note: Optional short note.
        """
        try:
            weight = Decimal(weight_kg)
        except Exception:
            return {
                "status": "error",
                "message": "Invalid weight format. Must be a decimal number.",
            }
        if weight <= 0:
            return {"status": "error", "message": "Weight must be a positive number."}

        payload = WeightEntryCreate(measured_at=datetime.now(UTC), weight_kg=weight, note=note)
        try:
            entry = await self.health_service.create_weight_entry(
                self.user_id, self.workspace_id, payload, audit_logger=self.audit_logger
            )
        except Exception:
            await self.session.rollback()
            logger.error("log_weight_failed", exc_info=True)
            return {
                "status": "error",
                "message": "An internal error occurred while executing the tool.",
            }

        return {
            "status": "success",
            "entity_public_id": str(entry.public_id),
            "entity_type": "weight_entry",
            "weight_kg": str(entry.weight_kg),
            "summary": f"Logged weight {entry.weight_kg} kg",
        }

    async def log_medication_event(
        self, name: str, status: str, dose_time: str | None = None
    ) -> dict:
        """Log a medication dose as taken or skipped (spec-069). Never
        guesses on an ambiguous name — asks for clarification instead.

        Args:
            name: The medication's name (fuzzy-matched against active medications).
            status: Either 'taken' or 'skipped'.
            dose_time: Optional ISO date-time for the dose slot; defaults to now.
        """
        status_value = status.strip().lower()
        if status_value not in ("taken", "skipped"):
            return {"status": "error", "message": "status must be 'taken' or 'skipped'."}

        medications = await self.medication_repo.get_active(self.workspace_id)
        normalized_query = _normalize_name(name)
        exact = [m for m in medications if _normalize_name(m.name) == normalized_query]
        candidates = exact or [
            m for m in medications if normalized_query in _normalize_name(m.name)
        ]
        if not candidates:
            return {"status": "error", "message": f"No active medication named '{name}' found."}
        if len(candidates) > 1:
            return {
                "status": "error",
                "needs_medication": True,
                "candidates": [c.name for c in candidates],
                "message": f"Multiple medications match '{name}' — which one did you mean?",
            }
        medication = candidates[0]

        if dose_time and dose_time.strip():
            try:
                scheduled_for = _parse_due_datetime(dose_time)
            except ValueError:
                return {"status": "error", "message": "Invalid time format. Use an ISO date-time."}
        else:
            scheduled_for = datetime.now(UTC)

        payload = MedicationEventUpsert(scheduled_for=scheduled_for, status=status_value)
        try:
            event = await self.health_service.upsert_event(
                self.user_id,
                self.workspace_id,
                medication.public_id,
                payload,
                audit_logger=self.audit_logger,
            )
        except Exception:
            await self.session.rollback()
            logger.error("log_medication_event_failed", exc_info=True)
            return {
                "status": "error",
                "message": "An internal error occurred while executing the tool.",
            }

        return {
            "status": "success",
            "entity_public_id": str(event.public_id),
            "entity_type": "medication_event",
            "medication_name": medication.name,
            "status_logged": event.status,
            "summary": f"Logged {medication.name} as {event.status}",
        }
