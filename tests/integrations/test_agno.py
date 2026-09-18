from __future__ import annotations

import asyncio

import pytest

from saltapp.agent import AskTimeout
from saltapp.integrations.agno import SaltToolkit, resolve_agno_run

from .conftest import first_action_id, make_agent, recording_card_handler

pytest.importorskip("agno")


def test_toolkit_schema_and_confirmation_gating():
    agent = make_agent(recording_card_handler("card-x", []))
    toolkit = SaltToolkit(agent=agent, chat_id="chat-1")
    names = set(toolkit.functions.keys())
    assert names == {
        "send_message", "ask_human", "request_payment", "send_invoice", "post_card", "get_payment_status",
    }
    # request_payment/send_invoice move real money -- gated for confirmation;
    # everything else isn't.
    assert toolkit.functions["request_payment"].requires_confirmation is True
    assert toolkit.functions["send_invoice"].requires_confirmation is True
    assert toolkit.functions["send_message"].requires_confirmation is False


def test_ask_human_method_resolves_by_typed_reply(posted):
    agent = make_agent(recording_card_handler("card-1", posted))
    toolkit = SaltToolkit(agent=agent, chat_id="chat-1")

    async def driver():
        loop = asyncio.get_event_loop()
        run = loop.run_in_executor(None, lambda: toolkit.ask_human("What's your name?"))
        await asyncio.sleep(0.05)
        agent._ask_registry.try_resolve_message("chat-1", "human-1", "Dan")
        return await run

    result = asyncio.run(driver())
    assert result == "Dan"


def test_ask_human_method_times_out():
    agent = make_agent(recording_card_handler("card-2", []))
    toolkit = SaltToolkit(agent=agent, chat_id="chat-2")
    with pytest.raises(AskTimeout):
        toolkit.ask_human("Anyone?", timeout_seconds=0.2)


class _FakeRequirement:
    def __init__(self, *, needs_confirmation=False, needs_user_input=False, needs_external_execution=False, tool_name="request_payment", tool_args=None):
        self.needs_confirmation = needs_confirmation
        self.needs_user_input = needs_user_input
        self.needs_external_execution = needs_external_execution
        self.tool_execution = type("TE", (), {"tool_name": tool_name, "tool_args": tool_args or {}})()
        self.user_input_schema = []
        self.confirmed = None

    def confirm(self):
        self.confirmed = True

    def reject(self):
        self.confirmed = False


class _FakeRunResponse:
    def __init__(self, requirements, is_paused=True, run_id="run-1"):
        self.active_requirements = requirements
        self.requirements = requirements
        self.is_paused = is_paused
        self.run_id = run_id


class _FakeAgnoAgent:
    def __init__(self):
        self.continue_calls = []

    def continue_run(self, *, run_id, requirements):
        self.continue_calls.append((run_id, requirements))
        return _FakeRunResponse([], is_paused=False)


def test_resolve_agno_run_confirms_via_salt_and_calls_continue_run(posted):
    agent = make_agent(recording_card_handler("card-3", posted))
    requirement = _FakeRequirement(needs_confirmation=True, tool_name="request_payment", tool_args={"amount": "5.00"})
    paused = _FakeRunResponse([requirement])
    agno_agent = _FakeAgnoAgent()

    async def driver():
        loop = asyncio.get_event_loop()
        run = loop.run_in_executor(
            None, lambda: resolve_agno_run(agno_agent, paused, agent=agent, chat_id="chat-3", timeout_seconds=5)
        )
        await asyncio.sleep(0.05)
        agent._ask_registry.try_resolve_message("chat-3", "human-1", "approve")
        return await run

    result = asyncio.run(driver())
    assert requirement.confirmed is True
    assert agno_agent.continue_calls == [("run-1", [requirement])]
    assert result.is_paused is False


def test_resolve_agno_run_rejects_external_execution():
    agent = make_agent(recording_card_handler("card-4", []))
    requirement = _FakeRequirement(needs_external_execution=True)
    paused = _FakeRunResponse([requirement])
    agno_agent = _FakeAgnoAgent()

    with pytest.raises(NotImplementedError):
        resolve_agno_run(agno_agent, paused, agent=agent, chat_id="chat-4", timeout_seconds=1)
