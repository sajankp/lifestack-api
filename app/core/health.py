import secrets
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
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


async def _check_redis() -> bool:
    if not settings.RATE_LIMIT_STORAGE_URI.startswith("redis"):
        return True
    client = redis_from_url(settings.RATE_LIMIT_STORAGE_URI)
    try:
        return bool(await client.ping())
    except Exception:
        logger.warning("readiness_redis_check_failed", exc_info=True)
        return False
    finally:
        await client.aclose()


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
    raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE, detail={"checks": checks})
