from __future__ import annotations

import asyncio

import pytest

from saltapp.agent import AskTimeout
from saltapp.integrations.adk import build_confirmation_response, build_tools, resolve_confirmation_via_salt

from .conftest import first_action_id, make_agent, recording_card_handler

pytest.importorskip("google.adk")


def test_build_tools_schema_and_confirmation_gating():
    agent = make_agent(recording_card_handler("card-x", []))
    tools = build_tools(agent, "chat-1")
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {
        "send_message", "ask_human", "request_payment", "send_invoice", "post_card", "get_payment_status",
    }
    assert by_name["request_payment"]._require_confirmation is True
    assert by_name["send_invoice"]._require_confirmation is True
    assert by_name["send_message"]._require_confirmation is False


def test_build_confirmation_response_shape():
    response = build_confirmation_response("call-1", True)
    assert response.id == "call-1"
    assert response.name == "adk_request_confirmation"
    assert response.response == {"confirmed": True}


@pytest.mark.asyncio
async def test_resolve_confirmation_via_salt_approves_by_typed_reply():
    agent = make_agent(recording_card_handler("card-1", []))

    task = asyncio.create_task(
        resolve_confirmation_via_salt(call_id="call-1", hint="Approve payment?", agent=agent, chat_id="chat-1", timeout_seconds=5)
    )
    await asyncio.sleep(0.05)
    agent._ask_registry.try_resolve_message("chat-1", "human-1", "approve")

    response = await asyncio.wait_for(task, timeout=2)
    assert response.response == {"confirmed": True}


@pytest.mark.asyncio
async def test_resolve_confirmation_via_salt_rejects_by_typed_reply():
    agent = make_agent(recording_card_handler("card-2", []))

    task = asyncio.create_task(
        resolve_confirmation_via_salt(call_id="call-2", hint="Approve payment?", agent=agent, chat_id="chat-2", timeout_seconds=5)
    )
    await asyncio.sleep(0.05)
    agent._ask_registry.try_resolve_message("chat-2", "human-1", "reject")

    response = await asyncio.wait_for(task, timeout=2)
    assert response.response == {"confirmed": False}


@pytest.mark.asyncio
async def test_resolve_confirmation_via_salt_times_out():
    agent = make_agent(recording_card_handler("card-3", []))
    with pytest.raises(AskTimeout):
        await resolve_confirmation_via_salt(call_id="call-3", hint="Approve?", agent=agent, chat_id="chat-3", timeout_seconds=0.2)
