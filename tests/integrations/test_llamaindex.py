from __future__ import annotations

import asyncio

import pytest

from saltapp.agent import AskTimeout
from saltapp.integrations.llamaindex import SaltToolSpec

from .conftest import first_action_id, make_agent, recording_card_handler

pytest.importorskip("llama_index.core")


def test_tool_spec_schema_generation():
    agent = make_agent(recording_card_handler("card-x", []))
    spec = SaltToolSpec(agent=agent, chat_id="chat-1")
    tools = spec.to_tool_list()
    names = {t.metadata.name for t in tools}
    assert names == {
        "send_message", "ask_human", "request_payment", "send_invoice", "post_card", "get_payment_status",
    }


@pytest.mark.asyncio
async def test_async_ask_human_tool_resolves_by_button_tap(posted):
    agent = make_agent(recording_card_handler("card-1", posted))
    spec = SaltToolSpec(agent=agent, chat_id="chat-1")
    ask_tool = next(t for t in spec.to_tool_list() if t.metadata.name == "ask_human")

    task = asyncio.create_task(ask_tool.async_fn(question="Which env?", options=["staging", "production"]))
    await asyncio.sleep(0.05)
    action_id = first_action_id(posted)
    agent._ask_registry.try_resolve_card_interaction("card-1", action_id, {"id": "human-1"}, None)

    answer = await asyncio.wait_for(task, timeout=2)
    assert answer == "staging"


def test_sync_ask_human_tool_resolves_by_typed_reply(posted):
    agent = make_agent(recording_card_handler("card-2", posted))
    spec = SaltToolSpec(agent=agent, chat_id="chat-2")

    async def driver():
        loop = asyncio.get_event_loop()
        run = loop.run_in_executor(None, lambda: spec.ask_human("Name?"))
        await asyncio.sleep(0.05)
        agent._ask_registry.try_resolve_message("chat-2", "human-1", "Dan")
        return await run

    assert asyncio.run(driver()) == "Dan"


@pytest.mark.asyncio
async def test_ask_human_tool_times_out():
    agent = make_agent(recording_card_handler("card-3", []))
    spec = SaltToolSpec(agent=agent, chat_id="chat-3")
    with pytest.raises(AskTimeout):
        await spec.aask_human("Anyone?", options=["yes"], timeout_seconds=0.2)
