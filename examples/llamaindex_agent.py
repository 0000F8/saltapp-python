"""LlamaIndex example: a FunctionAgent with Salt's six tools via
`SaltToolSpec`, including `ask_human` -- the model calls it to ask the
human a real question on Salt mid-task.

Setup: pip install "saltapp[llamaindex]" plus an LLM integration (e.g.
`llama-index-llms-openai` and OPENAI_API_KEY), then:

    export SALT_API_KEY=...  APP_PUBLIC_KEY=...  APP_PRIVATE_KEY=...  PGP_PASSPHRASE=...
    python examples/llamaindex_agent.py

Message the agent "send a payment request, but ask me for the amount
first" -- it calls ask_human to get the amount, then request_payment.
"""

from __future__ import annotations

import asyncio
import os

from llama_index.core.agent.workflow import FunctionAgent
from llama_index.llms.openai import OpenAI

from saltapp.agent import Agent
from saltapp.integrations.llamaindex import SaltToolSpec

agent = Agent(
    host=os.environ.get("SALT_HOST", "https://saltapp.ai"),
    api_key=os.environ["SALT_API_KEY"],
    public_key=os.environ["APP_PUBLIC_KEY"],
    private_key=os.environ["APP_PRIVATE_KEY"],
    passphrase=os.environ["PGP_PASSPHRASE"],
)


@agent.on_message
async def on_message(ctx) -> None:
    tool_spec = SaltToolSpec(agent=agent, chat_id=ctx.chat_id)
    li_agent = FunctionAgent(
        llm=OpenAI(model="gpt-5.2"),
        tools=tool_spec.to_tool_list(),
        system_prompt="You help the human message, ask questions, and request payments on Salt. Be brief.",
    )
    response = await li_agent.run(user_msg=ctx.text)
    await ctx.reply(str(response))


async def main() -> None:
    await agent.ensure_identity()
    await agent.run_socket_async()


if __name__ == "__main__":
    asyncio.run(main())
