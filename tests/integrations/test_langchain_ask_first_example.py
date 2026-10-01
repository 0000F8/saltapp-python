from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib

import httpx
import pytest

pytest.importorskip("langchain_core")

from .conftest import make_agent

_PATH = pathlib.Path(__file__).resolve().parents[2] / "examples" / "langchain_ask_first.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("langchain_ask_first", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_example_opens_the_chat_asks_and_returns_the_tapped_answer():
    example = _load_example()
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/api/v1/search/contacts":
            return httpx.Response(200, json=[{"id": "u-1", "username": "ada"}])
        if request.url.path == "/api/v1/chats":
            return httpx.Response(200, json={"id": "chat-9"})
        posted.append(body)
        return httpx.Response(200, json={"resource_id": "card-9", "resource": {"id": "card-9"}})

    agent = make_agent(handler)
    task = asyncio.create_task(example.ask_first(agent, "ada", "Which city?", ["Lisbon", "Porto"]))
    for _ in range(100):
        if posted:
            break
        await asyncio.sleep(0.02)
    buttons = next(b for b in posted[0]["blocks"] if b["type"] == "actions")["elements"]
    porto = next(b for b in buttons if b["label"] == "Porto")
    agent._ask_registry.try_resolve_card_interaction("card-9", porto["action_id"], {"id": "u-1"}, None)

    assert await asyncio.wait_for(task, timeout=3) == "Porto"
