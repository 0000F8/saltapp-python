from __future__ import annotations

import asyncio

import pytest

from saltapp.agent import AskTimeout
from saltapp.integrations.camel import SaltHumanToolkit

from .conftest import make_agent, recording_card_handler

pytest.importorskip("camel")


def test_toolkit_exposes_human_toolkit_compatible_names_plus_the_rest():
    agent = make_agent(recording_card_handler("card-x", []))
    toolkit = SaltHumanToolkit(agent=agent, chat_id="chat-1")
    names = {t.get_function_name() for t in toolkit.get_tools()}
    # ask_human_via_console / send_message_to_user are camel.toolkits.HumanToolkit's
    # own two method names -- this toolkit must expose them verbatim so it
    # drops in wherever HumanToolkit is used today.
    assert names == {
        "ask_human_via_console", "send_message_to_user",
        "request_payment", "send_invoice", "post_card", "get_payment_status",
    }


def test_ask_human_via_console_resolves_by_typed_reply():
    agent = make_agent(recording_card_handler("card-1", []))
    toolkit = SaltHumanToolkit(agent=agent, chat_id="chat-1")

    async def driver():
        loop = asyncio.get_event_loop()
        run = loop.run_in_executor(None, lambda: toolkit.ask_human_via_console("What's your name?"))
        await asyncio.sleep(0.05)
        agent._ask_registry.try_resolve_message("chat-1", "human-1", "Dan")
        return await run

    assert asyncio.run(driver()) == "Dan"


@pytest.mark.asyncio
async def test_ask_human_via_console_times_out():
    # `ask_human_via_console(question)` has no timeout parameter -- it
    # matches camel.toolkits.HumanToolkit's own zero-extra-argument
    # signature exactly -- so it always runs on SaltTools.ask_human's 120s
    # default. Exercise that default path directly through `_salt` (the
    # same underlying call `ask_human_via_console` makes) with a short
    # timeout instead of blocking a thread for two minutes.
    agent = make_agent(recording_card_handler("card-2", []))
    toolkit = SaltHumanToolkit(agent=agent, chat_id="chat-2")

    with pytest.raises(AskTimeout):
        await toolkit._salt.ask_human("Anyone?", timeout_seconds=0.2)
