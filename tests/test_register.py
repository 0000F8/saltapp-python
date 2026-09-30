# register_agent against a fake server with the real shapes: GET
# /api/v1/config -> {terms_version, privacy_version, ...}; POST /auth (root
# agent branch of Users::RegistrationsController#create_agent) -> the
# safe agent fields plus `api_key`; refusals -> {"status": {"message"}}.
from __future__ import annotations

import json

import httpx
import pytest

from saltapp import SaltApiError, register_agent, register_agent_async
from saltapp.client import SaltClient

AGENT = {
    "id": "9a3f1c52-0000-4000-8000-000000000001",
    "username": "stranger_bot",
    "display_name": "Stranger Bot",
    "account_type": "Agent",
    "public_fingerprint": "ABCD",
    "salt_did": "did:web:saltapp.ai:api:agents:stranger_bot",
    "api_key": "raw-api-key-once",
}


def make_handler(seen: list, status: int = 201, body: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/config":
            return httpx.Response(200, json={"terms_version": "2026-09-10", "privacy_version": "2026-09-11"})
        return httpx.Response(status, json=body if body is not None else AGENT)

    return handler


def test_register_agent_keeps_private_key_local_and_returns_api_key_once():
    seen: list = []
    http = httpx.Client(transport=httpx.MockTransport(make_handler(seen)))
    result = register_agent(username="stranger_bot", display_name="Stranger Bot", base_url="https://salt.test/", listed=True, http=http)

    assert [str(r.url) for r in seen] == ["https://salt.test/api/v1/config", "https://salt.test/auth/"]
    body = json.loads(seen[1].content)
    assert body["account_type"] == "Agent" and body["listed"] is True
    assert body["accepted_terms_version"] == "2026-09-10" and body["accepted_privacy_version"] == "2026-09-11"
    assert "BEGIN PGP PUBLIC KEY BLOCK" in body["public_key"]
    assert "webhook" not in body  # socket mode
    assert "PRIVATE KEY" not in json.dumps(body)

    assert result.api_key == "raw-api-key-once"
    assert "api_key" not in result.agent
    assert "BEGIN PGP PRIVATE KEY BLOCK" in result.private_key
    assert result.public_key == body["public_key"]
    assert result.identity.agent_id == AGENT["id"] and result.identity.api_key == "raw-api-key-once"
    agent = result.build_agent("https://salt.test")
    assert agent.identity.agent_id == AGENT["id"]


def test_register_agent_surfaces_refusal_sentence():
    seen: list = []
    refusal = {"status": {"message": "Your account couldn't be created. Display name can't start with salt"}}
    http = httpx.Client(transport=httpx.MockTransport(make_handler(seen, 422, refusal)))
    with pytest.raises(SaltApiError) as info:
        register_agent(username="x_bot", display_name="Salt Helper", base_url="https://salt.test", http=http)
    assert info.value.status == 422
    assert "Display name can't start with salt" in str(info.value)


@pytest.mark.asyncio
async def test_register_agent_async_forwards_webhook():
    seen: list = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(make_handler(seen)))
    result = await register_agent_async(username="stranger_bot", display_name="Stranger Bot", base_url="https://salt.test", webhook="https://me.example/hook", http=http)
    assert json.loads(seen[1].content)["webhook"] == "https://me.example/hook"
    assert result.api_key == "raw-api-key-once"


def test_search_contacts_by_handle():
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[{"id": "u1", "username": "dan"}])

    client = SaltClient("https://salt.test", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.search_contacts("k", username="dan")[0]["id"] == "u1"
    assert str(seen[0].url) == "https://salt.test/api/v1/search/contacts?username=dan"
    assert seen[0].headers["api-key"] == "k"
