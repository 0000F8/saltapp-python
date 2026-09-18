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
    """Answers every request with `{"id": card_id}` and records each
    request's JSON body -- used to both satisfy `post_card`/messages calls
    and pull out the real (randomly-suffixed) action_id an `ask_human` call
    generated, the same way tests/test_ask.py does for `ctx.ask()` directly."""

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content) if request.content else {})
        return httpx.Response(200, json={"id": card_id})

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
