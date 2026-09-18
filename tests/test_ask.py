from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from saltapp.agent import Agent, AskTimeout, MessageContext

HOST = "https://example.saltapp.test"


def make_agent(handler) -> Agent:
    from saltapp.client import AsyncSaltClient

    transport = httpx.MockTransport(handler)
    client = AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=transport))
    return Agent(
        host=HOST, api_key="key", public_key="pub", private_key="priv", passphrase="pw",
        agent_id="agent-1", client=client,
    )


def make_recording_card_handler(card_id: str, posted: list) -> object:
    """A mock transport handler for POST /api/v1/cards that both answers
    with `card_id` and records the real `blocks` the agent posted, so a
    test can pull out the actual (randomly-suffixed) action_id ask()
    generated instead of guessing it."""

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"id": card_id})

    return handler


def first_action_id(posted: list) -> str:
    body = posted[0]
    for block in body["blocks"]:
        if block.get("type") == "actions":
            return block["elements"][0]["action_id"]
    raise AssertionError("no actions block was posted")


def make_ctx(agent: Agent, chat_id: str = "chat-1") -> MessageContext:
    return MessageContext(
        agent, chat_id=chat_id, sender_id="human-1", sender={"id": "human-1", "username": "dan", "account_type": "User"},
        text="hi", room_id=chat_id, chat_meta={}, raw_message={},
    )


@pytest.mark.asyncio
async def test_ask_resolves_by_button_tap():
    posted: list = []
    agent = make_agent(make_recording_card_handler("card-1", posted))
    ctx = make_ctx(agent)

    ask_task = asyncio.create_task(ctx.ask("Which environment?", options=["staging", "production"], timeout=5))
    await asyncio.sleep(0.05)  # let the card post + registration happen

    action_id = first_action_id(posted)
    # Simulate the incoming card_interaction webhook/socket event: the
    # tapped button's real action_id, on the card ask() actually posted.
    resolved = agent._ask_registry.try_resolve_card_interaction(
        "card-1", action_id, {"id": "human-1"}, None
    )
    assert resolved is True

    result = await asyncio.wait_for(ask_task, timeout=2)
    assert result.kind == "option"
    assert result.action_id == action_id
    assert result.value == "staging"  # ask() maps the FIRST option's action_id back to its label


@pytest.mark.asyncio
async def test_ask_resolves_by_typed_reply():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "card-2"})

    agent = make_agent(handler)
    ctx = make_ctx(agent, chat_id="chat-2")

    ask_task = asyncio.create_task(ctx.ask("What's your name?", free_text=True, timeout=5))
    await asyncio.sleep(0.05)

    resolved = agent._ask_registry.try_resolve_message("chat-2", "human-1", "Dan")
    assert resolved is True

    result = await asyncio.wait_for(ask_task, timeout=2)
    assert result.kind == "text"
    assert result.text == "Dan"


@pytest.mark.asyncio
async def test_ask_free_text_ignores_wrong_sender_when_scoped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "card-3"})

    agent = make_agent(handler)
    ctx = make_ctx(agent, chat_id="chat-3")

    ask_task = asyncio.create_task(ctx.ask("Confirm?", free_text=True, from_user_id="human-1", timeout=1))
    await asyncio.sleep(0.05)

    # A message from someone else must not resolve this ask.
    resolved = agent._ask_registry.try_resolve_message("chat-3", "someone-else", "not it")
    assert resolved is False

    with pytest.raises(AskTimeout):
        await asyncio.wait_for(ask_task, timeout=3)


@pytest.mark.asyncio
async def test_ask_times_out_with_no_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "card-4"})

    agent = make_agent(handler)
    ctx = make_ctx(agent, chat_id="chat-4")

    with pytest.raises(AskTimeout):
        await ctx.ask("Anyone there?", options=["yes"], timeout=0.2)

    # The waiter must be cleaned up after a timeout, not leaked.
    assert "card-4" not in agent._ask_registry._by_card_id


@pytest.mark.asyncio
async def test_approve_true_on_yes_button():
    posted: list = []
    agent = make_agent(make_recording_card_handler("card-5", posted))
    ctx = make_ctx(agent, chat_id="chat-5")

    approve_task = asyncio.create_task(ctx.approve("Really delete everything?", timeout=5))
    await asyncio.sleep(0.05)

    # approve() builds ["Yes", "No"] in that order, so the first button's
    # real action_id is "Yes".
    action_id = first_action_id(posted)
    agent._ask_registry.try_resolve_card_interaction("card-5", action_id, {"id": "human-1"}, None)

    result = await asyncio.wait_for(approve_task, timeout=2)
    assert result is True


@pytest.mark.asyncio
async def test_approve_false_on_typed_no():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "card-6"})

    agent = make_agent(handler)
    ctx = make_ctx(agent, chat_id="chat-6")

    approve_task = asyncio.create_task(ctx.approve("Proceed?", timeout=5))
    await asyncio.sleep(0.05)

    agent._ask_registry.try_resolve_message("chat-6", "human-1", "no")
    result = await asyncio.wait_for(approve_task, timeout=2)
    assert result is False
