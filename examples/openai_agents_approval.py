"""OpenAI Agents SDK example: an Agent whose `request_payment` tool
`needs_approval`, resolved by asking on Salt (see `resolve_interruptions`)
instead of auto-approving or blocking on a console prompt.

Setup: pip install "saltapp[openai_agents]" and OPENAI_API_KEY, then:

    export SALT_API_KEY=...  APP_PUBLIC_KEY=...  APP_PRIVATE_KEY=...  PGP_PASSPHRASE=...
    python examples/openai_agents_approval.py

Message the agent "request $5 from bob for wallet w1"; it calls
request_payment, which interrupts the run for your approval on Salt.
"""

from __future__ import annotations

import asyncio
import os

from agents import Agent as OaiAgent, Runner

from saltapp.agent import Agent
from saltapp.integrations.openai_agents import build_tools, resolve_interruptions

agent = Agent(
    host=os.environ.get("SALT_HOST", "https://saltapp.ai"),
    api_key=os.environ["SALT_API_KEY"],
    public_key=os.environ["APP_PUBLIC_KEY"],
    private_key=os.environ["APP_PRIVATE_KEY"],
    passphrase=os.environ["PGP_PASSPHRASE"],
)


@agent.on_message
async def on_message(ctx) -> None:
    oai_agent = OaiAgent(
        name="salt-bot",
        instructions="You help the human message and request payments on Salt. Be brief.",
        tools=build_tools(agent, ctx.chat_id),
    )

    result = await Runner.run(oai_agent, ctx.text)
    while result.interruptions:
        state = await resolve_interruptions(result, agent=agent, chat_id=ctx.chat_id)
        result = await Runner.run(oai_agent, state)

    await ctx.reply(result.final_output or "Done.")


async def main() -> None:
    await agent.ensure_identity()
    await agent.run_socket_async()


if __name__ == "__main__":
    asyncio.run(main())
