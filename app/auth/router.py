import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app.auth.schemas import TokenResponse, UserCreate
from app.auth.service import AuthService
from app.config import settings
from app.core.auth import create_token, get_user_info_from_token
from app.core.dependencies import get_auth_service, limiter

router = APIRouter()


@router.post("/register", response_model=bool)
@limiter.limit(settings.rate_limit_auth if hasattr(settings, "rate_limit_auth") else "10/minute")
async def create_user(
    request: Request,
    user_in: UserCreate,
    auth_service: AuthService = Depends(get_auth_service),
):
    await auth_service.register_user(user_in)
    return True


@router.post("/login", response_model=TokenResponse)
@limiter.limit(settings.rate_limit_auth if hasattr(settings, "rate_limit_auth") else "10/minute")
async def login_for_access_token(
    request: Request,
    response: Response,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    auth_service: AuthService = Depends(get_auth_service),
    remember_me: bool = True,
):
    user = await auth_service.authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Generate a new Session ID (sid) for this login session
    sid = str(uuid.uuid4())

    access_token_expires = timedelta(seconds=settings.ACCESS_TOKEN_EXPIRE_SECONDS)
    access_token = create_token(
        data={"sub": user.username, "sub_id": str(user.id)},
        expires_delta=access_token_expires,
        sid=sid,
        token_type="access",
    )
    refresh_token_expires = timedelta(seconds=settings.REFRESH_TOKEN_EXPIRE_SECONDS)
    refresh_token = create_token(
        data={"sub": user.username, "sub_id": str(user.id)},
        expires_delta=refresh_token_expires,
        sid=sid,
        token_type="refresh",
    )

    # Set HttpOnly cookies
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_SECONDS,
        samesite="lax",
        secure=False,  # Set True in production
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        max_age=settings.REFRESH_TOKEN_EXPIRE_SECONDS if remember_me else None,
        samesite="lax",
        secure=False,
    )

    # Return empty tokens in body, as they are now in HttpOnly cookies
    return TokenResponse(access_token="", token_type="bearer")


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit(settings.rate_limit_auth if hasattr(settings, "rate_limit_auth") else "10/minute")
async def refresh_token(
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
):
    refresh_token = request.cookies.get("refresh_token")

    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token missing"
        )

    try:
        username, user_id, sid = get_user_info_from_token(refresh_token, expected_type="refresh")
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        ) from None

    # We could also verify the user still exists here

    access_token_expires = timedelta(seconds=settings.ACCESS_TOKEN_EXPIRE_SECONDS)
    access_token = create_token(
        data={"sub": username, "sub_id": user_id},
        expires_delta=access_token_expires,
        sid=sid,
        token_type="access",
    )

    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_SECONDS,
        samesite="lax",
        secure=False,
    )

    return TokenResponse(access_token="", token_type="bearer")


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Logout by clearing auth cookies."""
    for key in ("access_token", "refresh_token"):
        response.set_cookie(
            key=key,
            value="",
            httponly=True,
            max_age=0,
            expires=0,
            samesite="lax",
            secure=False,
        )
    return {"message": "Logged out successfully"}
