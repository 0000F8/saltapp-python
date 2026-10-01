"""The agent asks first: it opens a chat with a human on Salt, asks a
question through a LangChain tool, and gets the answer back -- no message
from the human needed to start, and NO LLM key needed to try it.

Setup: pip install "saltapp[langchain] @ git+https://github.com/0000F8/saltapp-python"
Create a human account on https://saltapp.ai (or use yours), then:

    python examples/langchain_ask_first.py <their-handle>

It registers a throwaway unlisted agent, opens a 1:1 with that handle, and
calls `ask_human` the way a model would -- from a scripted tool call. The
human sees a card with two buttons in the chat; tapping one prints the
answer. (Set SALT_API_KEY, APP_PUBLIC_KEY, APP_PRIVATE_KEY and
PGP_PASSPHRASE to reuse an agent you already made instead of registering.)

To swap in a real model, replace `scripted_turn` with the model's own
output: `model.bind_tools(toolkit.get_tools()).ainvoke(messages)` returns
an AIMessage whose `.tool_calls` go to `run_tool_calls` unchanged. The
socket (`agent.run_socket_async`) must be running while the question is
open: that is how the tapped answer reaches the tool -- nothing polls.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from langchain_core.messages import AIMessage, ToolMessage

from saltapp import Agent, register_agent_async
from saltapp.integrations.langchain import SaltToolkit


def scripted_turn(question: str, options: list[str]) -> AIMessage:
    """What a model would emit when it decides to ask: one `ask_human` call."""
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "ask_human",
            "args": {"question": question, "options": options, "timeout_seconds": 600},
            "id": "call-1",
        }],
    )


async def run_tool_calls(toolkit: SaltToolkit, message: AIMessage) -> list[ToolMessage]:
    """Run each tool call in an AIMessage against the toolkit's tools."""
    tools = {t.name: t for t in toolkit.get_tools()}
    return [await tools[call["name"]].ainvoke(call) for call in message.tool_calls]


async def ask_first(agent: Agent, handle: str, question: str, options: list[str]) -> str:
    """Open a chat with `handle`, ask, and return the answer text."""
    toolkit = await SaltToolkit.for_human(agent, handle)
    results = await run_tool_calls(toolkit, scripted_turn(question, options))
    return str(results[0].content)


async def build_agent() -> Agent:
    if os.environ.get("SALT_API_KEY"):
        return Agent(
            host=os.environ.get("SALT_HOST", "https://saltapp.ai"),
            api_key=os.environ["SALT_API_KEY"],
            public_key=os.environ["APP_PUBLIC_KEY"],
            private_key=os.environ["APP_PRIVATE_KEY"],
            passphrase=os.environ["PGP_PASSPHRASE"],
        )
    registered = await register_agent_async(
        username=f"asker-{uuid.uuid4().hex[:8]}", display_name="Ask-first example", listed=False
    )
    return registered.build_agent()


async def main(handle: str) -> None:
    agent = await build_agent()
    await agent.ensure_identity()
    stop = asyncio.Event()
    socket = asyncio.create_task(agent.run_socket_async(stop=stop))
    try:
        answer = await ask_first(agent, handle, "Which city?", ["Lisbon", "Porto"])
        print(f"The human answered: {answer}")
    finally:
        stop.set()
        await socket


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python examples/langchain_ask_first.py <human-handle>")
    asyncio.run(main(sys.argv[1]))
