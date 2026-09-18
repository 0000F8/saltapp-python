"""Pydantic AI example: an agent with a `requires_approval=True` tool, whose
approval request is resolved by asking on Salt instead of trusting the
model or blocking on a console `input()`.

Setup: pip install "saltapp[pydantic_ai]" and an LLM pydantic-ai can use
(e.g. OPENAI_API_KEY), then:

    export SALT_API_KEY=...  APP_PUBLIC_KEY=...  APP_PRIVATE_KEY=...  PGP_PASSPHRASE=...
    python examples/pydantic_ai_deferred_approval.py

Message the agent "send $5 to alice"; it calls a gated `send_payment`
tool, which pauses for your approval on Salt before "sending" anything.
"""

from __future__ import annotations

import asyncio
import os

from pydantic_ai import Agent as PaiAgent, DeferredToolRequests

from saltapp.agent import Agent
from saltapp.integrations.pydantic_ai import build_toolset, resolve_deferred_approvals

agent = Agent(
    host=os.environ.get("SALT_HOST", "https://saltapp.ai"),
    api_key=os.environ["SALT_API_KEY"],
    public_key=os.environ["APP_PUBLIC_KEY"],
    private_key=os.environ["APP_PRIVATE_KEY"],
    passphrase=os.environ["PGP_PASSPHRASE"],
)


@agent.on_message
async def on_message(ctx) -> None:
    pai_agent = PaiAgent(
        "openai:gpt-5.2",
        toolsets=[build_toolset(agent, ctx.chat_id)],
        output_type=[str, DeferredToolRequests],
        instructions="You help the human message and pay people on Salt. Be brief.",
    )

    # request_payment is one of the six shared Salt tools -- gate it with
    # pydantic-ai's own approval mechanism (separately from Salt's own
    # ask_human tool, to show both HITL shapes in one example).
    @pai_agent.tool_plain(requires_approval=True)
    def send_payment(receiver_handle: str, amount_usd: str) -> str:
        return f"Sent ${amount_usd} to {receiver_handle}."

    result = await pai_agent.run(ctx.text)
    while isinstance(result.output, DeferredToolRequests):
        results = await resolve_deferred_approvals(result.output, agent=agent, chat_id=ctx.chat_id)
        result = await pai_agent.run(message_history=result.all_messages(), deferred_tool_results=results)

    await ctx.reply(result.output)


async def main() -> None:
    await agent.ensure_identity()
    await agent.run_socket_async()


if __name__ == "__main__":
    asyncio.run(main())
