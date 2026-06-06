import uuid
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.config import settings
from app.core.exceptions import UnauthorizedError

# In Lifestack, we only use Argon2id (no legacy bcrypt migration needed)
pwd_hash = PasswordHash([Argon2Hasher()])


def hash_password(password: str) -> str:
    """Hash a password using Argon2id."""
    return pwd_hash.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> tuple[bool, str | None]:
    """Verify password. Returns (is_valid, new_hash)."""
    return pwd_hash.verify_and_update(plain_password, hashed_password)


def create_token(
    data: dict,
    expires_delta: timedelta | None = None,
    sid: str | None = None,
    token_type: str | None = None,
) -> str:
    to_encode = data.copy()
    to_encode.update({"jti": str(uuid.uuid4())})
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(minutes=15)

    if sid:
        to_encode.update({"sid": sid})
    if token_type:
        to_encode.update({"token_type": token_type})

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm="HS256")
    return encoded_jwt


def get_user_info_from_token(
    token: str, expected_type: str = "access"
) -> tuple[str, str, str, int | None]:
    """
    Decodes the token and returns (username, user_id, sid, default_workspace_id).
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        token_type = payload.get("token_type")
        if token_type != expected_type:
            raise UnauthorizedError(detail="Could not validate credentials")

        username = payload.get("sub")
        user_id = payload.get("sub_id")
        sid = payload.get("sid")
        default_workspace_id = payload.get("default_workspace_id")

        if None in (username, user_id, sid):
            raise UnauthorizedError(detail="Could not validate credentials")

    except JWTError as e:
        detail = (
            "Token has expired"
            if isinstance(e, jwt.ExpiredSignatureError)
            else "Could not validate credentials"
        )
        raise UnauthorizedError(detail=detail) from e

    try:
        parsed_workspace_id = (
            int(default_workspace_id) if default_workspace_id is not None else None
        )
    except (ValueError, TypeError):
        parsed_workspace_id = None

    return username, user_id, sid, parsed_workspace_id
