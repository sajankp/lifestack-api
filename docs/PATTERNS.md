# Lifestack - Code Patterns and Reference Implementations

> Concrete examples for the patterns described in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Module Shape

Every domain module should keep the same layered shape:

```text
models.py       -> persistence model definitions
schemas.py      -> request / response contracts
repository.py   -> database access scoped by workspace
service.py      -> domain logic for one module
router.py       -> HTTP layer and dependency resolution
```

The todo module is the reference for this pattern, but this file intentionally shows only the stable shape and boundaries, not every current implementation detail.

### Model Pattern

Use models that separate internal and external identity and carry tenant context explicitly:

```python
class Todo(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    title: str = Field(max_length=100)
    description: str | None = Field(default="", max_length=500)
    due_date: datetime | None = None
    priority: PriorityEnum = Field(default=PriorityEnum.medium)
    completed: bool = Field(default=False)
```

**Notes:**
- `id` is the internal PK.
- `public_id` is the external identifier exposed to clients.
- `workspace_id` is the tenant boundary for access control and query scoping.
- `user_id` can record ownership or creator metadata, but it should not replace workspace scoping.

### Schema Pattern

Keep create/update/response schemas explicit and narrow:

```python
class TodoBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(default="", max_length=500)
    due_date: datetime | None = None
    priority: PriorityEnum = Field(default=PriorityEnum.medium)
    completed: bool = Field(default=False)


class TodoCreate(TodoBase):
    pass


class TodoUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)
    due_date: datetime | None = None
    priority: PriorityEnum | None = None
    completed: bool | None = None


class TodoResponse(TodoBase):
    public_id: uuid.UUID
    workspace_id: int
    created_at: datetime
    updated_at: datetime
```

**Notes:**
- Use `TodoCreate` for required/allowed create fields.
- Use partial update schemas with `exclude_unset=True`.
- Keep response schemas client-facing; expose `public_id`, not internal `id`.

### Repository Pattern

Repositories own DB access and must always scope reads and writes by workspace:

```python
class TodoRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_all(self, workspace_id: int, completed: bool | None = None) -> Sequence[Todo]:
        query = select(Todo).where(Todo.workspace_id == workspace_id)
        if completed is not None:
            query = query.where(Todo.completed == completed)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> Todo | None:
        query = select(Todo).where(
            Todo.workspace_id == workspace_id,
            Todo.public_id == public_id,
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
```

**Notes:**
- Every query is scoped by `workspace_id`.
- Repositories should use `flush()`, not `commit()`, so transaction boundaries stay above the repository layer.
- Return domain objects, not ad-hoc dicts.

### Service Pattern

Services hold module business logic and accept workspace context explicitly:

```python
class TodoService:
    def __init__(self, repository: TodoRepository):
        self.repository = repository

    async def list_todos(self, workspace_id: int, completed: bool | None = None) -> Sequence[Todo]:
        return await self.repository.get_all(workspace_id, completed)

    async def get_todo(self, workspace_id: int, public_id: uuid.UUID) -> Todo:
        ...

    async def create_todo(self, user_id: int, workspace_id: int, todo_in: TodoCreate) -> Todo:
        ...

    async def update_todo(self, workspace_id: int, public_id: uuid.UUID, todo_in: TodoUpdate) -> Todo:
        ...
```

**Notes:**
- Services should not read `Request` objects directly.
- Services should not import other module services for cross-module orchestration; use `application/` workflows for that.
- Missing entity lookups should fail consistently for the active workspace.

### Router Pattern

Routers should stay thin and resolve authenticated user plus active workspace via dependencies:

```python
@router.post("/", response_model=TodoResponse, status_code=status.HTTP_201_CREATED)
async def create_todo(
    todo_in: TodoCreate,
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    return await todo_service.create_todo(user["id"], workspace_id, todo_in)
```

**Notes:**
- Routers validate/deserialize input, resolve dependencies, call the service, and return the result.
- The active workspace comes from a dependency, not from route params.
- External routes use `public_id` UUIDs for entity lookup.

---

## Cross-Module Workflows

Workflows live in `app/application/` and compose multiple module services. They are the only place where modules interact.

```python
class SomeWorkflow:
    def __init__(self, service_a, service_b):
        self.service_a = service_a
        self.service_b = service_b

    async def run(self, workspace_id: int) -> None:
        data = await self.service_a.get_state(workspace_id)
        if data.requires_followup:
            await self.service_b.handle_followup(workspace_id, data)
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
from fastapi import Depends, HTTPException, Request

from app.core.database.postgres import get_db_session

async def get_current_user(request: Request) -> dict:
    if not hasattr(request.state, "user_id") or not request.state.user_id:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"id": request.state.user_id, "username": request.state.username}


async def get_current_workspace_id(
    request: Request,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> int:
    if not hasattr(request.state, "user_id") or not request.state.user_id:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    workspaces = await workspace_service.get_user_workspaces(request.state.user_id)
    if not workspaces:
        workspace = await workspace_service.ensure_default_workspace(
            request.state.user_id, request.state.username
        )
        return workspace.id

    return workspaces[0].id
```

**Notes:**
- Session management should come from a DB dependency such as `get_db_session`, not be recreated ad hoc in each router.
- Auth middleware populates request user context before route handlers run.
- A dedicated dependency resolves the active `workspace_id` for the request.
- Stage 1 can resolve the first/default workspace for a user, but repositories and services should still operate on `workspace_id`.

---

## Auth Middleware (Carried from To-Do)

```python
async def auth_middleware(request: Request, call_next):
    """Read JWT from HttpOnly cookie, validate, and populate request state."""
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
- `request.state` is populated for downstream dependencies such as `get_current_user()` and `get_current_workspace_id()`.
- CSRF origin checks should be added for mutating requests when `SameSite=None`.

---

## Wiring in `main.py`

```python
app = FastAPI(...)

register_exception_handlers(app)
app.middleware("http")(auth_middleware)

app.include_router(auth_router, prefix="/v1/auth")
app.include_router(todo_router, prefix="/v1")
```

**Notes:**
- Apply auth, exception, and routing concerns at the application boundary.
- Version routers at inclusion time, not by hardcoding version segments inside every endpoint.

---

## Testing Pattern

```python
# app/todo/tests/test_service.py
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.todo.models import Todo
from app.todo.schemas import TodoCreate, TodoUpdate
from app.todo.service import TodoService


@pytest.fixture
def mock_repo():
    repo = AsyncMock()
    repo.create.return_value = Todo(
        id=1,
        public_id=uuid.uuid4(),
        workspace_id=1,
        user_id=10,
        title="Test",
        completed=False,
    )
    return repo


@pytest.fixture
def service(mock_repo):
    return TodoService(repository=mock_repo)


@pytest.mark.asyncio
async def test_create_todo(service, mock_repo):
    result = await service.create_todo(10, 1, TodoCreate(title="Buy groceries"))
    mock_repo.create.assert_called_once()
    assert result.title == "Test"
    assert result.workspace_id == 1


@pytest.mark.asyncio
async def test_get_todo_not_found(service, mock_repo):
    mock_repo.get_by_public_id.return_value = None
    with pytest.raises(HTTPException):
        await service.get_todo(1, uuid.uuid4())
```

**Notes:**
- Repository is mocked; tests exercise business logic, not the database.
- Add separate integration tests that hit the real DB through the full stack.
- Include workspace isolation scenarios in integration tests, not only happy-path CRUD.

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
