from __future__ import annotations

import asyncio

import pytest

from saltapp.agent import AskTimeout
from saltapp.integrations.langchain import SaltToolkit, ask_via_interrupt, SaltInterruptRunner

from .conftest import first_action_id, make_agent, recording_card_handler

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")


def test_toolkit_schema_generation():
    agent = make_agent(recording_card_handler("card-x", []))
    toolkit = SaltToolkit(agent=agent, chat_id="chat-1")
    tools = toolkit.get_tools()
    names = {t.name for t in tools}
    assert names == {
        "send_message", "ask_human", "request_payment", "send_invoice", "post_card", "get_payment_status",
    }
    send_message = next(t for t in tools if t.name == "send_message")
    schema = send_message.args_schema.model_json_schema()
    assert schema["properties"]["text"]["type"] == "string"


@pytest.mark.asyncio
async def test_toolkit_ask_human_tool_resolves_by_button_tap(posted):
    agent = make_agent(recording_card_handler("card-1", posted))
    toolkit = SaltToolkit(agent=agent, chat_id="chat-1")
    ask_tool = next(t for t in toolkit.get_tools() if t.name == "ask_human")

    task = asyncio.create_task(ask_tool.coroutine(question="Which env?", options=["staging", "production"]))
    await asyncio.sleep(0.05)
    action_id = first_action_id(posted)
    agent._ask_registry.try_resolve_card_interaction("card-1", action_id, {"id": "human-1"}, None)

    answer = await asyncio.wait_for(task, timeout=2)
    assert answer == "staging"


@pytest.mark.asyncio
async def test_toolkit_ask_human_tool_times_out():
    agent = make_agent(recording_card_handler("card-2", []))
    toolkit = SaltToolkit(agent=agent, chat_id="chat-2")
    ask_tool = next(t for t in toolkit.get_tools() if t.name == "ask_human")

    with pytest.raises(AskTimeout):
        await ask_tool.coroutine(question="Anyone?", options=["yes"], timeout_seconds=0.2)


@pytest.mark.asyncio
async def test_interrupt_runner_resumes_with_tapped_answer(posted):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    class State(TypedDict):
        approved: str

    def node(state: State) -> dict:
        answer = ask_via_interrupt("Approve this?", options=["Yes", "No"])
        return {"approved": answer}

    builder = StateGraph(State)
    builder.add_node("ask", node)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    agent = make_agent(recording_card_handler("card-3", posted))
    runner = SaltInterruptRunner(graph, agent=agent, chat_id="chat-3")

    task = asyncio.create_task(runner.arun({"approved": ""}, thread_id="thread-1"))
    await asyncio.sleep(0.1)
    action_id = first_action_id(posted)
    agent._ask_registry.try_resolve_card_interaction("card-3", action_id, {"id": "human-1"}, None)

    result = await asyncio.wait_for(task, timeout=3)
    assert result == {"approved": "Yes"}
