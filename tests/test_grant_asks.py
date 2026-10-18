# Ask for what's required (0.11.0): refusal envelope on SaltApiError, request_grant / check_grant,
# on_grant_decided, X-Salt-Client. Mirrors salt-agent-sdk tests/grant-asks.test.js.
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

import saltapp
from saltapp.agent import Agent
from saltapp.client import AsyncSaltClient, SaltClient
from saltapp.errors import SaltApiError
from saltapp.webhook import Event

HOST = "https://example.saltapp.test"

ENVELOPE = {
    "error": "You need permission to post on your profile.",
    "code": "missing_capability",
    "grant": {"capability": "profile.post", "selector": {}, "mode": "auto"},
    "ask": {
        "method": "POST",
        "path": "/api/v1/mandates",
        "body": {
            "grantor_id": "owner-1",
            "capabilities": [{"capability": "profile.post", "selector": {}, "mode": "auto"}],
            "label": "Post on my profile",
            "reason": None,
            "expires_at": None,
        },
    },
}


def sync_client(handler) -> SaltClient:
    return SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(handler)))


def async_client(handler) -> AsyncSaltClient:
    return AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_every_request_carries_x_salt_client():
    seen = []

    def handler(request):
        seen.append(request.headers)
        return httpx.Response(200, json={"mandates": []})

    sync_client(handler).list_mandates("k")
    assert seen[0]["x-salt-client"] == f"saltapp-python/{saltapp.__version__}"
    assert saltapp.__version__ == "0.11.1"


def test_refusal_envelope_is_typed_and_absent_when_not_sent():
    bodies = iter([ENVELOPE, {"error": "no", "code": "forbidden"}])
    client = sync_client(lambda request: httpx.Response(403, json=next(bodies)))
    with pytest.raises(SaltApiError) as ei:
        client.list_mandates("k")
    assert ei.value.code == "missing_capability"
    assert ei.value.grant["capability"] == "profile.post"
    assert ei.value.ask["path"] == "/api/v1/mandates"
    assert ei.value.ask["body"]["grantor_id"] == "owner-1"
    with pytest.raises(SaltApiError) as ei2:
        client.list_mandates("k")
    assert ei2.value.grant is None and ei2.value.ask is None


def test_request_grant_and_check_grant_sync():
    calls = []

    def handler(request):
        calls.append(request)
        if "/check" in request.url.path:
            return httpx.Response(200, json={"allowed": True, "mandate_id": "m1", "mode": "auto", "constraints": {}})
        return httpx.Response(201, json={"id": "m1"})

    client = sync_client(handler)
    client.request_grant(
        "k",
        grantor_id="owner-1",
        capabilities=ENVELOPE["ask"]["body"]["capabilities"],
        reason="weekly update",
        duration_days=7,
        estimated_cost={"amount": 20, "currency": "USD", "period": "month"},
        resume_token="rt-1",
        chat_id="chat-1",
    )
    req = calls[0]
    body = json.loads(req.content)
    assert req.method == "POST" and req.url.path == "/api/v1/mandates"
    assert body["grantor_id"] == "owner-1" and body["reason"] == "weekly update"
    assert body["estimated_cost"] == {"amount": 20, "currency": "USD", "period": "month"}
    assert body["resume_token"] == "rt-1" and body["chat_id"] == "chat-1"
    exp = datetime.strptime(body["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert 6.99 < (exp - datetime.now(timezone.utc)).total_seconds() / 86400 < 7.01

    check = client.check_grant("k", "wake.repeat")
    assert calls[1].method == "GET" and str(calls[1].url).endswith("/api/v1/mandates/check?capability=wake.repeat")
    assert check["allowed"] is True


async def test_request_grant_and_check_grant_async():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"allowed": False, "id": "m1"})

    client = async_client(handler)
    await client.request_grant("k", grantor_id="o", capabilities=[{"capability": "room.create", "mode": "auto"}])
    assert json.loads(calls[0].content) == {"grantor_id": "o", "capabilities": [{"capability": "room.create", "mode": "auto"}]}
    assert (await client.check_grant("k", "room.create"))["allowed"] is False


def make_agent() -> Agent:
    client = async_client(lambda request: httpx.Response(200, json={}))
    return Agent(host=HOST, api_key="agent-key", public_key="pub", private_key="priv", agent_id="self-agent", client=client)


async def test_on_grant_decided_fires_on_activation_and_decision():
    agent = make_agent()
    seen = []

    @agent.on_grant_decided
    async def decided(ctx):
        seen.append(ctx)

    mandate = {
        "id": "m1",
        "status": "active",
        "grantor": {"id": "owner-1"},
        "grantee": {"id": "self-agent"},
        "capabilities": [{"id": "c1", "capability": "profile.post", "selector": {}, "mode": "auto", "constraints": {}}],
    }
    await agent.dispatch(Event(type="mandate_activated", body={"mandate": mandate, "reason": "weekly update", "resume_token": "rt-1"}))
    assert len(seen) == 1
    assert seen[0].approved is True and seen[0].resume_token == "rt-1"
    assert seen[0].capabilities == ["profile.post"] and seen[0].reason == "weekly update"

    exercise = {"id": 5, "mandate_id": "m1", "capability": "agent.spending", "decision": "denied", "actor": {"id": "self-agent"}}
    await agent.dispatch(Event(type="approval_decided", body={"exercise": exercise, "resume_token": "rt-2"}))
    assert len(seen) == 2
    assert seen[1].approved is False and seen[1].resume_token == "rt-2"
    assert seen[1].capabilities == ["agent.spending"]

    # The owner's side of the same activation is not a decision on this agent's own ask.
    await agent.dispatch(Event(type="mandate_activated", body={"mandate": {**mandate, "grantee": {"id": "owner-1"}}}))
    assert len(seen) == 2
