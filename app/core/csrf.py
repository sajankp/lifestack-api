import secrets

from fastapi import Request, Response

from app.config import settings
from app.core.exceptions import CSRFFailedError

CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def issue_csrf_token(response: Response, *, max_age: int | None) -> str:
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=False,
        max_age=max_age,
        path="/",
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        secure=settings.COOKIE_SECURE,
    )
    return token


def clear_csrf_token(response: Response) -> None:
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value="",
        httponly=False,
        max_age=0,
        expires=0,
        path="/",
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        secure=settings.COOKIE_SECURE,
    )


def validate_cookie_csrf_token(request: Request) -> None:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get(CSRF_HEADER_NAME)

    if not cookie_token or not header_token:
        raise CSRFFailedError(detail="CSRF token cookie and X-CSRF-Token header are required")
    if not secrets.compare_digest(cookie_token, header_token):
        raise CSRFFailedError(detail="CSRF token mismatch")
