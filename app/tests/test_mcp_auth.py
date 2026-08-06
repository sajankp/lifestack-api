from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from app.config import settings
from app.mcp.auth import LifestackTokenVerifier


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def getdel(self, key: str):
        return self.values.pop(key, None)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.values[key] = value
        return True

    async def delete(self, key: str):
        return int(self.values.pop(key, None) is not None)


@pytest.mark.anyio
async def test_mcp_oauth_client_and_authorization_code_are_durable_shapes(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.example.test")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.test")
    provider = LifestackTokenVerifier()
    provider._redis = FakeRedis()
    provider.get_routes("/mcp")
    client = OAuthClientInformationFull(
        client_id="client-1",
        redirect_uris=["http://127.0.0.1:43123/callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="mcp:read",
    )

    await provider.register_client(client)
    assert (await provider.get_client("client-1")).client_id == "client-1"

    authorization_url = await provider.authorize(
        client,
        AuthorizationParams(
            state="client-state",
            scopes=["mcp:read"],
            code_challenge="A" * 43,
            redirect_uri="http://127.0.0.1:43123/callback",
            redirect_uri_provided_explicitly=True,
            resource=provider.resource_url,
        ),
    )
    state = parse_qs(urlparse(authorization_url).query)["state"][0]
    assert await provider.get_authorization_request(state) == {
        "client_name": "MCP client",
        "scopes": ["mcp:read"],
    }

    callback_url = await provider.complete_authorization(state, 42, "session-1")
    code = parse_qs(urlparse(callback_url).query)["code"][0]
    authorization_code = await provider.load_authorization_code(client, code)
    assert authorization_code is not None
    assert authorization_code.subject == "42:session-1"
    assert await provider.load_authorization_code(client, code) is None


@pytest.mark.anyio
async def test_mcp_oauth_rejects_wrong_resource(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.example.test")
    provider = LifestackTokenVerifier()
    provider._redis = FakeRedis()
    provider.get_routes("/mcp")
    client = OAuthClientInformationFull(
        client_id="client-1", redirect_uris=["http://127.0.0.1:43123/callback"]
    )

    with pytest.raises(Exception) as exc_info:
        await provider.authorize(
            client,
            AuthorizationParams(
                state=None,
                scopes=["mcp:read"],
                code_challenge="A" * 43,
                redirect_uri="http://127.0.0.1:43123/callback",
                redirect_uri_provided_explicitly=True,
                resource="https://other.example.test/mcp",
            ),
        )
    assert exc_info.value.error_description == "resource must identify this MCP server"
