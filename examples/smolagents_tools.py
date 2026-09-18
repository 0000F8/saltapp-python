"""smolagents example: a ToolCallingAgent with Salt's six tools, including
`ask_human` -- the model itself decides to ask the human a real question
on Salt mid-task, no separate approval framework needed.

Setup: pip install "saltapp[smolagents]" and a model smolagents can use
(e.g. HF_TOKEN for an InferenceClientModel), then:

    export SALT_API_KEY=...  APP_PUBLIC_KEY=...  APP_PRIVATE_KEY=...  PGP_PASSPHRASE=...
    python examples/smolagents_tools.py

Message the agent "send a payment request, but ask me for the amount
first" -- it calls ask_human to get the amount, then request_payment.
"""

from __future__ import annotations

import asyncio
import os

from smolagents import InferenceClientModel, ToolCallingAgent

from saltapp.agent import Agent
from saltapp.integrations.smolagents import build_tools
from saltapp.socket import FileCursorStore

agent = Agent(
    host=os.environ.get("SALT_HOST", "https://saltapp.ai"),
    api_key=os.environ["SALT_API_KEY"],
    public_key=os.environ["APP_PUBLIC_KEY"],
    private_key=os.environ["APP_PRIVATE_KEY"],
    passphrase=os.environ["PGP_PASSPHRASE"],
)


@agent.on_message
async def on_message(ctx) -> None:
    model = InferenceClientModel()
    smol_agent = ToolCallingAgent(
        tools=build_tools(agent, ctx.chat_id),
        model=model,
        instructions="You help the human message, ask questions, and request payments on Salt. Be brief.",
    )
    # smolagents' agent.run() is blocking (its own HTTP calls under the
    # hood) -- run it off the event loop so the socket poller keeps going.
    result = await asyncio.to_thread(smol_agent.run, ctx.text)
    await ctx.reply(str(result))


async def main() -> None:
    await agent.ensure_identity()
    await agent.run_socket_async(cursor_store=FileCursorStore("./data/smolagents_cursor.txt"))


if __name__ == "__main__":
    asyncio.run(main())
