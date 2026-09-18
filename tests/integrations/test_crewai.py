from __future__ import annotations

import asyncio

import pytest

from saltapp.agent import AskTimeout
from saltapp.integrations.crewai import SaltAskHumanTool, SaltHumanInputProvider, salt_tools

from .conftest import first_action_id, make_agent, recording_card_handler

pytest.importorskip("crewai")


def test_salt_tools_schema_generation():
    agent = make_agent(recording_card_handler("card-x", []))
    tools = salt_tools(agent, "chat-1")
    names = {t.name for t in tools}
    assert names == {
        "send_message", "ask_human", "request_payment", "send_invoice", "post_card", "get_payment_status",
    }
    send_message = next(t for t in tools if t.name == "send_message")
    assert "text" in send_message.args_schema.model_fields


@pytest.mark.asyncio
async def test_ask_human_tool_arun_resolves_by_button_tap(posted):
    agent = make_agent(recording_card_handler("card-1", posted))
    tool = SaltAskHumanTool(agent, "chat-1")

    task = asyncio.create_task(tool._arun(question="Which env?", options=["staging", "production"]))
    await asyncio.sleep(0.05)
    action_id = first_action_id(posted)
    agent._ask_registry.try_resolve_card_interaction("card-1", action_id, {"id": "human-1"}, None)

    answer = await asyncio.wait_for(task, timeout=2)
    assert answer == "staging"


def test_ask_human_tool_run_sync_bridge_resolves_by_typed_reply(posted):
    agent = make_agent(recording_card_handler("card-2", posted))
    tool = SaltAskHumanTool(agent, "chat-2")

    async def driver():
        loop = asyncio.get_event_loop()
        run_task = loop.run_in_executor(None, lambda: tool._run(question="Name?", options=None))
        await asyncio.sleep(0.05)
        agent._ask_registry.try_resolve_message("chat-2", "human-1", "Dan")
        return await run_task

    result = asyncio.run(driver())
    assert result == "Dan"


@pytest.mark.asyncio
async def test_ask_human_tool_times_out():
    agent = make_agent(recording_card_handler("card-3", []))
    tool = SaltAskHumanTool(agent, "chat-3")

    with pytest.raises(AskTimeout):
        await tool._arun(question="Anyone?", options=["yes"], timeout_seconds=0.2)


@pytest.mark.asyncio
async def test_human_input_provider_asks_on_salt_and_returns_reply(posted):
    agent = make_agent(recording_card_handler("card-4", posted))
    provider = SaltHumanInputProvider(agent, "chat-4")

    task = asyncio.create_task(provider.handle_feedback_async("Draft looks like: Hello world"))
    await asyncio.sleep(0.05)
    agent._ask_registry.try_resolve_message("chat-4", "human-1", "looks good")

    result = await asyncio.wait_for(task, timeout=2)
    assert result == "looks good"
    assert "Hello world" in posted[0]["text"]
