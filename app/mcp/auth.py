"""Authentication and authorization helpers for the MCP transport.

FastMCP's ``OAuthProvider`` is both an OAuth authorization server and a bearer
token verifier.  The implementation below stores the short-lived OAuth state,
clients, codes and refresh tokens in Redis so it works across restarts and
multiple API workers.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import jwt
import redis.asyncio as redis
from fastmcp.server.auth import AccessToken, OAuthProvider
from fastmcp.server.auth.auth import ClientRegistrationOptions, RevocationOptions
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from app.auth.repository import AuthSessionRepository, UserRepository
from app.config import settings
from app.core.auth import create_token
from app.core.database import postgres


class LifestackTokenVerifier(OAuthProvider):
    """Lifestack OAuth authorization server and MCP resource server."""

    _CLIENT_TTL = 60 * 60 * 24 * 90
    _STATE_TTL = 60 * 10
    _CODE_TTL = 60
    _REFRESH_TTL = 60 * 60 * 24 * 30

    def __init__(self):
        base_url = settings.MCP_BASE_URL
        if not base_url:
            raise ValueError("MCP_BASE_URL must be configured when MCP is enabled")
        if not base_url.startswith("https://"):
            raise ValueError("MCP_BASE_URL must use HTTPS")

        super().__init__(
            base_url=base_url.rstrip("/"),
            resource_base_url=base_url.rstrip("/"),
            issuer_url=base_url.rstrip("/"),
            # Authentication is required at transport level; individual tools
            # enforce mcp:read versus mcp:write so read-only grants work.
            required_scopes=[],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["mcp:read", "mcp:write"],
                default_scopes=["mcp:read"],
                require_software_id=False,
                require_software_version=False,
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        self._redis: redis.Redis | None = None

    @property
    def resource_url(self) -> str:
        """Canonical MCP resource URL used for RFC 8707 audience binding."""
        if self._resource_url is not None:
            return str(self._resource_url).rstrip("/")
        return f"{str(self.resource_base_url).rstrip('/')}{settings.MCP_MOUNT_PATH}"

    def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._redis

    @staticmethod
    def _client_key(client_id: str) -> str:
        return f"mcp:oauth:client:{client_id}"

    @staticmethod
    def _state_key(state: str) -> str:
        return f"mcp:oauth:state:{state}"

    @staticmethod
    def _code_key(code: str) -> str:
        return f"mcp:oauth:code:{code}"

    @staticmethod
    def _refresh_key(token: str) -> str:
        return f"mcp:oauth:refresh:{token}"

    @staticmethod
    def _redirect_with_query(uri: str, values: dict[str, str]) -> str:
        parts = urlsplit(uri)
        query = parse_qsl(parts.query, keep_blank_values=True)
        query.extend(values.items())
        return urlunsplit((
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        ))

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = await self._get_redis().get(self._client_key(client_id))
        if raw is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(json.loads(raw))
        except (ValueError, TypeError):
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Persist the SDK-issued client ID and secret exactly as issued."""
        if not client_info.client_id:
            raise ValueError("FastMCP must issue a client_id before persistence")
        await self._get_redis().set(
            self._client_key(client_info.client_id),
            json.dumps(client_info.model_dump(mode="json")),
            ex=self._CLIENT_TTL,
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Start authorization in the signed-in Lifestack web application."""
        expected_resource = self.resource_url
        if params.resource != expected_resource:
            raise AuthorizeError(
                error="invalid_request",
                error_description="resource must identify this MCP server",
            )

        state = secrets.token_urlsafe(32)
        state_payload = {
            "client_id": client.client_id,
            "scopes": params.scopes or [],
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
            "state": params.state,
        }
        await self._get_redis().set(
            self._state_key(state), json.dumps(state_payload), ex=self._STATE_TTL
        )
        return f"{settings.FRONTEND_URL.rstrip('/')}/mcp/authorize?{urlencode({'state': state})}"

    async def get_authorization_request(self, state: str) -> dict[str, Any] | None:
        raw = await self._get_redis().get(self._state_key(state))
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            client = await self.get_client(payload["client_id"])
            if client is None:
                return None
            return {
                "client_name": client.client_name or "MCP client",
                "scopes": payload["scopes"],
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    async def complete_authorization(self, state: str, user_id: int, sid: str) -> str:
        """Consume browser authorization state and issue a one-use code."""
        raw = await self._get_redis().getdel(self._state_key(state))
        if raw is None:
            raise AuthorizeError(error="invalid_request", error_description="Authorization expired")
        payload = json.loads(raw)
        client = await self.get_client(payload["client_id"])
        if client is None:
            raise AuthorizeError(error="unauthorized_client", error_description="Unknown client")

        code = secrets.token_urlsafe(32)
        authorization_code = AuthorizationCode(
            code=code,
            scopes=payload["scopes"],
            expires_at=datetime.now(UTC).timestamp() + self._CODE_TTL,
            client_id=payload["client_id"],
            code_challenge=payload["code_challenge"],
            redirect_uri=payload["redirect_uri"],
            redirect_uri_provided_explicitly=payload["redirect_uri_provided_explicitly"],
            resource=payload["resource"],
            subject=f"{user_id}:{sid}",
        )
        await self._get_redis().set(
            self._code_key(code),
            json.dumps(authorization_code.model_dump(mode="json")),
            ex=self._CODE_TTL,
        )
        query = {"code": code}
        if payload.get("state"):
            query["state"] = payload["state"]
        return self._redirect_with_query(payload["redirect_uri"], query)

    async def deny_authorization(self, state: str) -> str:
        """Consume a pending request and return the standard OAuth denial."""
        raw = await self._get_redis().getdel(self._state_key(state))
        if raw is None:
            raise AuthorizeError(error="invalid_request", error_description="Authorization expired")
        payload = json.loads(raw)
        query = {"error": "access_denied"}
        if payload.get("state"):
            query["state"] = payload["state"]
        return self._redirect_with_query(payload["redirect_uri"], query)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        raw = await self._get_redis().getdel(self._code_key(authorization_code))
        if raw is None:
            return None
        try:
            code = AuthorizationCode.model_validate(json.loads(raw))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        return code if code.client_id == client.client_id else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        user_id, sid = self._subject_parts(authorization_code.subject)
        access_token, expires_in = await self._issue_access_token(
            client.client_id or "", user_id, sid, authorization_code.scopes
        )
        refresh = await self._issue_refresh_token(
            client.client_id or "", authorization_code.scopes, authorization_code.subject
        )
        return OAuthToken(
            access_token=access_token,
            expires_in=expires_in,
            scope=" ".join(authorization_code.scopes),
            refresh_token=refresh.token,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        raw = await self._get_redis().get(self._refresh_key(refresh_token))
        if raw is None:
            return None
        try:
            token = RefreshToken.model_validate(json.loads(raw))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        return token if token.client_id == client.client_id else None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if not set(scopes).issubset(refresh_token.scopes):
            raise TokenError(
                error="invalid_scope", error_description="Scope exceeds original grant"
            )
        await self._get_redis().delete(self._refresh_key(refresh_token.token))
        user_id, sid = self._subject_parts(refresh_token.subject)
        access_token, expires_in = await self._issue_access_token(
            client.client_id or "", user_id, sid, scopes
        )
        replacement = await self._issue_refresh_token(
            client.client_id or "", scopes, refresh_token.subject
        )
        return OAuthToken(
            access_token=access_token,
            expires_in=expires_in,
            scope=" ".join(scopes),
            refresh_token=replacement.token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            claims = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=["HS256"],
                audience=self.resource_url,
                issuer=str(self.issuer_url),
                options={"require": ["exp", "aud", "iss", "sub", "sid"]},
            )
            if claims.get("token_type") != "mcp_access":
                return None
            user_id = int(claims["sub"])
            sid = str(claims["sid"])
            scopes = str(claims.get("scope", "")).split()
        except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
            return None

        async with postgres.async_session_maker() as session:
            auth_session = await AuthSessionRepository(session).get_active_by_sid(sid, user_id)
            user = await UserRepository(session).get_by_id(user_id)
        if auth_session is None or user is None or not user.is_active:
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id", "")),
            subject=str(user_id),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.resource_url,
            claims={"user_id": user_id, "sid": sid, **claims},
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            await self._get_redis().delete(self._refresh_key(token.token))
            return
        claims = token.claims or {}
        sid = claims.get("sid")
        user_id = claims.get("user_id")
        if not sid or user_id is None:
            return
        async with postgres.async_session_maker() as session, session.begin():
            auth_session = await AuthSessionRepository(session).get_active_by_sid(
                str(sid), int(user_id)
            )
            if auth_session is not None:
                auth_session.revoked_at = datetime.now(UTC)
                session.add(auth_session)

    async def _issue_access_token(
        self, client_id: str, user_id: int, sid: str, scopes: list[str]
    ) -> tuple[str, int]:
        expires_in = settings.ACCESS_TOKEN_EXPIRE_SECONDS
        token = create_token(
            data={
                "sub": str(user_id),
                "aud": self.resource_url,
                "iss": str(self.issuer_url),
                "scope": " ".join(scopes),
                "client_id": client_id,
            },
            expires_delta=timedelta(seconds=expires_in),
            sid=sid,
            token_type="mcp_access",
        )
        return token, expires_in

    async def _issue_refresh_token(
        self, client_id: str, scopes: list[str], subject: str | None
    ) -> RefreshToken:
        token = secrets.token_urlsafe(48)
        refresh = RefreshToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(datetime.now(UTC).timestamp()) + self._REFRESH_TTL,
            subject=subject,
        )
        await self._get_redis().set(
            self._refresh_key(token),
            json.dumps(refresh.model_dump(mode="json")),
            ex=self._REFRESH_TTL,
        )
        return refresh

    @staticmethod
    def _subject_parts(subject: str | None) -> tuple[int, str]:
        if not subject or ":" not in subject:
            raise TokenError(
                error="invalid_grant", error_description="Invalid authorization subject"
            )
        user_id, sid = subject.split(":", 1)
        try:
            return int(user_id), sid
        except ValueError as exc:
            raise TokenError(
                error="invalid_grant", error_description="Invalid authorization subject"
            ) from exc
