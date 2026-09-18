from __future__ import annotations

import asyncio

import pytest

from saltapp.agent import AskTimeout
from saltapp.integrations.openai_agents import build_tools, resolve_interruptions

from .conftest import make_agent, recording_card_handler

pytest.importorskip("agents")


def test_build_tools_schema_and_approval_gating():
    agent = make_agent(recording_card_handler("card-x", []))
    tools = build_tools(agent, "chat-1")
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {
        "send_message", "ask_human", "request_payment", "send_invoice", "post_card", "get_payment_status",
    }
    assert by_name["request_payment"].needs_approval is True
    assert by_name["send_invoice"].needs_approval is True
    assert by_name["send_message"].needs_approval is False
    # strict schema must build cleanly (this is what a `dict[str, Any]`
    # line_items param used to break -- see _tools.py's LineItemInput note).
    assert by_name["send_invoice"].strict_json_schema is True


@pytest.mark.asyncio
async def test_resolve_interruptions_end_to_end_with_scripted_model():
    from agents import Agent as OaiAgent, Runner
    from agents.testing import ScriptedModel, assistant_message, function_call

    agent = make_agent(recording_card_handler("card-1", []))
    tools = build_tools(agent, "chat-1")

    model = ScriptedModel(
        [
            [
                function_call(
                    name="request_payment",
                    arguments={"receiver_id": "u1", "wallet_id": "w1", "amount": "5.00", "message": None},
                    call_id="call-1",
                )
            ],
            [assistant_message("Payment requested.")],
        ]
    )
    my_agent = OaiAgent(name="salt-bot", tools=tools, model=model)

    result = await Runner.run(my_agent, "Send $5 to u1")
    assert len(result.interruptions) == 1

    task = asyncio.create_task(
        resolve_interruptions(result, agent=agent, chat_id="chat-1", timeout_seconds=5)
    )
    await asyncio.sleep(0.05)
    agent._ask_registry.try_resolve_message("chat-1", "human-1", "approve")
    state = await asyncio.wait_for(task, timeout=2)

    result2 = await Runner.run(my_agent, state)
    assert result2.interruptions == []
    assert result2.final_output == "Payment requested."


@pytest.mark.asyncio
async def test_resolve_interruptions_times_out_with_no_answer():
    from agents import Agent as OaiAgent, Runner
    from agents.testing import ScriptedModel, function_call

    agent = make_agent(recording_card_handler("card-2", []))
    tools = build_tools(agent, "chat-2")
    model = ScriptedModel(
        [[function_call(name="request_payment", arguments={"receiver_id": "u1", "wallet_id": "w1", "amount": "5.00", "message": None}, call_id="call-2")]]
    )
    my_agent = OaiAgent(name="salt-bot", tools=tools, model=model)

    result = await Runner.run(my_agent, "Send $5 to u1")
    assert len(result.interruptions) == 1

    with pytest.raises(AskTimeout):
        await resolve_interruptions(result, agent=agent, chat_id="chat-2", timeout_seconds=0.2)
