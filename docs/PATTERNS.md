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

    async def get_todo(self, workspace_id: int, public_id: uuid.UUID) -> Todo: ...

    async def create_todo(self, user_id: int, workspace_id: int, todo_in: TodoCreate) -> Todo: ...

    async def update_todo(
        self, workspace_id: int, public_id: uuid.UUID, todo_in: TodoUpdate
    ) -> Todo: ...
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

The `app/application/` layer is split into two files with a hard boundary:

```text
jobs.py      → scheduler wrappers: advisory lock, iteration, sessions, timeout, error isolation
workflows.py → business logic: receives a session + workspace object, returns a result
```

Jobs call workflows. Workflows never import from `jobs.py`.

```python
# app/application/jobs.py — per-workspace jobs go through run_workspace_job
from app.application.workflows import evaluate_workspace_budget_guardrails
from app.core.constants import ADVISORY_LOCK_BUDGET_GUARDRAILS


async def budget_guardrails_job(workspace_id: int | None = None) -> None:
    await run_workspace_job(
        job_name="budget_guardrails",
        lock_key=ADVISORY_LOCK_BUDGET_GUARDRAILS,
        process_workspace=evaluate_workspace_budget_guardrails,
        workspace_id=workspace_id,
    )
```

`run_workspace_job` (in `jobs.py`) owns the scaffolding: session-level advisory
lock, workspace iteration, per-workspace transactions with timeout, failure
isolation, and the standard log events (`{job_name}_start`,
`_skipped_lock_held`, `_workspace_success`, `_workspace_timeout`,
`_workspace_failed`, `_completed`).

**Connection discipline (do not regress this):** a job run holds exactly ONE
pooled connection. The advisory lock is a *session-level* lock
(`pg_try_advisory_lock` + `finally` unlock) taken on the same session that does
the per-workspace work — session-level locks survive `COMMIT`, so each
workspace still commits in its own transaction. The old shape (an outer
lock-holder session kept open while a second per-workspace session was checked
out inside the loop) could deadlock the pool under concurrent job runs and was
removed in PR #119; `test_scheduler.py` has regression tests asserting the
single-connection property. Two more rules baked into the wrapper:

- Fetch workspace **ids**, not ORM objects, before the loop: a per-workspace
  rollback expires every object loaded on the shared session, and touching an
  expired attribute afterwards raises under async lazy-load. Re-fetch the
  `Workspace` inside each per-workspace transaction.
- Advisory-lock keys are registered centrally in `app/core/constants.py`.

Jobs with genuinely different shapes (`recurring_transactions_job`,
`weekly_summary_job` — custom log-event names and per-run aggregation) keep
their own scaffolding but follow the same single-connection pattern.

```python
# app/core/scheduler.py
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.application.jobs import budget_guardrails_job
from app.config import settings

scheduler = AsyncIOScheduler()


def configure_scheduler() -> None:
    if not settings.SCHEDULER_ENABLED:
        return
    scheduler.add_job(
        budget_guardrails_job,
        "interval",
        hours=settings.BUDGET_GUARDRAILS_INTERVAL_HOURS,
        id="budget_guardrails",
        replace_existing=True,
    )
```

**Notes:**
- **Gating**: `SCHEDULER_ENABLED` must be `true` to register any jobs. Only one process instance should have this set in production.
- **Split-brain prevention**: Postgres advisory transaction lock (`pg_try_advisory_xact_lock`) ensures only one instance executes concurrently during rolling deploys. *Make sure the lock transaction session is held open for the entire duration of the task execution, not closed before the actual background processing starts (since the lock is transaction-scoped and released automatically when the transaction ends).*
- **Per-workspace timeout**: `asyncio.wait_for(..., timeout=300.0)` abandons a stuck workspace after 5 minutes. This prevents blocking application shutdown.
- **Failure isolation**: One workspace exception is caught, logged with `exc_info=True`, and skipped. The remaining workspaces continue.
- **Transaction boundary**: Each workspace runs inside its own `async with session.begin()`. Commit and rollback are managed by the context manager, not by the workflow.

---

## Audit Logging

Audit logging is implemented in `app/core/audit.py` and injected via FastAPI dependencies.

### Model

```python
class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_logs"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(index=True)
    actor_id: int  # authenticated user who triggered the action
    action: str  # "create" | "update" | "complete" | "delete" | "budget_guardrail_triggered"
    module: str  # "todo" | "spending" | "application"
    entity_type: str  # e.g. "todo", "spending_transaction"
    entity_id: int  # internal PK of the affected record
    details: dict = Field(default_factory=dict, sa_column=Column(JSONB))
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True)),
    )
```

### Helper and event contract

```python
class AuditLogger:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def log(
        self,
        *,
        workspace_id: int,
        actor_id: int,
        action: str,  # "create" | "update" | "complete" | "delete" | custom
        module: str,
        entity_type: str,
        entity_id: int,
        details: dict,  # must include: entity_public_id, before, after, changed_fields
    ) -> AuditLog:
        ...
        # Validates required contract keys
        # Validates action-level rules (create → before=None, delete → after=None)
        # Applies PII/secrets redaction to `details`
        # Calls session.flush() — NOT session.commit()
        # Emits logger.info("audit_log_written", ...)
```

**Event contract** — all callers must supply these keys in `details`:

| Key | Type | Rule |
|---|---|---|
| `entity_public_id` | `str` | Always present |
| `before` | `dict \| None` | `None` for `create` actions |
| `after` | `dict \| None` | `None` for `delete` actions |
| `changed_fields` | `list[str]` | Fields that differ between before and after |
| `request_id` | `str \| None` | Optional correlation ID from structlog context |

### Injection in routers

```python
@router.post("/", response_model=TodoResponse, status_code=201)
async def create_todo(
    todo_in: TodoCreate,
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
):
    return await todo_service.create_todo(
        user_id=user["id"],
        workspace_id=workspace_id,
        todo_in=todo_in,
        audit_logger=audit_logger,
    )
```

### Atomicity guarantee

`AuditLogger.log()` calls `session.flush()`, not `session.commit()`. The audit row is part of the same DB transaction as the business mutation:
- If the request handler's transaction commits → audit row is persisted.
- If the transaction rolls back → audit row is discarded.

This is enforced by injecting the **same session** for both the repository and the `AuditLogger` via the same FastAPI dependency.

**Notes:**
- Never pass a different session to `AuditLogger` than the one used by the repository in the same request.
- Audit writes belong in the **service or workflow layer**, not inside generic repository methods.
- Sensitive keys (`password`, `token`, `api_key`, `secret`, `account_number`, etc.) are recursively redacted to `[REDACTED]` before the `details` dict is stored.

---

## Centralized Exceptions (RFC 7807)

```python
# app/core/exceptions.py
from fastapi import Request
from fastapi.responses import JSONResponse

PROBLEM_JSON = "application/problem+json"
ERROR_BASE_URI = "https://lifestack.app/errors"


class APIError(Exception):
    type_str = "internal-server-error"
    title = "Internal Server Error"
    status_code = 500

    def __init__(
        self,
        detail: str,
        *,
        type_str: str | None = None,
        title: str | None = None,
        status_code: int | None = None,
        **extra_fields: object,
    ):
        if type_str is not None:
            self.type_str = type_str
        if title is not None:
            self.title = title
        if status_code is not None:
            self.status_code = status_code
        self.detail = detail
        self.extra_fields = extra_fields

    @property
    def type_uri(self) -> str:
        return f"{ERROR_BASE_URI}/{self.type_str}"


class NotFoundError(APIError):
    type_str = "not-found"
    title = "Not Found"
    status_code = 404


async def api_exception_handler(request: Request, exc: APIError) -> JSONResponse:
    body: dict[str, object] = {
        "type": exc.type_uri,
        "title": exc.title,
        "status": exc.status_code,
        "detail": exc.detail,
        "instance": str(request.url.path),
    }
    if exc.extra_fields:
        body.update(exc.extra_fields)
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        media_type=PROBLEM_JSON,
    )
```

**Notes:**
- All error responses follow [RFC 7807 Problem Details](https://www.rfc-editor.org/rfc/rfc7807).
- `type` field provides a machine-readable error URI.
- `instance` is the request path that triggered the error.
- Subclasses set class-level defaults for `type_str`, `title`, and `status_code`; call sites only need `detail`.
- Runtime overrides are supported for ad-hoc error responses.
- Consistent across all modules — clients can parse errors uniformly.

---

## Dependencies and Session Management

```python
# app/core/dependencies.py
from fastapi import Depends, Request

from app.core.exceptions import UnauthorizedError


async def get_current_user(request: Request) -> dict:
    if not hasattr(request.state, "user_id") or not request.state.user_id:
        raise UnauthorizedError(detail="Not authenticated")
    return {"id": request.state.user_id, "username": request.state.username}


async def get_current_workspace_id(
    request: Request,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
    category_service: CategoryService = Depends(get_spending_category_service),
) -> int:
    if not hasattr(request.state, "user_id") or not request.state.user_id:
        raise UnauthorizedError(detail="Not authenticated")

    workspaces = await workspace_service.get_user_workspaces(request.state.user_id)
    if not workspaces:
        workspace = await workspace_service.ensure_default_workspace(
            request.state.user_id, request.state.username
        )
        await category_service.provision_default_categories(workspace.id)
        return workspace.id

    return workspaces[0].id
```

**Notes:**
- Session management should come from a DB dependency such as `get_db_session`, not be recreated ad hoc in each router.
- Auth middleware populates request user context before route handlers run.
- A dedicated dependency resolves the active `workspace_id` for the request.
- Stage 1 can resolve the first/default workspace for a user, but repositories and services should still operate on `workspace_id`.
- If fallback provisioning is enabled, it should seed the workspace and default spending categories in the same request transaction.

---

## Auth Middleware (Carried from To-Do)

```python
async def auth_middleware(request: Request, call_next):
    """Read JWT, validate it, enforce session/CSRF rules, then populate request state."""
    if request.method == "OPTIONS":
        return await call_next(request)

    normalized_path = request.url.path.rstrip("/") or "/"
    if normalized_path in PUBLIC_PATHS:
        return await call_next(request)

    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

    username, user_id, sid = get_user_info_from_token(token)
    request.state.user_id = int(user_id)
    request.state.username = username
    request.state.sid = sid

    if token_from_cookie and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        ...  # validate Origin against trusted origins when provided

    async with postgres.async_session_maker() as session:
        auth_session = await AuthSessionRepository(session).get_active_by_sid(sid, user_id)
    if not auth_session:
        ...

    return await call_next(request)
```

**Notes:**
- Tokens are accepted from HttpOnly cookies or `Authorization: Bearer ...` headers.
- Public paths are whitelisted; everything else requires a valid token.
- `request.state` is populated for downstream dependencies such as `get_current_user()` and `get_current_workspace_id()`.
- Session revocation is enforced server-side by validating the JWT `sid` against `auth_sessions`.
- Cookie-authenticated mutating requests validate `Origin` against trusted origins when the header is present.

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
# app/core/dependencies.py (rate limiter section)
from slowapi import Limiter
from slowapi.util import get_remote_address
from app.config import settings


def _rate_limit_key_func(request):
    """Use authenticated user ID when available, fall back to IP."""
    if hasattr(request.state, "user_id") and request.state.user_id:
        return f"user:{request.state.user_id}"
    return get_remote_address(request)


limiter = Limiter(
    key_func=_rate_limit_key_func,
    storage_uri=settings.RATE_LIMIT_STORAGE_URI,
)
```

**Usage in routers:**
```python
@router.post("/login")
@limiter.limit(settings.rate_limit_auth)  # 5/minute
def login(request: Request, ...):
    ...
```

**Notes:**
- `memory://` is acceptable for local dev and tests; production should point `RATE_LIMIT_STORAGE_URI` at Redis.
- Per-user limits when authenticated (prefixed with `user:` to avoid key collisions with IP addresses), per-IP for anonymous endpoints.
- Configurable via environment variables (`RATE_LIMIT_DEFAULT`, `RATE_LIMIT_AUTH`).
- 429 responses should go through the same RFC 7807 problem-details handler as the rest of the API.

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
    exporter = OTLPSpanExporter(endpoint=f"{settings.otel_exporter_otlp_endpoint}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
```

---

## Security Middleware (Carried from To-Do)

### OWASP Security Headers

```python
# Inline middleware in main.py
@_app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    normalized = request.url.path.rstrip("/") or "/"
    is_docs = normalized in {"/docs", "/openapi.json"} or normalized.startswith("/docs/")
    if not is_docs:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'"
        )
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response
```

### Metrics Endpoint Protection

```python
import secrets


def verify_metrics_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Protect /metrics with bearer token using constant-time comparison."""
    expected_token = settings.METRICS_TOKEN
    if not secrets.compare_digest(credentials.credentials, expected_token):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid metrics token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials


# In main.py:
@_app.get("/metrics", tags=["health"])
async def metrics_endpoint(token: str = Depends(verify_metrics_token)):
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

**Notes:**
- Metrics endpoint is secure by default; requires a valid `METRICS_TOKEN` bearer token.
- Constant-time comparison (`secrets.compare_digest`) prevents timing attacks.
- `METRICS_TOKEN` must be changed from its default in production (enforced by `Settings` validator).

## Data Aggregation & Dashboard Patterns
### SQL-Level Counting and Summing
Do not fetch full lists of ORM objects into memory just to count or sum them (e.g., `len(objects)` or `sum(obj.amount for obj in objects)`). This causes severe memory bloat and violates latency targets as workspaces grow.
Instead, create explicit aggregation methods on the corresponding Repository that push the operations down to the database using `func.count()`, `func.sum()`, and conditional aggregations with `case()`.

**Anti-Pattern:**
```python
# Bad: fetches 1000 objects into memory
todos, _ = await self.todo_service.list_todos(limit=1000)
open_count = len(todos)
overdue_count = sum(1 for t in todos if t.due_date < now)
```

**Pattern:**
```python
# In Repository:
query = select(
    func.count().label("open_count"),
    func.sum(case((Todo.due_date < now, 1), else_=0)).label("overdue_count")
).where(Todo.workspace_id == workspace_id)

result = await self.session.execute(query)
row = result.mappings().first()
open_count = row.get("open_count") or 0

### N+1 Query Prevention in Loops
Do not perform database queries inside loop iterations. Fetching related data or checking state (e.g., checking if a system todo exists for each category) inside a loop results in the N+1 query problem, creating severe performance degradation as the size of the collection grows.
Instead, pre-fetch all matching records once using a single batch query (e.g., using `.like()` or `.in_()`), build a lookup dictionary in memory, and look up the data from the dictionary during iteration.

**Anti-Pattern:**
```python
# Bad: fetches existing todo inside the loop (N+1 queries)
for budget in budgets:
    todo_res = await session.execute(
        select(Todo).where(Todo.system_key == f"budget:guardrail:{budget.category_id}")
    )
    todo = todo_res.scalar()
```

**Pattern:**
```python
# Good: pre-fetch all system todos in a single query
todos_res = await session.execute(
    select(Todo).where(
        Todo.workspace_id == workspace.id,
        Todo.system_key.like("budget:guardrail:%"),
    )
)
todos_map = {todo.system_key: todo for todo in todos_res.scalars().all()}

for budget in budgets:
    todo = todos_map.get(f"budget:guardrail:{budget.category_id}")
```

### Boolean Sorting in SQL
When sorting query results by a boolean expression (e.g., `role == "owner"` or checking status), be aware of database sorting behavior. In PostgreSQL, boolean expressions evaluate to `true` (1) or `false` (0). By default, ascending order puts `false` before `true`, meaning `role != "owner"` elements will be returned first.
To prioritize `true` elements first (e.g., to ensure workspace owners are resolved first), you must explicitly apply descending ordering using `.desc()`.

**Anti-Pattern:**
```python
# Bad: sorts False (non-owner) before True (owner)
select(WorkspaceMembership.user_id)
.order_by(WorkspaceMembership.role == "owner")
```

**Pattern:**
```python
# Good: sorts True (owner) before False (non-owner)
select(WorkspaceMembership.user_id)
.order_by((WorkspaceMembership.role == "owner").desc())
```

**Notes:**
- Postgres Advisory Locks: Always use session-level advisory locks (`pg_advisory_xact_lock`) for operations that span multiple queries to prevent race conditions. Ensure the lock key is scoped by `workspace_id` to prevent cross-workspace contention.

## Validation Robustness

### Exception Handler Robustness
Backend exception handlers (like `request_validation_exception_handler`) must be extremely robust. If a logger fails to serialize a validation error, the client receives a `500` instead of a `422`. Always stringify or safely serialize error details before logging.

**Pattern:**
```python
# In app/core/exceptions.py
from fastapi.encoders import jsonable_encoder

async def request_validation_exception_handler(request, exc):
    # Safe logging: ensure exc.errors() is serializable while preserving structure
    logger.warning("validation_error", errors=jsonable_encoder(exc.errors()), url=str(request.url))
    return JSONResponse(
        status_code=422,
        content={
            "type": "https://lifestack.app/errors/validation-error",
            "title": "Request Validation Error",
            "status": 422,
            "detail": "The request payload or parameters are invalid.",
            "errors": exc.errors(),
            "instance": str(request.url.path),
        },
    )


## Response Helpers & FK Resolution in Services

To keep router files slim and isolate HTTP concerns from domain mapping, all foreign key resolution, cache building, and entity-to-response mapping should be handled within service helper functions or dedicated `response_helpers.py` files.

### Naming Conventions

- **`_to_response` mapping helper**: For simple entities, use a helper of the signature `_to_response(entity: Model, **caches) -> ResponseSchema`.
- **`_build_X_cache` cache builder**: When resolving batch relationships (e.g. looking up accounts or import batches to prevent N+1 queries), implement internal cache builders returning a mapping:
  `async def _build_X_cache(self, workspace_id: int, items: list) -> dict`
- **Detailed wrapper methods**: Expose methods on the service class named `[action]_with_details` (e.g., `list_holdings_with_details`) that fetch the raw entities, build the necessary caches, construct the detailed response schemas using helpers, and return them directly to the router.

This ensures routers only delegate to a single service call and return the validated response model directly.
```

---

## Shared Core Helpers (use these, don't re-roll them)

Added across the 2026-07 codebase-improvement batch. New code should reach for
these instead of re-implementing the pattern locally:

- **`app/core/repository.py` — `BaseRepository[T]`**: shared persistence
  primitives (`create`/`save` = add + flush + refresh, `delete`).
  Repositories subclass it and add their domain query methods (including
  `get_by_public_id`, which stays per-repository because workspace scoping
  differs). All 17+ repository classes are on it; a new module's repository
  starts as `class XRepository(BaseRepository[X])`.
- **`app/core/pagination.py` — `build_page(items, total, pagination)`**:
  builds the standard paginated list envelope. Every list endpoint uses it —
  do not hand-assemble `{items, total, limit, offset}` dicts in routers.
- **`app/core/audit.py` — `snapshot_columns(entity, fields)`**: constructs
  audit-log payloads from a per-service field tuple, preserving str/isoformat
  conversions. Audit payload field sets are contract — change the field tuple,
  not the helper.
- **`app/core/currency.py`**: centralized FX/currency conversion helpers
  (moved out of `investing/performance_service.py`); deliberately decoupled
  from `app.finance` models.
- **`app/core/recurrence.py` — `advance_due_date(...)`**: shared recurrence
  math (spec-053 monthly modes, anchor-day clamping) used by both spending
  and todo. Recurrence bugs get fixed here, once.
- **`app/application/jobs.py` — `run_workspace_job(...)`**: per-workspace
  scheduled-job scaffolding — see the Scheduled Jobs section above.

Frontend counterparts in `lifestack-web` (documented in that repo's
`docs/PATTERNS.md`): `src/hooks/useInvalidatingMutation.ts` (mutate +
invalidate query keys) and `src/lib/queryKeys.ts` (the module-scoped query-key
registry — never inline raw key arrays in pages).

## Import Module Split (`app/imports/`)

`ImportService` (`app/imports/service.py`) was a single ~2,445-line class with
per-`ImportModule` if/elif branches running through `validate_batch_file` and
`commit_batch`. It's now a thin façade: `ImportService` keeps the shared
upload/temp-file handling, the generic CSV/XLSX row-iteration and chunked-commit
harness, and dispatch — the actual per-format validation and commit logic
lives in its own module, one per import format:

- `app/imports/cams_cas_import.py` + `cams_cas_parser.py` — CAMS CAS (mutual funds)
- `app/imports/demat_cas_import.py` + `demat_cas_parser.py` — Demat CAS (NSDL holdings verification, spec-060)
- `app/imports/finance_transfers_import.py` — `finance-transfers`
- `app/imports/investing_constituents_import.py` — `investing-constituents`
- `app/imports/investing_orders_import.py` — `investing-orders`
- `app/imports/spending_import.py` — `spending-transactions` and `spending-budgets`
- `app/imports/shared.py` — small helpers shared across the above

Router imports (`app.core.dependencies.get_import_service` →
`ImportService(repo, session, order_service=...)`) are unchanged — callers
never see the split. **When adding a new import format** (e.g. a future
registrar/statement layout), add a new `<format>_import.py` with a
`validate_<format>_upload`/`validate_<format>_batch`/`commit_<format>_chunk`
(or equivalent) triplet and wire it into `ImportService`'s three dispatch
points, rather than growing the inline if/elif chains again.
