# Mandates R2: SaltClient/AsyncSaltClient.act_for (header injection, auto
# idempotency, the {"asked": True, ...} 202 shape), the mandate management
# methods, and the six new Agent dispatch events (mandate_offered/activated/
# paused/revoked, approval_requested/decided). Mirrors salt-agent-sdk's
# tests/act-for.test.js and tests/mandate-events.test.js.
from __future__ import annotations

import json

import httpx
import pytest

from saltapp.agent import (
    ApprovalDecidedContext,
    ApprovalRequestedContext,
    MandateLifecycleContext,
    MandateOfferedContext,
    Agent,
)
from saltapp.client import ActingSaltClient, AsyncActingSaltClient, AsyncSaltClient, SaltClient, is_asked
from saltapp.webhook import Event, classify

HOST = "https://example.saltapp.test"


def make_sync_client(handler) -> SaltClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return SaltClient(HOST, http=http)


def make_async_client(handler) -> AsyncSaltClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return AsyncSaltClient(HOST, http=http)


def json_response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body)


def asked_response(exercise_id="ex-1", expires_at="2026-09-24T00:00:00Z") -> httpx.Response:
    return json_response(202, {"status": "asked", "exercise_id": exercise_id, "expires_at": expires_at})


# --- client.act_for: headers -------------------------------------------------


def test_act_for_sends_x_salt_act_for_and_x_salt_mandate():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers))
        return json_response(200, {"id": "chat-1"})

    client = make_sync_client(handler)

    unpinned = client.act_for("principal-1")
    assert isinstance(unpinned, ActingSaltClient)
    unpinned.create_or_get_chat("agent-key", "contact-1")
    assert captured[0]["x-salt-act-for"] == "principal-1"
    assert "x-salt-mandate" not in captured[0]
    assert captured[0]["api-key"] == "agent-key"

    pinned = client.act_for("principal-1", "mandate-9")
    pinned.create_or_get_chat("agent-key", "contact-1")
    assert captured[1]["x-salt-act-for"] == "principal-1"
    assert captured[1]["x-salt-mandate"] == "mandate-9"


def test_base_client_never_sends_acting_headers():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers))
        return json_response(200, {"id": "chat-1"})

    client = make_sync_client(handler)
    client.create_or_get_chat("agent-key", "contact-1")
    assert "x-salt-act-for" not in captured[0]
    assert "x-salt-mandate" not in captured[0]


def test_act_for_auto_idempotency_key_on_post_never_on_get():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, dict(request.headers)))
        return json_response(200, {"ok": True})

    client = make_sync_client(handler)
    acting = client.act_for("principal-1")

    acting.post_message("agent-key", "chat-1", "hi")
    post_method, post_headers = next(c for c in captured if c[0] == "POST")
    assert "idempotency-key" in post_headers and post_headers["idempotency-key"]

    acting.get_chat_members("agent-key", "chat-1")
    get_method, get_headers = next(c for c in captured if c[0] == "GET")
    assert "idempotency-key" not in get_headers


def test_act_for_honours_explicit_idempotency_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return json_response(200, {})

    client = make_sync_client(handler)
    acting = client.act_for("principal-1")
    acting.create_invoice(
        "agent-key",
        chat_id="c1",
        receiver_id="u2",
        wallet_id="w1",
        amount="5.00",
        line_items=[{"name": "thing", "qty": 1, "unit_price": "5.00", "subtotal": "5.00"}],
        idempotency_key="my-own-key",
    )
    assert captured["headers"]["idempotency-key"] == "my-own-key"


# --- client.act_for / prepare_transfer: the 202 "asked" shape ---------------


def test_asked_202_resolves_typed_never_raises():
    client = make_sync_client(lambda request: asked_response())
    acting = client.act_for("principal-1", "mandate-1")

    result = acting.post_message("agent-key", "chat-1", "please send $5")
    assert is_asked(result) is True
    assert result == {"asked": True, "exercise_id": "ex-1", "expires_at": "2026-09-24T00:00:00Z"}


def test_prepare_transfer_always_resolves_asked():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return asked_response(exercise_id="ex-2", expires_at="2026-09-24T01:00:00Z")

    client = make_sync_client(handler)
    acting = client.act_for("principal-1")
    result = acting.prepare_transfer("agent-key", wallet_id="w-1", receiver_id="u-2", amount="5.00")

    assert captured["url"] == f"{HOST}/api/v1/transfers/prepare"
    assert is_asked(result) is True
    assert result["exercise_id"] == "ex-2"


def test_is_asked_narrows_correctly():
    assert is_asked({"asked": True, "exercise_id": "x"}) is True
    assert is_asked({"id": "chat-1"}) is False
    assert is_asked(None) is False
    assert is_asked("asked") is False


def test_ordinary_non_202_response_through_act_for_is_unaffected():
    client = make_sync_client(lambda request: json_response(200, {"id": "chat-1", "session": {"users": []}}))
    acting = client.act_for("principal-1")
    chat = acting.create_or_get_chat("agent-key", "contact-1")
    assert is_asked(chat) is False
    assert chat["id"] == "chat-1"


@pytest.mark.asyncio
async def test_async_act_for_headers_and_asked_shape():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers))
        return asked_response()

    client = make_async_client(handler)
    acting = client.act_for("principal-1", "mandate-1")
    assert isinstance(acting, AsyncActingSaltClient)

    result = await acting.post_message("agent-key", "chat-1", "hi")
    assert captured[0]["x-salt-act-for"] == "principal-1"
    assert captured[0]["x-salt-mandate"] == "mandate-1"
    assert captured[0]["idempotency-key"]
    assert is_asked(result) is True


# --- client mandate management: always as yourself --------------------------


def test_mandate_management_methods_hit_the_right_endpoints():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), dict(request.headers), request.content))
        if "/accept" in str(request.url):
            return json_response(200, {"id": "mandate-1", "status": "active"})
        if "/decide" in str(request.url):
            return json_response(200, {"id": "ex-1", "decision": "approved"})
        if "exercises/open" in str(request.url):
            return json_response(200, {"exercises": []})
        if "role=grantor" in str(request.url):
            return json_response(200, {"mandates": []})
        return json_response(200, {"mandates": []})

    client = make_sync_client(handler)

    client.list_mandates("owner-key", role="grantor")
    assert "role=grantor" in calls[0][1]

    client.accept_mandate("agent-key", "mandate-1")
    accept_call = next(c for c in calls if "/accept" in c[1])
    assert accept_call[0] == "POST"
    assert "x-salt-act-for" not in accept_call[2]

    client.decide_mandate_exercise("owner-key", "ex-1", "approve", "looks right")
    decide_call = next(c for c in calls if "/decide" in c[1])
    assert json.loads(decide_call[3]) == {"decision": "approve", "note": "looks right"}

    client.get_open_mandate_exercises("owner-key")
    assert any("exercises/open" in c[1] for c in calls)


# --- webhook.classify: the six new event types ------------------------------


@pytest.mark.parametrize(
    "event_type",
    ["mandate_offered", "mandate_activated", "mandate_paused", "mandate_revoked", "approval_requested", "approval_decided"],
)
def test_classify_recognizes_mandate_event_types(event_type):
    assert classify({"type": event_type, "mandate": {"id": "m1"}}) == event_type


def test_classify_still_falls_back_to_unknown_for_junk():
    assert classify({"type": "something_new_and_unrecognized"}) == "unknown"


# --- Agent dispatch: mandate_offered / activated / paused / revoked ---------


def make_agent(handler=None, *, agent_id: str = "self-agent") -> Agent:
    def default_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "mandate-1", "status": "active"})

    transport = httpx.MockTransport(handler or default_handler)
    client = AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=transport))
    return Agent(host=HOST, api_key="agent-key", public_key="pub", private_key="priv", agent_id=agent_id, client=client)


def base_mandate(**overrides):
    mandate = {
        "id": "mandate-1",
        "kind": "explicit",
        "status": "proposed",
        "label": "Reply to messages",
        "grantor": {"id": "owner-1", "username": "dan"},
        "grantee": {"id": "self-agent", "username": "helper"},
        "capabilities": [{"id": "cap-1", "capability": "chat.send", "selector": {}, "mode": "auto", "constraints": {}}],
        "starts_at": "2026-09-23T00:00:00Z",
        "standing": True,
        "version": 1,
    }
    mandate.update(overrides)
    return mandate


def base_exercise(**overrides):
    exercise = {
        "id": 42,
        "mandate_id": "mandate-1",
        "capability": "money.pay",
        "action": "transfers#prepare",
        "decision": "asked",
        "summary": {"amount": "5.00"},
        "actor": {"id": "agent-1", "username": "helper", "account_type": "Agent"},
        "principal": {"id": "self-agent", "username": "owner-bot"},
        "created_at": "2026-09-23T00:00:00Z",
    }
    exercise.update(overrides)
    return exercise


@pytest.mark.asyncio
async def test_mandate_offered_reaches_handler_and_accept_calls_client():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={**base_mandate(), "status": "active"})

    agent = make_agent(handler)
    seen = []

    @agent.on_mandate_offered
    async def on_offered(ctx: MandateOfferedContext):
        seen.append(ctx.mandate["id"])
        accepted = await ctx.accept()
        assert accepted["status"] == "active"

    await agent.dispatch(Event(type="mandate_offered", body={"mandate": base_mandate()}))
    assert seen == ["mandate-1"]
    assert captured["url"].endswith("/api/v1/mandates/mandate-1/accept")
    assert captured["headers"]["api-key"] == "agent-key"


@pytest.mark.asyncio
async def test_mandate_lifecycle_events_reach_their_own_handlers():
    agent = make_agent()
    seen = []

    @agent.on_mandate_activated
    async def on_activated(ctx: MandateLifecycleContext):
        seen.append(("activated", ctx.mandate["status"]))

    @agent.on_mandate_paused
    async def on_paused(ctx: MandateLifecycleContext):
        seen.append(("paused", ctx.mandate.get("pause_reason")))

    @agent.on_mandate_revoked
    async def on_revoked(ctx: MandateLifecycleContext):
        seen.append(("revoked", ctx.mandate.get("revoke_reason")))

    await agent.dispatch(Event(type="mandate_activated", body={"mandate": base_mandate(status="active")}))
    await agent.dispatch(Event(type="mandate_paused", body={"mandate": base_mandate(status="paused", pause_reason="key_changed")}))
    await agent.dispatch(Event(type="mandate_revoked", body={"mandate": base_mandate(status="revoked", revoke_reason="no longer needed")}))

    assert seen == [
        ("activated", "active"),
        ("paused", "key_changed"),
        ("revoked", "no longer needed"),
    ]


@pytest.mark.asyncio
async def test_approval_requested_reaches_handler_and_decide_calls_client():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={**base_exercise(), "decision": "approved"})

    agent = make_agent(handler)
    seen = []

    @agent.on_approval_requested
    async def on_requested(ctx: ApprovalRequestedContext):
        seen.append(ctx.exercise["id"])
        decided = await ctx.decide("approve", "auto-approved by policy")
        assert decided["decision"] == "approved"

    await agent.dispatch(Event(type="approval_requested", body={"exercise": base_exercise()}))
    assert seen == [42]
    assert captured["url"].endswith("/api/v1/mandates/exercises/42/decide")
    assert captured["body"] == {"decision": "approve", "note": "auto-approved by policy"}


@pytest.mark.asyncio
async def test_approval_decided_reaches_handler_informational_only():
    agent = make_agent()
    seen = []

    @agent.on_approval_decided
    async def on_decided(ctx: ApprovalDecidedContext):
        seen.append(ctx.exercise["decision"])
        assert not hasattr(ctx, "decide")

    await agent.dispatch(Event(type="approval_decided", body={"exercise": base_exercise(decision="approved")}))
    assert seen == ["approved"]


@pytest.mark.asyncio
async def test_mandate_event_with_no_registered_handler_is_a_silent_noop():
    agent = make_agent()
    # No @agent.on_mandate_offered registered at all -- dispatch must not raise.
    await agent.dispatch(Event(type="mandate_offered", body={"mandate": base_mandate()}))


@pytest.mark.asyncio
async def test_mandate_events_are_deduped_by_delivery_id_like_every_other_event():
    agent = make_agent()
    seen = []

    @agent.on_mandate_activated
    async def on_activated(ctx: MandateLifecycleContext):
        seen.append(ctx.mandate["id"])

    event = Event(type="mandate_activated", body={"mandate": base_mandate(status="active")}, delivery_id="dup-1")
    await agent.dispatch(event)
    await agent.dispatch(event)
    assert seen == ["mandate-1"]
