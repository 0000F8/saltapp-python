from __future__ import annotations

import asyncio

import pytest

from saltapp.agent import AskTimeout
from saltapp.integrations.pydantic_ai import build_toolset, resolve_deferred_approvals

from .conftest import first_action_id, make_agent, recording_card_handler

pydantic_ai = pytest.importorskip("pydantic_ai")


def test_toolset_exposes_all_six_tools():
    agent = make_agent(recording_card_handler("card-x", []))
    toolset = build_toolset(agent, "chat-1")
    tool_defs = asyncio.run(toolset.get_tools(_fake_run_context()))
    assert set(tool_defs.keys()) == {
        "send_message", "ask_human", "request_payment", "send_invoice", "post_card", "get_payment_status",
    }


def _fake_run_context():
    from pydantic_ai import RunContext

    # A minimal RunContext -- FunctionToolset.get_tools only reads a few
    # attributes off it for a plain (non-approval-gated) toolset.
    return RunContext(deps=None, model=None, usage=None, prompt=None)


@pytest.mark.asyncio
async def test_resolve_deferred_approvals_approves_via_typed_reply(posted):
    from pydantic_ai import ToolDenied
    from pydantic_ai.messages import ToolCallPart

    agent = make_agent(recording_card_handler("card-1", posted))
    call = ToolCallPart(tool_name="delete_file", args={"path": "x.py"}, tool_call_id="call-1")

    class FakeRequests:
        approvals = [call]
        calls: list = []

    task = asyncio.create_task(
        resolve_deferred_approvals(FakeRequests(), agent=agent, chat_id="chat-1", timeout_seconds=5)
    )
    await asyncio.sleep(0.05)
    agent._ask_registry.try_resolve_message("chat-1", "human-1", "approve")

    results = await asyncio.wait_for(task, timeout=2)
    assert results.approvals["call-1"] is True


@pytest.mark.asyncio
async def test_resolve_deferred_approvals_denies_via_typed_reply(posted):
    from pydantic_ai import ToolDenied
    from pydantic_ai.messages import ToolCallPart

    agent = make_agent(recording_card_handler("card-2", posted))
    call = ToolCallPart(tool_name="delete_file", args={"path": "x.py"}, tool_call_id="call-2")

    class FakeRequests:
        approvals = [call]
        calls: list = []

    task = asyncio.create_task(
        resolve_deferred_approvals(FakeRequests(), agent=agent, chat_id="chat-2", timeout_seconds=5)
    )
    await asyncio.sleep(0.05)
    agent._ask_registry.try_resolve_message("chat-2", "human-1", "deny")

    results = await asyncio.wait_for(task, timeout=2)
    assert isinstance(results.approvals["call-2"], ToolDenied)


@pytest.mark.asyncio
async def test_resolve_deferred_approvals_times_out():
    from pydantic_ai.messages import ToolCallPart

    agent = make_agent(recording_card_handler("card-3", []))
    call = ToolCallPart(tool_name="delete_file", args={"path": "x.py"}, tool_call_id="call-3")

    class FakeRequests:
        approvals = [call]
        calls: list = []

    with pytest.raises(AskTimeout):
        await resolve_deferred_approvals(FakeRequests(), agent=agent, chat_id="chat-3", timeout_seconds=0.2)
