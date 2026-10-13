# Device moments: device_session_ended (end_reason "lost_contact" when the
# person's computer stopped answering for 15 s mid-session) and device_back
# (the same computer answering again, once per lost session, within 24 h).
# Plaintext agent-rail deliveries dispatched like the mandate events (see
# test_mandates.py). Shapes follow salt-api's DeviceSessionEventJob /
# DeviceBackEventJob and docs/DEVICE_PROTOCOL.md. Mirrors salt-agent-sdk's
# tests/device-moments.test.js.
from __future__ import annotations

import httpx
import pytest

from saltapp import crypto
from saltapp.agent import DEVICE_LOST_CONTACT, Agent, DeviceBackContext, DeviceSessionEndedContext
from saltapp.client import AsyncSaltClient
from saltapp.errors import SaltAppError
from saltapp.webhook import Event, classify

HOST = "https://example.saltapp.test"
AGENT_ID = "00000000-0000-0000-0000-0000000000a1"
PERSON_ID = "00000000-0000-0000-0000-0000000000b2"
DEVICE_ID = "00000000-0000-0000-0000-0000000000d3"
SESSION_ID = "00000000-0000-0000-0000-0000000005e5"
CHAT_ID = "00000000-0000-0000-0000-0000000000c4"


def ended_body(**overrides):
    body = {
        "type": "device_session_ended", "event": "ended", "session_id": SESSION_ID, "id": SESSION_ID,
        "agent_id": AGENT_ID, "device_id": DEVICE_ID, "mandate_id": "mandate-1", "chat_id": "lane-1",
        "status": "ended", "intent": "Reply to Maya in Mail", "end_reason": "lost_contact",
        "ended_at": "2026-10-12T09:00:15.000Z", "chat": {"id": "lane-1", "readers": {"ids": [PERSON_ID], "open": False}},
    }
    body.update(overrides)
    return body


def back_body(**overrides):
    body = {
        "type": "device_back", "device_id": DEVICE_ID, "device_name": "Work Mac", "previous_session_id": SESSION_ID,
        "offline_at": "2026-10-12T09:00:15.000Z", "back_at": "2026-10-12T09:03:02.000Z", "chat_id": CHAT_ID,
        "chat": {"id": CHAT_ID, "readers": {"ids": [PERSON_ID], "open": False}},
    }
    body.update(overrides)
    return body


def make_agent(handler=None, *, agent_id=AGENT_ID, public_key="pub", private_key="priv") -> Agent:
    transport = httpx.MockTransport(handler or (lambda request: httpx.Response(200, json={})))
    client = AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=transport))
    return Agent(host=HOST, api_key="agent-key", public_key=public_key, private_key=private_key, agent_id=agent_id, client=client)


@pytest.mark.parametrize("event_type", ["device_session_ended", "device_back"])
def test_classify_recognizes_the_device_events(event_type):
    assert classify({"type": event_type}) == event_type


async def test_device_session_ended_reaches_its_handler_with_lost_contact_set():
    agent = make_agent()
    got = []

    @agent.on_device_session_ended
    async def on_ended(ctx: DeviceSessionEndedContext):
        got.append(ctx)

    await agent.dispatch(Event(type="device_session_ended", body=ended_body()))
    assert len(got) == 1
    ctx = got[0]
    assert DEVICE_LOST_CONTACT == "lost_contact"
    assert ctx.session_id == SESSION_ID and ctx.device_id == DEVICE_ID and ctx.mandate_id == "mandate-1"
    assert ctx.end_reason == "lost_contact" and ctx.lost_contact is True
    assert ctx.lane_chat_id == "lane-1", "the device lane, not the person's 1:1"
    assert ctx.intent == "Reply to Maya in Mail" and ctx.ended_at == "2026-10-12T09:00:15.000Z"
    assert ctx.body["mandate_id"] == "mandate-1"
    assert not hasattr(ctx, "reply"), "no reply(): the lane is not where the person reads"


async def test_device_session_ended_fires_for_every_end_reason_but_lost_contact_is_only_true_for_lost_contact():
    agent = make_agent()
    got = []

    @agent.on_device_session_ended
    async def on_ended(ctx):
        got.append((ctx.end_reason, ctx.lost_contact))

    await agent.dispatch(Event(type="device_session_ended", body=ended_body(end_reason="stopped_by_person")))
    await agent.dispatch(Event(type="device_session_ended", body=ended_body(end_reason=None)))
    assert got == [("stopped_by_person", False), (None, False)]


async def test_the_devices_own_copy_of_device_session_ended_is_ignored():
    agent = make_agent(agent_id=DEVICE_ID)  # this process hosts the computer itself
    got = []

    @agent.on_device_session_ended
    async def on_ended(ctx):
        got.append(ctx)

    await agent.dispatch(Event(type="device_session_ended", body=ended_body()))
    assert got == []


async def test_device_events_without_a_handler_are_dropped_silently():
    agent = make_agent()
    await agent.dispatch(Event(type="device_session_ended", body=ended_body()))
    await agent.dispatch(Event(type="device_back", body=back_body()))


async def test_a_raising_device_handler_is_logged_not_raised(caplog):
    agent = make_agent()

    @agent.on_device_session_ended
    async def boom(ctx):
        raise RuntimeError("boom")

    await agent.dispatch(Event(type="device_session_ended", body=ended_body()))
    assert any("device_session_ended failed: boom" in r.getMessage() for r in caplog.records)


async def test_device_back_reaches_its_handler_and_reply_encrypts_for_the_one_to_one():
    agent_keys = crypto.generate_keypair("agent-pass")
    person_keys = crypto.generate_keypair("person-pass")
    members = [
        {"id": PERSON_ID, "username": "ada", "display_name": "Ada", "account_type": "User", "public_key": person_keys.public_key},
        {"id": AGENT_ID, "username": "helper", "display_name": "Helper", "account_type": "Agent", "public_key": agent_keys.public_key},
    ]
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": CHAT_ID, "session": {"users": members}})
        posted.append(request)
        return httpx.Response(200, json={"id": "m1"})

    agent = make_agent(handler, public_key=agent_keys.public_key, private_key=agent_keys.private_key)
    agent.identity.passphrase = "agent-pass"
    seen = []

    @agent.on_device_back
    async def on_back(ctx: DeviceBackContext):
        seen.append(ctx)
        await ctx.reply("Work Mac is back. Want me to finish “Reply to Maya in Mail”?")

    await agent.dispatch(Event(type="device_back", body=back_body()))
    ctx = seen[0]
    assert ctx.device_id == DEVICE_ID and ctx.device_name == "Work Mac"
    assert ctx.previous_session_id == SESSION_ID
    assert ctx.offline_at == "2026-10-12T09:00:15.000Z" and ctx.back_at == "2026-10-12T09:03:02.000Z"
    assert ctx.chat_id == CHAT_ID
    assert ctx.body["chat"]["readers"]["ids"] == [PERSON_ID]

    assert len(posted) == 1
    assert posted[0].url.path == "/api/v1/messages"
    import json as _json

    payload = _json.loads(posted[0].content)
    assert str(payload["chat_id"]) == CHAT_ID
    assert crypto.decrypt(payload["message"], person_keys.private_key, "person-pass") == "Work Mac is back. Want me to finish “Reply to Maya in Mail”?"


async def test_device_back_with_no_one_to_one_chat_id_is_none_and_reply_raises_a_clear_error():
    agent = make_agent()
    outcome = []

    @agent.on_device_back
    async def on_back(ctx):
        assert ctx.chat_id is None
        with pytest.raises(SaltAppError, match="no 1:1 with this agent"):
            await ctx.reply("hi")
        outcome.append("raised")

    await agent.dispatch(Event(type="device_back", body=back_body(chat_id=None, chat=None)))
    assert outcome == ["raised"]
