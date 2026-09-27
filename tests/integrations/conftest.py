# Shared test plumbing for every saltapp.integrations.<framework> test
# file: build an Agent whose whole network surface is an httpx.MockTransport
# (never real HTTP), plus the same card-posting-then-tapping choreography
# tests/test_ask.py already uses for saltapp.agent.Agent.ask() directly --
# reused here because every framework's HITL bridge ultimately calls that
# same ask().
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from saltapp.agent import Agent
from saltapp.client import AsyncSaltClient

HOST = "https://example.saltapp.test"


def make_agent(handler: Callable[[httpx.Request], httpx.Response]) -> Agent:
    transport = httpx.MockTransport(handler)
    client = AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=transport))
    return Agent(
        host=HOST, api_key="key", public_key="pub", private_key="priv", passphrase="pw",
        agent_id="agent-1", client=client,
    )


def recording_card_handler(card_id: str, posted: list) -> Callable[[httpx.Request], httpx.Response]:
    """Answers every request and records each request's JSON body -- used
    to both satisfy `post_card`/messages calls and pull out the real
    (randomly-suffixed) action_id an `ask_human` call generated, the same
    way tests/test_ask.py does for `ctx.ask()` directly.

    The card-shaped response mirrors the REAL POST /api/v1/cards response
    (salt-api `cards_controller#create` renders the card's chat BUBBLE via
    `Message#formatted_message` -- see openapi.json's `Message` schema,
    `Card#as_chat_resource`): there is no top-level `id` or `card_id`, only
    `resource_id` (mirrored at `resource.id`). This used to answer with
    the bare `{"id": card_id}` a hand-written guess would produce, which
    is exactly the shape `saltapp.agent._BaseContext.ask()` used to
    assume -- the fake and the bug matched each other and hid this across
    every framework integration test that used it."""

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content) if request.content else {})
        return httpx.Response(
            200,
            json={
                "chat_id": "chat-1",
                "message": "📇 shared a card",
                "message_id": "msg-1",
                "resource_type": "Card",
                "resource_id": card_id,
                "resource": {"id": card_id, "card_type": "blocks", "state": {"blocks": []}},
            },
        )

    return handler


def first_action_id(posted: list) -> str:
    for body in posted:
        for block in body.get("blocks", []):
            if block.get("type") == "actions":
                return block["elements"][0]["action_id"]
    raise AssertionError("no actions block was posted")


@pytest.fixture()
def posted() -> list:
    return []
