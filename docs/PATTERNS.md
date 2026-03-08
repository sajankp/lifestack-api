# Lifestack - Code Patterns and Reference Implementations

> Concrete examples for the patterns described in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Module Shape

Every domain module follows the same four-file structure. The todo module is used as the reference.

### Models

```python
# app/todo/models.py
import uuid
from datetime import datetime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel


class Todo(SQLModel, table=True):
    __tablename__ = "todos"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(index=True, foreign_key="workspaces.id")
    title: str
    priority: str = "medium"
    status: str = "pending"
    due_date: datetime | None = None
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSONB))
    notes: str | None = None
    metadata_: dict = Field(
        default_factory=dict,
        sa_column=Column("metadata", JSONB),
    )
    system_key: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
```

**Notes:**
- Internal primary keys use `BIGINT`; `public_id` is the external-facing UUID.
- `workspace_id` is an internal foreign key on every business table.
- `default_factory` avoids shared mutable defaults for lists and dicts.
- `system_key` is optional and used to deduplicate system-generated tasks.
- If subtasks drive business logic later, promote them to a separate table.

### Schemas

```python
# app/todo/schemas.py
import uuid
from datetime import datetime
from pydantic import BaseModel, Field as PydanticField


class TodoCreate(BaseModel):
    title: str
    priority: str = "medium"
    due_date: datetime | None = None
    tags: list[str] = PydanticField(default_factory=list)
    notes: str | None = None


class TodoUpdate(BaseModel):
    title: str | None = None
    priority: str | None = None
    status: str | None = None
    due_date: datetime | None = None
    tags: list[str] | None = None
    notes: str | None = None


class TodoResponse(BaseModel):
    public_id: uuid.UUID
    title: str
    priority: str
    status: str
    due_date: datetime | None
    tags: list[str]
    notes: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
```

### Repository

```python
# app/todo/repository.py
import uuid
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from app.todo.models import Todo


class TodoRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def find_by_workspace(
        self, workspace_id: int, status: str | None = None
    ) -> list[Todo]:
        query = select(Todo).where(Todo.workspace_id == workspace_id)
        if status:
            query = query.where(Todo.status == status)
        query = query.order_by(Todo.created_at.desc())
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def find_by_public_id(
        self, workspace_id: int, todo_public_id: uuid.UUID
    ) -> Todo | None:
        query = select(Todo).where(
            Todo.workspace_id == workspace_id,
            Todo.public_id == todo_public_id,
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def find_open_system_task(
        self, workspace_id: int, system_key: str
    ) -> Todo | None:
        query = (
            select(Todo)
            .where(
                Todo.workspace_id == workspace_id,
                Todo.system_key == system_key,
                Todo.status != "completed",
            )
            .order_by(Todo.created_at.desc())
        )
        result = await self.session.execute(query)
        return result.scalars().first()

    async def find_recent_system_task(
        self, workspace_id: int, system_key: str, since: datetime
    ) -> Todo | None:
        query = (
            select(Todo)
            .where(
                Todo.workspace_id == workspace_id,
                Todo.system_key == system_key,
                Todo.created_at >= since,
            )
            .order_by(Todo.created_at.desc())
        )
        result = await self.session.execute(query)
        return result.scalars().first()

    async def create(self, todo: Todo) -> Todo:
        self.session.add(todo)
        await self.session.flush()
        await self.session.refresh(todo)
        return todo

    async def update(self, todo: Todo) -> Todo:
        self.session.add(todo)
        await self.session.flush()
        await self.session.refresh(todo)
        return todo

    async def delete(self, todo: Todo) -> None:
        await self.session.delete(todo)
        await self.session.flush()
```

**Notes:**
- Every query is scoped by `workspace_id`; no data leaks between workspaces.
- External lookups use `public_id`; internal joins still use integer primary keys.
- Uses `flush()` instead of `commit()` so the router or a workflow can control transaction boundaries.
- Returns domain objects, not dicts.

### Service

```python
# app/todo/service.py
import uuid
from datetime import datetime, timedelta
from app.core.exceptions import NotFoundError
from app.todo.models import Todo
from app.todo.repository import TodoRepository
from app.todo.schemas import TodoCreate


class TodoService:
    def __init__(self, repo: TodoRepository):
        self.repo = repo

    async def list_todos(
        self, workspace_id: int, status: str | None = None
    ) -> list[Todo]:
        return await self.repo.find_by_workspace(workspace_id, status)

    async def get_todo(self, workspace_id: int, todo_public_id: uuid.UUID) -> Todo:
        todo = await self.repo.find_by_public_id(workspace_id, todo_public_id)
        if not todo:
            raise NotFoundError(f"Todo {todo_public_id} not found")
        return todo

    async def create_todo(self, workspace_id: int, data: TodoCreate) -> Todo:
        todo = Todo(**data.model_dump(), workspace_id=workspace_id)
        return await self.repo.create(todo)

    async def complete_todo(
        self, workspace_id: int, todo_public_id: uuid.UUID
    ) -> Todo:
        todo = await self.get_todo(workspace_id, todo_public_id)
        todo.status = "completed"
        todo.updated_at = datetime.utcnow()
        return await self.repo.update(todo)

    async def ensure_system_task(
        self,
        workspace_id: int,
        system_key: str,
        title: str,
        cooldown_hours: int = 24,
    ) -> Todo:
        """Guarantees at most one open task per rule, with an optional cooldown."""
        existing = await self.repo.find_open_system_task(workspace_id, system_key)
        if existing:
            return existing

        cutoff = datetime.utcnow() - timedelta(hours=cooldown_hours)
        recent = await self.repo.find_recent_system_task(
            workspace_id, system_key, cutoff
        )
        if recent:
            return recent

        todo = Todo(
            workspace_id=workspace_id,
            title=title,
            priority="medium",
            tags=["system-generated"],
            system_key=system_key,
        )
        return await self.repo.create(todo)
```

**Notes:**
- Service never imports another module's service; cross-module logic goes in `application/`.
- `ensure_system_task()` makes background rules idempotent and prevents duplicate task spam.
- Add a partial unique index in a migration if you want database-level enforcement for one open task per `workspace_id + system_key`.

### Router

```python
# app/todo/router.py
import uuid
from fastapi import APIRouter, Depends
from app.core.dependencies import get_current_workspace, get_session
from app.todo.repository import TodoRepository
from app.todo.schemas import TodoCreate, TodoResponse
from app.todo.service import TodoService

router = APIRouter(prefix="/todo", tags=["todo"])


def get_todo_service(session=Depends(get_session)) -> TodoService:
    return TodoService(TodoRepository(session))


@router.get("/", response_model=list[TodoResponse])
async def list_todos(
    status: str | None = None,
    workspace_id: int = Depends(get_current_workspace),
    service: TodoService = Depends(get_todo_service),
):
    return await service.list_todos(workspace_id, status)


@router.post("/", response_model=TodoResponse, status_code=201)
async def create_todo(
    data: TodoCreate,
    workspace_id: int = Depends(get_current_workspace),
    service: TodoService = Depends(get_todo_service),
):
    return await service.create_todo(workspace_id, data)


@router.post("/{todo_id}/complete", response_model=TodoResponse)
async def complete_todo(
    todo_id: uuid.UUID,
    workspace_id: int = Depends(get_current_workspace),
    service: TodoService = Depends(get_todo_service),
):
    return await service.complete_todo(workspace_id, todo_id)
```

**Notes:**
- `get_current_workspace()` returns the internal workspace primary key used for DB access.
- External routes use UUIDs for entity lookup while repositories keep integer joins internally.
- Service is constructed per-request via `Depends`, getting a fresh session each time.

---

## Cross-Module Workflows

Workflows live in `app/application/` and compose multiple module services. They are the only place where modules interact.

```python
# app/application/workflows.py
from app.spending.service import SpendingService
from app.todo.service import TodoService


class BudgetReviewWorkflow:
    def __init__(self, spending_service: SpendingService, todo_service: TodoService):
        self.spending = spending_service
        self.todo = todo_service

    async def check_and_alert(self, workspace_id: int) -> None:
        status = await self.spending.get_budget_status(workspace_id)
        if status.is_over_limit:
            await self.todo.ensure_system_task(
                workspace_id=workspace_id,
                system_key="budget_review",
                title="Review this month's spending",
                cooldown_hours=24,
            )


class WeeklySummaryWorkflow:
    def __init__(self, todo_service, spending_service, investing_service):
        self.todo = todo_service
        self.spending = spending_service
        self.investing = investing_service

    async def generate(self, workspace_id: int) -> dict:
        return {
            "todos_completed": await self.todo.count_completed_this_week(workspace_id),
            "total_spent": await self.spending.get_weekly_total(workspace_id),
            "portfolio_change": await self.investing.get_weekly_change(workspace_id),
        }
```

**Notes:**
- Plain classes with explicit dependencies; no magic, no decorators, no event subscriptions.
- Testable by injecting mock services.
- Can be called from routers, scheduled jobs, or future AI/MCP adapters.
- Background workflows should be idempotent: rerunning the same rule should not create duplicate user-visible tasks.

---

## Scheduled Jobs

```python
# app/application/jobs.py
import logging
from app.application.workflows import BudgetReviewWorkflow
from app.core.database.postgres import get_session_factory
from app.platform.workspaces import WorkspaceRepository
from app.spending.repository import SpendingRepository
from app.spending.service import SpendingService
from app.todo.repository import TodoRepository
from app.todo.service import TodoService

logger = logging.getLogger(__name__)


async def budget_check_job():
    """Runs every 6 hours via APScheduler."""
    async with get_session_factory() as session:
        workspace_ids = await WorkspaceRepository(session).list_active_ids()

    for workspace_id in workspace_ids:
        async with get_session_factory() as session:
            workflow = BudgetReviewWorkflow(
                spending_service=SpendingService(SpendingRepository(session)),
                todo_service=TodoService(TodoRepository(session)),
            )
            try:
                await workflow.check_and_alert(workspace_id)
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception(
                    "Budget check failed for workspace_id=%s", workspace_id
                )


# app/core/scheduler.py
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()
scheduler.add_job(budget_check_job, "interval", hours=6)
scheduler.add_job(process_recurring_transactions, "cron", hour=0)
scheduler.add_job(send_daily_reminders, "cron", hour=9)
scheduler.add_job(generate_weekly_summaries, "cron", day_of_week="mon", hour=8)
```

**Notes:**
- Each workspace is processed in its own transaction.
- One failing workspace does not poison the whole scheduled batch.
- This is a better default than one long-running transaction across all tenants.

---

## Audit Logging

```python
# app/core/audit.py
import uuid
from datetime import datetime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_logs"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(index=True)
    actor_id: int           # user who performed the action
    action: str             # "create", "update", "delete", "complete"
    module: str             # "todo", "spending", "investing"
    entity_id: int          # internal ID of the affected record
    details: dict = Field(default_factory=dict, sa_column=Column(JSONB))
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)


class AuditLogger:
    def __init__(self, session):
        self.session = session

    async def log(
        self,
        workspace_id: int,
        actor_id: int,
        action: str,
        module: str,
        entity_id: int,
        details: dict | None = None,
    ):
        entry = AuditLog(
            workspace_id=workspace_id,
            actor_id=actor_id,
            action=action,
            module=module,
            entity_id=entity_id,
            details=details or {},
        )
        self.session.add(entry)
        # no commit; let the caller's transaction boundary handle it
```

**Notes:**
- `workspace_id` is scoped like everything else.
- Audit logs follow the same internal `BIGINT` + external `public_id` pattern when they need to be exposed.
- No `commit()`; audit writes happen inside the same transaction as the business change.
- `details` is JSONB for per-module flexibility.

---

## Centralized Exceptions (RFC 7807)

```python
# app/core/exceptions.py
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class LifestackError(Exception):
    """Base for all domain errors."""


class NotFoundError(LifestackError):
    pass


class AuthorizationError(LifestackError):
    pass


class ValidationError(LifestackError):
    pass


def _problem_response(request: Request, status: int, title: str, detail: str):
    """Build an RFC 7807 Problem Details response."""
    return JSONResponse(
        status_code=status,
        content={
            "type": f"https://lifestack.app/errors/{title.lower().replace(' ', '-')}",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": str(request.url.path),
        },
    )


def register_exception_handlers(app: FastAPI):
    @app.exception_handler(NotFoundError)
    async def _(request, exc):
        return _problem_response(request, 404, "Not Found", str(exc))

    @app.exception_handler(AuthorizationError)
    async def _(request, exc):
        return _problem_response(request, 403, "Forbidden", str(exc))

    @app.exception_handler(ValidationError)
    async def _(request, exc):
        return _problem_response(request, 422, "Validation Error", str(exc))
```

**Notes:**
- All error responses follow [RFC 7807 Problem Details](https://www.rfc-editor.org/rfc/rfc7807).
- `type` field provides a machine-readable error URI.
- `instance` is the request path that triggered the error.
- Consistent across all modules — clients can parse errors uniformly.

---

## Dependencies and Session Management

```python
# app/core/dependencies.py
from fastapi import HTTPException, Request
from app.core.database.postgres import async_session_maker


async def get_session():
    async with async_session_maker() as session:
        yield session
        await session.commit()
        # on exception: commit is skipped, context manager rolls back automatically


def get_authenticated_user(request: Request):
    """Dependency that retrieves the authenticated user from request state.
    Assumes the auth middleware has already validated the cookie token
    and populated request.state.
    """
    if not hasattr(request.state, "user_id") or not request.state.user_id:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return request.state


async def get_current_workspace(request: Request) -> int:
    """Extract workspace_id from the authenticated user context."""
    user = get_authenticated_user(request)
    return user.workspace_id
```

**Notes:**
- Session auto-commits if no exception is raised and rolls back otherwise.
- Auth is handled by a **cookie middleware** (not `HTTPBearer`), which populates `request.state.user_id` and `request.state.username` before route handlers run.
- `get_current_workspace()` extracts the active internal workspace ID from request state.
- Internal integer IDs are fine inside auth/session context; UUIDs matter mainly for externally referenced resources.

---

## Auth Middleware (Carried from To-Do)

```python
# app/core/auth_middleware.py
from fastapi import Request, status
from fastapi.responses import JSONResponse
from app.core.auth import decode_token, get_user_info_from_token

# Routes that do not require authentication
PUBLIC_PATHS = {
    "/token", "/docs", "/openapi.json", "/redoc",
    "/", "/token/refresh", "/health", "/health/ready",
    "/user", "/metrics", "/auth/logout",
}


async def auth_middleware(request: Request, call_next):
    """Read JWT from HttpOnly cookie, validate, and populate request.state."""
    if request.method == "OPTIONS":
        return await call_next(request)

    normalized_path = request.url.path.rstrip("/") or "/"
    if normalized_path in PUBLIC_PATHS:
        return await call_next(request)

    token = request.cookies.get("access_token")
    if not token:
        return JSONResponse(
            content={"detail": "Missing token"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    try:
        username, user_id = get_user_info_from_token(token)
        request.state.user_id = user_id
        request.state.username = username
    except Exception as e:
        return JSONResponse(
            content={"detail": str(e)},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    return await call_next(request)
```

**Notes:**
- Tokens are read from HttpOnly cookies, not Authorization headers.
- Public paths are whitelisted; everything else requires a valid token.
- `request.state` is populated for downstream route handlers and dependencies.
- CSRF origin checks should be added for mutating requests when `SameSite=None`.

---

## Wiring in `main.py`

```python
# app/main.py
from fastapi import FastAPI
from app.auth.router import router as auth_router
from app.core.exceptions import register_exception_handlers
from app.core.scheduler import scheduler
from app.dashboard.router import router as dashboard_router
from app.investing.router import router as investing_router
from app.spending.router import router as spending_router
from app.todo.router import router as todo_router

app = FastAPI(title="Lifestack API")

register_exception_handlers(app)

app.include_router(auth_router)
app.include_router(todo_router)
app.include_router(spending_router)
app.include_router(investing_router)
app.include_router(dashboard_router)


@app.on_event("startup")
async def startup():
    scheduler.start()


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
```

---

## Testing Pattern

```python
# app/todo/tests/test_service.py
import uuid
import pytest
from unittest.mock import AsyncMock
from app.core.exceptions import NotFoundError
from app.todo.models import Todo
from app.todo.schemas import TodoCreate
from app.todo.service import TodoService


@pytest.fixture
def mock_repo():
    repo = AsyncMock()
    repo.create.return_value = Todo(
        id=1,
        public_id=uuid.uuid4(),
        workspace_id=1,
        title="Test",
        status="pending",
    )
    return repo


@pytest.fixture
def service(mock_repo):
    return TodoService(repo=mock_repo)


@pytest.mark.asyncio
async def test_create_todo(service, mock_repo):
    result = await service.create_todo(1, TodoCreate(title="Buy groceries"))
    mock_repo.create.assert_called_once()
    assert result.title == "Test"
    assert result.workspace_id == 1


@pytest.mark.asyncio
async def test_get_todo_not_found(service, mock_repo):
    mock_repo.find_by_public_id.return_value = None
    with pytest.raises(NotFoundError):
        await service.get_todo(1, uuid.uuid4())
```

**Notes:**
- Repository is mocked; tests exercise business logic, not the database.
- Workflow tests work the same way: mock the module services and assert the orchestration.

---

## Rate Limiting (Carried from To-Do)

```python
# app/core/rate_limiter.py
from slowapi import Limiter
from slowapi.util import get_remote_address
from app.config import get_settings

settings = get_settings()


def _key_func(request):
    """Use authenticated user ID when available, fall back to IP."""
    if hasattr(request.state, "user_id") and request.state.user_id:
        return str(request.state.user_id)
    return get_remote_address(request)


limiter = Limiter(
    key_func=_key_func,
    default_limits=[settings.rate_limit_default],
    storage_uri=settings.redis_url,  # Redis backend (required)
    enabled=settings.rate_limit_enabled,
)
```

**Usage in routers:**
```python
@router.post("/token/")
@limiter.limit(settings.rate_limit_auth)  # 5/minute
def login(request: Request, ...):
    ...
```

**Notes:**
- Redis backend via `REDIS_URL` environment variable.
- Per-user limits when authenticated, per-IP for anonymous endpoints.
- Configurable via environment variables (`RATE_LIMIT_DEFAULT`, `RATE_LIMIT_AUTH`).

---

## Observability Patterns (Carried from To-Do)

### Prometheus Custom Metrics

```python
# app/utils/metrics.py
from prometheus_client import Counter, Gauge, Histogram

LOGINS_TOTAL = Counter("logins_total", "Total login attempts", ["status"])
REGISTRATIONS_TOTAL = Counter("registrations_total", "Total user registrations")
TODOS_CREATED_TOTAL = Counter("todos_created_total", "Total todos created")
TODOS_COMPLETED_TOTAL = Counter("todos_completed_total", "Total todos completed")
TODOS_DELETED_TOTAL = Counter("todos_deleted_total", "Total todos deleted")
TODOS_PER_USER = Histogram("todos_per_user", "Number of todos returned per query")
DB_QUERY_DURATION_SECONDS = Histogram(
    "db_query_duration_seconds", "DB query duration", ["operation"]
)
DB_CONNECTIONS_ACTIVE = Gauge("db_connections_active", "Active DB connections")
```

### Structured Logging Middleware

```python
# app/middleware/logging.py
import structlog
from starlette.middleware.base import BaseHTTPMiddleware

logger = structlog.get_logger()


class StructlogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
        )
        return response
```

### OpenTelemetry Setup

```python
# app/utils/telemetry.py
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup_telemetry(app, settings):
    if not settings.otel_exporter_otlp_endpoint:
        return

    provider = TracerProvider()
    exporter = OTLPSpanExporter(
        endpoint=f"{settings.otel_exporter_otlp_endpoint}/v1/traces"
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
```

---

## Security Middleware (Carried from To-Do)

### OWASP Security Headers

```python
# app/middleware/security.py
from starlette.middleware.base import BaseHTTPMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains"
        )
        # CSP configured via environment variables
        return response
```

### Metrics Endpoint Protection

```python
def verify_metrics_token(request: Request):
    """Protect /metrics with bearer token or dev-mode access."""
    if settings.metrics_bearer_token:
        auth_header = request.headers.get("Authorization")
        expected = f"Bearer {settings.metrics_bearer_token}"
        if not (auth_header and secrets.compare_digest(auth_header, expected)):
            raise HTTPException(status_code=403, detail="Forbidden")
    elif settings.metrics_dev_mode:
        pass  # Allow public access in dev
    else:
        raise HTTPException(status_code=403, detail="Forbidden")

# In main.py:
Instrumentator().instrument(app).expose(
    app, dependencies=[Depends(verify_metrics_token)]
)
```

**Notes:**
- Metrics endpoint is secure by default; requires explicit token or dev-mode opt-in.
- Constant-time comparison (`secrets.compare_digest`) prevents timing attacks.
