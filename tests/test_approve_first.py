# Approve-first (Salt 0.240.0): request_ask, on_approval_decided's typed ask, X-Salt-Plan, needs_ask,
# request_mode_change, replaces / plan_id, and the approve_first / plan_required errors.
# Mirrors salt-agent-sdk tests/approve-first.test.js.
from __future__ import annotations

import json

import httpx
import pytest

from saltapp import ApproveFirstError, PlanRequiredError, SaltApiError, crypto
from saltapp.agent import Agent, ApprovalAsk, ApprovalDecidedContext
from saltapp.client import AsyncSaltClient, SaltClient, is_asked, media_digest
from saltapp.webhook import Event

HOST = "https://example.saltapp.test"
ASKED = {"status": "asked", "exercise_id": "ex-1", "expires_at": "2026-10-21T00:00:00Z", "approved": 1, "needed": 3}


def sync_client(handler) -> SaltClient:
    return SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(handler)))


def async_client(handler) -> AsyncSaltClient:
    return AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def recorder(response):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(request, len(calls) - 1) if callable(response) else response

    return calls, handler


def test_request_ask_posts_words_with_a_generated_idempotency_key():
    calls, handler = recorder(httpx.Response(202, json=ASKED))
    client = sync_client(handler)
    out = client.request_ask("k", "m1", capability="profile.post", words="Hello world", where="my profile")
    assert out == {"asked": True, "exercise_id": "ex-1", "expires_at": "2026-10-21T00:00:00Z", "approved": 1, "needed": 3}
    assert is_asked(out)
    req = calls[0]
    assert req.method == "POST" and req.url.path == "/api/v1/mandates/m1/asks"
    assert json.loads(req.content) == {"capability": "profile.post", "where": "my profile", "words": "Hello world"}
    assert len(req.headers["idempotency-key"]) == 36
    client.request_ask("k", "m1", capability="room.create", words="A room", where="public", idempotency_key="mine-1")
    assert calls[1].headers["idempotency-key"] == "mine-1"


def test_a_plain_asked_202_keeps_its_old_shape():
    _, handler = recorder(httpx.Response(202, json={"status": "asked", "exercise_id": "e", "expires_at": "x"}))
    assert sync_client(handler).request_ask("k", "m1", capability="profile.post", words="a", where="w") == {
        "asked": True, "exercise_id": "e", "expires_at": "x",
    }


def test_request_ask_outreach_armor_encrypt_to_and_client_side_refusals(keypair_a, keypair_b):
    calls, handler = recorder(httpx.Response(202, json=ASKED))
    client = sync_client(handler)
    client.request_ask("k", "m1", capability="outreach.request", words_armored="-----BEGIN PGP MESSAGE-----x", where="Sam", recipient_id="u7")
    assert json.loads(calls[0].content) == {"capability": "outreach.request", "where": "Sam", "recipient_id": "u7", "words_armored": "-----BEGIN PGP MESSAGE-----x"}

    client.request_ask("k", "m1", capability="outreach.request", words="Hi Sam, a quick hello", encrypt_to=keypair_a.public_key, own_public_key=keypair_b.public_key, where="Sam", chat_id="room-3")
    sent = json.loads(calls[1].content)
    assert "words" not in sent, "the plain words never leave"
    assert sent["words_armored"].startswith("-----BEGIN PGP MESSAGE-----")
    assert crypto.decrypt(sent["words_armored"], keypair_a.private_key, "passphrase-a") == "Hi Sam, a quick hello"
    assert crypto.decrypt(sent["words_armored"], keypair_b.private_key, "passphrase-b") == "Hi Sam, a quick hello", "sealed to the agent too"
    with pytest.raises(ValueError, match="own_public_key"):
        client.request_ask("k", "m1", capability="outreach.request", words="x", encrypt_to=keypair_a.public_key, where="Sam", chat_id="c")

    with pytest.raises(ValueError, match="outreach.request only"):
        client.request_ask("k", "m1", capability="profile.post", words="x", encrypt_to=keypair_a.public_key, where="w")
    with pytest.raises(ValueError, match="not both"):
        client.request_ask("k", "m1", capability="outreach.request", words="x", words_armored="y", where="w", recipient_id="u")
    with pytest.raises(ValueError, match="exact words"):
        client.request_ask("k", "m1", capability="profile.post", where="w")
    assert json.loads(calls[1].content)["chat_id"] == "room-3"
    with pytest.raises(ValueError, match="recipient_id .* or chat_id"):
        client.request_ask("k", "m1", capability="outreach.request", words="x", where="w")
    with pytest.raises(ValueError, match="not both"):
        client.request_ask("k", "m1", capability="outreach.request", words="x", where="w", recipient_id="u", chat_id="c")
    with pytest.raises(ValueError, match="outreach.request only"):
        client.request_ask("k", "m1", capability="profile.post", words="x", where="w", chat_id="c")
    assert len(calls) == 2, "nothing invalid reaches the network"


def test_request_ask_errors_keep_the_code_and_retry_after():
    def respond(request, i):
        if i == 0:
            return httpx.Response(409, json={"error": "Different words for that key.", "code": "idempotency_mismatch"})
        return httpx.Response(429, json={"error": "Waiting on earlier approvals."}, headers={"Retry-After": "60"})

    _, handler = recorder(respond)
    client = sync_client(handler)
    with pytest.raises(SaltApiError) as e1:
        client.request_ask("k", "m1", capability="profile.post", words="a", where="w")
    assert e1.value.status == 409 and e1.value.code == "idempotency_mismatch"
    with pytest.raises(SaltApiError) as e2:
        client.request_ask("k", "m1", capability="profile.post", words="a", where="w")
    assert e2.value.status == 429 and e2.value.retry_after == 60


async def test_async_request_ask_and_mode_change_send_the_same_bodies():
    calls, handler = recorder(lambda request, i: httpx.Response(202, json=ASKED) if i == 0 else httpx.Response(201, json={"id": "m1"}))
    client = async_client(handler)
    out = await client.request_ask("k", "m1", capability="profile.post", words="Hello", where="my profile")
    assert out["exercise_id"] == "ex-1" and out["needed"] == 3
    assert len(calls[0].headers["idempotency-key"]) == 36
    await client.request_mode_change("k", "m1", capability="profile.post", reason="You approved 3")
    assert calls[1].url.path == "/api/v1/mandates/m1/tune_requests"
    assert json.loads(calls[1].content) == {"mode_change": {"capability": "profile.post", "mode": "notify"}, "reason": "You approved 3"}


def test_request_mode_change_sync_body():
    calls, handler = recorder(httpx.Response(201, json={"id": "m1", "tune_request": {"changes": {"mode_change": {"capability": "room.create", "mode": "notify"}}}}))
    out = sync_client(handler).request_mode_change("k", "m1", capability="room.create", reason="3 approved")
    assert calls[0].method == "POST" and calls[0].url.path == "/api/v1/mandates/m1/tune_requests"
    assert json.loads(calls[0].content) == {"mode_change": {"capability": "room.create", "mode": "notify"}, "reason": "3 approved"}
    assert out["tune_request"]["changes"]["mode_change"]["capability"] == "room.create"


def test_plan_sends_x_salt_plan_on_message_and_chat_helpers_and_only_when_given():
    calls, handler = recorder(httpx.Response(200, json={"id": "c1"}))
    client = sync_client(handler)
    client.post_message("k", "c1", "cipher", "sender", plan="plan-9")
    client.post_plain_message("k", "c1", "plain", plan="plan-9")
    client.create_or_get_chat("k", "u1", plan="plan-9")
    assert [c.headers.get("x-salt-plan") for c in calls] == ["plan-9"] * 3
    client.post_message("k", "c1", "cipher", "sender")
    client.create_or_get_chat("k", "u1")
    assert all("x-salt-plan" not in c.headers for c in calls[3:])


async def test_async_plan_header_and_send_message_passes_it_through(keypair_a):
    calls, handler = recorder(
        lambda request, i: httpx.Response(200, json=[{"id": "me", "public_key": keypair_a.public_key}, {"id": "p", "public_key": keypair_a.public_key}])
        if request.method == "GET" else httpx.Response(200, json={"id": "msg"})
    )
    client = async_client(handler)
    await client.post_plain_message("k", "c1", "plain", plan="plan-9")
    await client.create_or_get_chat("k", "u1", plan="plan-9")
    assert [c.headers.get("x-salt-plan") for c in calls] == ["plan-9", "plan-9"]


def test_approve_first_and_plan_required_are_typed_errors_with_the_servers_sentence():
    def respond(request, i):
        if i == 0:
            return httpx.Response(403, json={"error": "Each public post waits for the person's OK first.", "code": "approve_first", "capability": "profile.post", "approved": 1, "needed": 3})
        if i == 1:
            return httpx.Response(403, json={"error": "Name the plan this post belongs to.", "code": "plan_required"})
        if i == 2:
            return httpx.Response(422, json={"error": "Each public post waits for the person's OK (mode ask) until they have approved 3 of them.", "code": "approve_first", "capability": "profile.post", "approved": 0, "needed": 3})
        return httpx.Response(403, json={"error": "No.", "code": "forbidden"})

    _, handler = recorder(respond)
    client = sync_client(handler)
    with pytest.raises(ApproveFirstError) as e0:
        client.post_message("k", "c1", "x")
    assert isinstance(e0.value, SaltApiError)
    assert e0.value.code == "approve_first" and e0.value.status == 403
    assert e0.value.sentence == "Each public post waits for the person's OK first."
    assert (e0.value.capability, e0.value.approved, e0.value.needed) == ("profile.post", 1, 3)
    assert "Each public post waits" in str(e0.value)
    with pytest.raises(PlanRequiredError) as e1:
        client.post_message("k", "c1", "x")
    assert e1.value.sentence == "Name the plan this post belongs to." and e1.value.status == 403
    with pytest.raises(ApproveFirstError) as e2:
        client.request_grant("k", grantor_id="o", capabilities=[{"capability": "profile.post", "selector": {}, "mode": "auto"}])
    assert e2.value.status == 422 and e2.value.needed == 3
    with pytest.raises(SaltApiError) as e3:
        client.post_message("k", "c1", "x")
    assert not isinstance(e3.value, (ApproveFirstError, PlanRequiredError)) and e3.value.code == "forbidden"


def test_check_grant_reads_needs_ask():
    _, handler = recorder(httpx.Response(200, json={"allowed": False, "mandate_id": "m1", "mode": "ask", "needs_ask": True}))
    check = sync_client(handler).check_grant("k", "profile.post")
    assert check["needs_ask"] is True and check["mode"] == "ask" and check["allowed"] is False


def test_replaces_and_plan_id_ride_on_the_proposal_and_revoke_returns_also_ended():
    calls, handler = recorder(lambda request, i: httpx.Response(200, json={"id": "m1", "also_ended": ["m2", "m3"]} if request.url.path.endswith("/revoke") else {"id": "m9"}))
    client = sync_client(handler)
    client.propose_mandate("k", {"grantor_id": "o", "capabilities": [], "replaces": "strat_old"})
    client.request_grant("k", grantor_id="o", capabilities=[], replaces="strat_old", plan_id="plan-9")
    assert json.loads(calls[0].content)["replaces"] == "strat_old"
    body = json.loads(calls[1].content)
    assert body["replaces"] == "strat_old" and body["plan_id"] == "plan-9"
    client.request_grant("k", grantor_id="o", capabilities=[])
    plain = json.loads(calls[2].content)
    assert "replaces" not in plain and "plan_id" not in plain
    assert client.revoke_mandate("k", "m1")["also_ended"] == ["m2", "m3"]


def make_agent() -> Agent:
    client = async_client(lambda request: httpx.Response(200, json={}))
    return Agent(host=HOST, api_key="agent-key", public_key="pub", private_key="priv", agent_id="self-agent", client=client)


def exercise(**overrides):
    ex = {
        "id": "ex-1", "mandate_id": "m1", "capability": "profile.post", "action": "mandates#ask", "decision": "approved",
        "summary": {"words": "Hello", "where": "my profile"},
        "actor": {"id": "self-agent", "username": "helper"}, "principal": {"id": "owner-1", "username": "dan"},
    }
    ex.update(overrides)
    return ex


async def test_on_approval_decided_carries_approved_and_a_typed_ask():
    agent = make_agent()
    seen: list[ApprovalDecidedContext] = []

    @agent.on_approval_decided
    async def on_decided(ctx: ApprovalDecidedContext):
        seen.append(ctx)

    await agent.dispatch(Event(type="approval_decided", body={"type": "approval_decided", "exercise": exercise(summary={"words": "Hello!", "asked_words": "Hello", "where": "my profile"}, target_label="your profile")}))
    await agent.dispatch(Event(type="approval_decided", body={"type": "approval_decided", "exercise": exercise(id="ex-2", capability="outreach.request", decision="denied", summary={"words_armored": "ARMOR", "where": "Sam"})}))
    await agent.dispatch(Event(type="approval_decided", body={"type": "approval_decided", "exercise": exercise(id=7, capability="money.pay", action="transfers#prepare", summary={})}))
    assert len(seen) == 3
    first, second, third = seen
    assert first.approved is True and isinstance(first.ask, ApprovalAsk)
    assert (first.ask.exercise_id, first.ask.capability, first.ask.where, first.ask.words, first.ask.asked_words) == ("ex-1", "profile.post", "my profile", "Hello!", "Hello")
    assert first.ask.edited is True and first.ask.target_label == "your profile"
    assert second.approved is False and second.ask.words_armored == "ARMOR" and second.ask.words is None and second.ask.edited is False
    assert third.ask is None and third.exercise["id"] == 7


def test_create_room_posts_a_group_create_and_plan_rides_as_x_salt_plan():
    calls, handler = recorder(httpx.Response(200, json={"id": "room-1"}))
    client = sync_client(handler)
    assert client.create_room("k", name="Dark Orbit", description="d", open=True, plan="plan-9")["id"] == "room-1"
    req = calls[0]
    assert req.method == "POST" and req.url.path == "/api/v1/chats"
    assert json.loads(req.content) == {"contact_ids": [], "name": "Dark Orbit", "description": "d", "encrypted": False}
    assert req.headers["x-salt-plan"] == "plan-9"
    client.create_room("k", name="Club", public=True, contact_ids=["u1"])
    assert json.loads(calls[1].content) == {"contact_ids": ["u1"], "name": "Club", "public": True}
    assert "x-salt-plan" not in calls[1].headers


async def test_async_create_room_and_typed_refusal():
    calls, handler = recorder(lambda request, i: httpx.Response(200, json={"id": "r"}) if i == 0 else httpx.Response(
        403, json={"error": "This waits for your owner's OK. File it with POST /mandates/:id/asks and wait for approval.", "code": "approve_first", "capability": "room.create"}))
    client = async_client(handler)
    await client.create_room("k", name="x", open=True, plan="p")
    assert calls[0].headers["x-salt-plan"] == "p"
    with pytest.raises(ApproveFirstError) as e:
        await client.create_room("k", name="x", open=True)
    assert e.value.capability == "room.create" and "waits for your owner" in e.value.sentence


def test_post_to_profile_makes_the_room_then_posts_both_under_the_plan():
    calls, handler = recorder(lambda request, i: httpx.Response(200, json={"room_id": "pr1"} if request.url.path.endswith("profile_room") else {"message_id": "m1"}))
    client = sync_client(handler)
    out = client.post_to_profile("k", text="hi", media=(b"\x01\x02", "image/png"), plan="plan-9")
    assert out == {"message_id": "m1"}
    assert [c.url.path for c in calls] == ["/api/v1/profile_room", "/api/v1/messages"]
    assert [c.headers["x-salt-plan"] for c in calls] == ["plan-9", "plan-9"]
    assert json.loads(calls[1].content) == {"chat_id": "pr1", "message": "hi", "post_media": "AQI=", "post_media_content_type": "image/png"}
    client.post_to_profile("k", text="plain")
    assert json.loads(calls[3].content) == {"chat_id": "pr1", "message": "plain"}
    assert all("x-salt-plan" not in c.headers for c in calls[2:])


async def test_async_post_to_profile_refusal_is_typed():
    calls, handler = recorder(lambda request, i: httpx.Response(200, json={"room_id": "pr1"}) if i == 0 else httpx.Response(
        403, json={"error": "Say which plan this is for (X-Salt-Plan).", "code": "plan_required"}))
    with pytest.raises(PlanRequiredError) as e:
        await async_client(handler).post_to_profile("k", text="hi")
    assert e.value.sentence == "Say which plan this is for (X-Salt-Plan)."


def test_ask_about_kind_media_digest_and_their_refusals():
    calls, handler = recorder(httpx.Response(202, json=ASKED))
    client = sync_client(handler)
    digest = media_digest(b"abc")
    assert digest == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    client.request_ask("k", "m1", capability="profile.post", words="pic", where="profile", kind="reply", media_digest=digest)
    assert json.loads(calls[0].content) == {"capability": "profile.post", "where": "profile", "kind": "reply", "media_digest": digest, "words": "pic"}
    client.request_ask("k", "m1", capability="room.create", words="Club", where="public", about="Weekly chess")
    assert json.loads(calls[1].content)["about"] == "Weekly chess"
    with pytest.raises(ValueError, match="profile.post only"):
        client.request_ask("k", "m1", capability="room.create", words="x", where="w", kind="post")
    with pytest.raises(ValueError, match="room.create only"):
        client.request_ask("k", "m1", capability="profile.post", words="x", where="w", about="a")
    assert len(calls) == 2


async def test_approved_outreach_is_decrypted_for_the_agent(keypair_a):
    armored = crypto.encrypt_for("Edited hello", [keypair_a.public_key])
    client = async_client(lambda request: httpx.Response(200, json={}))
    agent = Agent(host=HOST, api_key="k", public_key=keypair_a.public_key, private_key=keypair_a.private_key, agent_id="self-agent", passphrase="passphrase-a", client=client)
    seen = []

    @agent.on_approval_decided
    async def on_decided(ctx):
        seen.append(ctx)

    ex = exercise(capability="outreach.request", summary={"words_armored": armored, "asked_words_armored": "ORIGINAL", "where": "Sam", "recipient_id": "u7"})
    await agent.dispatch(Event(type="approval_decided", body={"type": "approval_decided", "exercise": ex}))
    assert seen[0].ask.words == "Edited hello" and seen[0].ask.edited is True and seen[0].ask.asked_words_armored == "ORIGINAL"
