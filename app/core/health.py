import secrets

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.status import HTTP_401_UNAUTHORIZED

from app.config import settings

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
