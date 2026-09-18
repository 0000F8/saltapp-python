from __future__ import annotations

import asyncio

import pytest

from saltapp.agent import AskTimeout
from saltapp.integrations.smolagents import build_tools

from .conftest import make_agent, recording_card_handler

pytest.importorskip("smolagents")


def test_build_tools_schema_generation():
    agent = make_agent(recording_card_handler("card-x", []))
    tools = build_tools(agent, "chat-1")
    names = {t.name for t in tools}
    assert names == {
        "send_message", "ask_human", "request_payment", "send_invoice", "post_card", "get_payment_status",
    }
    ask_human = next(t for t in tools if t.name == "ask_human")
    assert "question" in ask_human.inputs
    assert ask_human.output_type == "string"


def test_ask_human_tool_resolves_by_typed_reply(posted):
    agent = make_agent(recording_card_handler("card-1", posted))
    tools = build_tools(agent, "chat-1")
    ask_human = next(t for t in tools if t.name == "ask_human")

    async def driver():
        loop = asyncio.get_event_loop()
        run = loop.run_in_executor(None, lambda: ask_human(question="What's your name?"))
        await asyncio.sleep(0.05)
        agent._ask_registry.try_resolve_message("chat-1", "human-1", "Dan")
        return await run

    result = asyncio.run(driver())
    assert result == "Dan"


def test_ask_human_tool_times_out():
    agent = make_agent(recording_card_handler("card-2", []))
    tools = build_tools(agent, "chat-2")
    ask_human = next(t for t in tools if t.name == "ask_human")

    with pytest.raises(AskTimeout):
        ask_human(question="Anyone?", options=["yes"], timeout_seconds=0.2)
