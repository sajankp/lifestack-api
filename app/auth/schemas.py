import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# Compiled pattern for password complexity check
_UPPERCASE_RE = re.compile(r"[A-Z]")
_LOWERCASE_RE = re.compile(r"[a-z]")
_DIGIT_RE = re.compile(r"\d")
_SPECIAL_RE = re.compile(r"[!@#$%^&*()\-_=+\[\]{}|;:,.<>?/~`]")


def _validate_password_complexity(v: str) -> str:
    """Enforce password complexity: upper, lower, digit, and special character."""
    errors = []
    if not _UPPERCASE_RE.search(v):
        errors.append("at least one uppercase letter")
    if not _LOWERCASE_RE.search(v):
        errors.append("at least one lowercase letter")
    if not _DIGIT_RE.search(v):
        errors.append("at least one digit")
    if not _SPECIAL_RE.search(v):
        errors.append("at least one special character (!@#$%^&*()...)")
    if errors:
        raise ValueError(f"Password must contain {', '.join(errors)}.")
    return v


class UserCreate(BaseModel):
    email: EmailStr
    username: str = Field(
        ...,
        min_length=3,
        max_length=50,
        pattern=r"^[a-zA-Z0-9_\-]+$",
        description="Username must be 3-50 chars, letters, numbers, underscores, or hyphens.",
    )
    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description=(
            "Password must be 8-128 chars and contain uppercase, lowercase, "
            "digit, and special character."
        ),
    )

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        return _validate_password_complexity(v)


class UserResponse(BaseModel):
    public_id: uuid.UUID
    email: EmailStr
    username: str
    is_active: bool
    timezone: str | None = None

    model_config = ConfigDict(from_attributes=True)


class UserTimezoneUpdate(BaseModel):
    timezone: str | None = Field(..., min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {value}") from exc
        return value


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description=(
            "New password must be 8-128 chars and contain uppercase, lowercase, "
            "digit, and special character."
        ),
    )

    @field_validator("new_password")
    @classmethod
    def new_password_complexity(cls, v: str) -> str:
        return _validate_password_complexity(v)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description=(
            "New password must be 8-128 chars and contain uppercase, lowercase, "
            "digit, and special character."
        ),
    )

    @field_validator("new_password")
    @classmethod
    def new_password_complexity(cls, v: str) -> str:
        return _validate_password_complexity(v)
