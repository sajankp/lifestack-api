import secrets
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis
from redis.asyncio import from_url as redis_from_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_503_SERVICE_UNAVAILABLE

from app.config import settings
from app.core.database.postgres import get_db_session_readonly
from app.core.scheduler import scheduler

logger = structlog.get_logger()

router = APIRouter(tags=["health"])

security = HTTPBearer()


def verify_metrics_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    expected_token = settings.METRICS_TOKEN
    if not secrets.compare_digest(credentials.credentials, expected_token):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid metrics token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials


@router.get("/health")
async def health_check():
    """Basic health check endpoint."""
    return {"status": "ok", "version": settings.VERSION}


@router.get("/metrics")
async def metrics_endpoint(token: str = Depends(verify_metrics_token)):
    """Prometheus metrics endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


_readiness_redis_client: Redis | None = None


def _get_readiness_redis_client() -> Redis:
    """Lazily-created, process-lifetime client — readiness probes are hit
    frequently by orchestrators, so a fresh connection per check would churn
    TCP connections. redis.asyncio.Redis owns an internal connection pool and
    is safe to share across concurrent requests."""
    global _readiness_redis_client
    if _readiness_redis_client is None:
        _readiness_redis_client = redis_from_url(settings.RATE_LIMIT_STORAGE_URI)
    return _readiness_redis_client


async def _check_redis() -> bool:
    if not settings.RATE_LIMIT_STORAGE_URI.startswith("redis"):
        return True
    try:
        return bool(await _get_readiness_redis_client().ping())
    except Exception:
        logger.warning("readiness_redis_check_failed", exc_info=True)
        return False


@router.get("/ready")
async def readiness_check(
    session: Annotated[AsyncSession, Depends(get_db_session_readonly)],
):
    """Dependency readiness probe: DB, scheduler, and Redis (when configured).

    Unlike ``/health`` (process-alive only), this checks the dependencies the
    app actually needs to serve traffic — for use as a Docker/orchestrator
    readiness gate, not a liveness probe.
    """
    checks = {"database": False, "scheduler": False, "redis": False}

    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        logger.warning("readiness_database_check_failed", exc_info=True)

    checks["scheduler"] = scheduler.running
    checks["redis"] = await _check_redis()

    if all(checks.values()):
        return {"status": "ready", "checks": checks}
    # Raising HTTPException here would go through http_exception_handler,
    # which stringifies `detail` for RFC 7807 problem-details responses —
    # returning JSONResponse directly keeps `checks` as real JSON.
    return JSONResponse(
        status_code=HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "not_ready", "checks": checks},
    )
