from __future__ import annotations

import asyncio
import sys

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from saltapp.agent import AskTimeout
from saltapp.integrations.langchain import SaltToolkit, ask_via_interrupt, SaltInterruptRunner

from .conftest import first_action_id, make_agent, recording_card_handler

# ask_via_interrupt()/SaltInterruptRunner require Python 3.11+ -- see
# saltapp/integrations/langchain.py's header comment and
# https://github.com/langchain-ai/langgraph/issues/8203. Below Python 3.11
# the tests assert the SDK's own clear RuntimeError rather than driving the
# real interrupt/resume flow, which upstream langgraph does not reliably
# support on that interpreter.
_PY311_PLUS = sys.version_info >= (3, 11)


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

    if not _PY311_PLUS:
        # See module header: langgraph's interrupt() is not reliable from an
        # async run on Python <3.11 (upstream, langgraph#8203), so
        # SaltInterruptRunner refuses clearly instead of driving it.
        with pytest.raises(RuntimeError, match="Python 3.11 or later"):
            await runner.arun({"approved": ""}, thread_id="thread-1")
        return

    task = asyncio.create_task(runner.arun({"approved": ""}, thread_id="thread-1"))
    await asyncio.sleep(0.1)
    action_id = first_action_id(posted)
    agent._ask_registry.try_resolve_card_interaction("card-3", action_id, {"id": "human-1"}, None)

    result = await asyncio.wait_for(task, timeout=3)
    assert result == {"approved": "Yes"}


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _PY311_PLUS,
    reason="ask_via_interrupt requires Python 3.11+ (see langgraph#8203); "
    "test_interrupt_runner_resumes_with_tapped_answer covers the <3.11 refusal",
)
async def test_interrupt_runner_two_node_graph_pauses_and_resumes(posted):
    """A real, multi-node graph: a node runs before the ask, the ask node
    pauses and resumes with the human's tapped answer, and a node after it
    uses that answer -- proves the interrupt/resume bridge in a shape closer
    to a production graph than the single-node test above."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    class State(TypedDict):
        prepared: bool
        approved: str
        finalized: bool

    def prepare(state: State) -> dict:
        return {"prepared": True}

    def ask(state: State) -> dict:
        answer = ask_via_interrupt("Approve this?", options=["Yes", "No"])
        return {"approved": answer}

    def finalize(state: State) -> dict:
        return {"finalized": state["approved"] == "Yes"}

    builder = StateGraph(State)
    builder.add_node("prepare", prepare)
    builder.add_node("ask", ask)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "ask")
    builder.add_edge("ask", "finalize")
    builder.add_edge("finalize", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    agent = make_agent(recording_card_handler("card-4", posted))
    runner = SaltInterruptRunner(graph, agent=agent, chat_id="chat-4")

    task = asyncio.create_task(
        runner.arun({"prepared": False, "approved": "", "finalized": False}, thread_id="thread-2")
    )
    await asyncio.sleep(0.1)
    action_id = first_action_id(posted)
    agent._ask_registry.try_resolve_card_interaction("card-4", action_id, {"id": "human-1"}, None)

    result = await asyncio.wait_for(task, timeout=3)
    assert result == {"prepared": True, "approved": "Yes", "finalized": True}


@pytest.mark.asyncio
async def test_for_human_opens_the_chat_and_scopes_the_tools():
    import json

    import httpx

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode(), request.content))
        if request.url.path == "/api/v1/search/contacts":
            return httpx.Response(200, json=[{"id": "u-1", "username": "ada"}])
        return httpx.Response(200, json={"id": "chat-42"})

    toolkit = await SaltToolkit.for_human(make_agent(handler), "@Ada")
    assert toolkit.chat_id == "chat-42"
    assert seen[0][2] == "username=Ada"
    assert json.loads(seen[1][3]) == {"contact_id": "u-1"}
    assert {t.name for t in toolkit.get_tools()} >= {"ask_human", "send_message"}


@pytest.mark.asyncio
async def test_for_human_names_a_missing_handle():
    import httpx

    from saltapp.errors import SaltAppError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with pytest.raises(SaltAppError, match="@nobody"):
        await SaltToolkit.for_human(make_agent(handler), "nobody")
