from __future__ import annotations

import httpx
import pytest

from saltapp.agent import CONSULT_RUNAWAY_LIMIT, MAX_AGENT_TO_AGENT_REPLIES_PER_CHAT, Agent
from saltapp.client import AsyncSaltClient
from saltapp.webhook import Event

HOST = "https://example.saltapp.test"


def make_agent(handler=None, *, agent_id: str = "self-agent") -> Agent:
    def default_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"session": {"users": []}})

    transport = httpx.MockTransport(handler or default_handler)
    client = AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=transport))
    return Agent(host=HOST, api_key="key", public_key="pub", private_key="priv", agent_id=agent_id, client=client)


def message_event(*, chat_id="c1", sender_id="human-1", sender_account_type="User", text="hi", mentions=None, chat_meta=None, delivery_id=None):
    return Event(
        type="message",
        body={
            "message": {
                "chat_id": chat_id,
                "message": text,
                "user": {"id": sender_id, "account_type": sender_account_type, "username": "someone"},
                "mentions": mentions or [],
            },
            "chat": chat_meta or {},
        },
        delivery_id=delivery_id,
    )


@pytest.mark.asyncio
async def test_never_replies_to_own_message():
    agent = make_agent(agent_id="self-agent")
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx)

    await agent.dispatch(message_event(sender_id="self-agent"))
    assert received == []


@pytest.mark.asyncio
async def test_handles_ordinary_human_message():
    agent = make_agent()
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx.text)

    await agent.dispatch(message_event(text="hello there"))
    assert received == ["hello there"]


@pytest.mark.asyncio
async def test_gacm_stays_silent_when_another_agent_is_active():
    agent = make_agent(agent_id="self-agent")
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx)

    await agent.dispatch(message_event(chat_meta={"active_agent_id": "other-agent"}))
    assert received == []


@pytest.mark.asyncio
async def test_gacm_responds_when_this_agent_is_active():
    agent = make_agent(agent_id="self-agent")
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx)

    await agent.dispatch(message_event(chat_meta={"active_agent_id": "self-agent"}))
    assert len(received) == 1


@pytest.mark.asyncio
async def test_mention_rule_ignores_unmentioned_agent_message_when_human_present():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"session": {"users": [
            {"id": "human-1", "account_type": "User"},
            {"id": "other-agent", "account_type": "Agent"},
            {"id": "self-agent", "account_type": "Agent"},
        ]}})

    agent = make_agent(handler, agent_id="self-agent")
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx)

    await agent.dispatch(message_event(sender_id="other-agent", sender_account_type="Agent", mentions=[]))
    assert received == []


@pytest.mark.asyncio
async def test_mention_rule_answers_when_mentioned_with_human_present():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"session": {"users": [
            {"id": "human-1", "account_type": "User"},
            {"id": "other-agent", "account_type": "Agent"},
            {"id": "self-agent", "account_type": "Agent"},
        ]}})

    agent = make_agent(handler, agent_id="self-agent")
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx)

    await agent.dispatch(message_event(sender_id="other-agent", sender_account_type="Agent", mentions=["self-agent"]))
    assert len(received) == 1


@pytest.mark.asyncio
async def test_agent_to_agent_loop_guard_caps_replies_in_agent_only_chat():
    def handler(request: httpx.Request) -> httpx.Response:
        # Agent-only chat: no non-observer human present.
        return httpx.Response(200, json={"session": {"users": [
            {"id": "other-agent", "account_type": "Agent"},
            {"id": "self-agent", "account_type": "Agent"},
        ]}})

    agent = make_agent(handler, agent_id="self-agent")
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx)

    for _ in range(MAX_AGENT_TO_AGENT_REPLIES_PER_CHAT + 3):
        await agent.dispatch(message_event(sender_id="other-agent", sender_account_type="Agent"))

    assert len(received) == MAX_AGENT_TO_AGENT_REPLIES_PER_CHAT


@pytest.mark.asyncio
async def test_consult_lane_gets_higher_runaway_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"session": {"users": [
            {"id": "other-agent", "account_type": "Agent"},
            {"id": "self-agent", "account_type": "Agent"},
        ]}})

    agent = make_agent(handler, agent_id="self-agent")
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx)

    for _ in range(CONSULT_RUNAWAY_LIMIT + 2):
        await agent.dispatch(message_event(sender_id="other-agent", sender_account_type="Agent", chat_meta={"lane_kind": "consult"}))

    assert len(received) == CONSULT_RUNAWAY_LIMIT


@pytest.mark.asyncio
async def test_human_message_resets_agent_to_agent_count():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"session": {"users": [
            {"id": "other-agent", "account_type": "Agent"},
            {"id": "self-agent", "account_type": "Agent"},
        ]}})

    agent = make_agent(handler, agent_id="self-agent")
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx)

    for _ in range(MAX_AGENT_TO_AGENT_REPLIES_PER_CHAT):
        await agent.dispatch(message_event(sender_id="other-agent", sender_account_type="Agent"))
    assert len(received) == MAX_AGENT_TO_AGENT_REPLIES_PER_CHAT

    # A human speaking resets the count.
    await agent.dispatch(message_event(sender_id="human-1", sender_account_type="User", text="hey"))
    await agent.dispatch(message_event(sender_id="other-agent", sender_account_type="Agent"))
    assert len(received) == MAX_AGENT_TO_AGENT_REPLIES_PER_CHAT + 2


@pytest.mark.asyncio
async def test_delivery_id_dedupe_skips_repeat_delivery():
    agent = make_agent()
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx)

    event = message_event(delivery_id="dup-1")
    await agent.dispatch(event)
    await agent.dispatch(event)  # a retried delivery of the same event
    assert len(received) == 1


@pytest.mark.asyncio
async def test_system_event_type_is_not_treated_as_a_prompt():
    agent = make_agent()
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx)

    event = Event(type="message", body={"message": {"chat_id": "c1", "event_type": "call_missed", "user": {"id": "human-1"}}, "chat": {}})
    await agent.dispatch(event)
    assert received == []
