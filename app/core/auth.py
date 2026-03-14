from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from jose import JWTError, jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.config import settings

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


def get_user_info_from_token(token: str, expected_type: str = "access") -> tuple[str, str, str]:
    """
    Decodes the token and returns (username, user_id, sid).
    """
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        token_type = payload.get("token_type")
        if token_type != expected_type:
            raise credentials_exception

        username = payload.get("sub")
        user_id = payload.get("sub_id")
        sid = payload.get("sid")

        if None in (username, user_id, sid):
            raise credentials_exception

    except JWTError as e:
        if isinstance(e, jwt.ExpiredSignatureError):
            credentials_exception.detail = "Token has expired"
        raise credentials_exception from e

    return username, user_id, sid
