"""CAMEL example: a ChatAgent using `SaltHumanToolkit` as a drop-in swap
for CAMEL's own `HumanToolkit` -- same `ask_human_via_console`/
`send_message_to_user` tool names, routed to Salt instead of the console.

Setup: pip install "saltapp[camel]" and an LLM CAMEL can use (e.g.
OPENAI_API_KEY), then:

    export SALT_API_KEY=...  APP_PUBLIC_KEY=...  APP_PRIVATE_KEY=...  PGP_PASSPHRASE=...
    python examples/camel_human_toolkit.py

Message the agent "send a payment request, but ask me for the amount
first" -- it calls ask_human_via_console (answered on Salt), then
request_payment.
"""

from __future__ import annotations

import asyncio
import os

from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.models import ModelFactory
from camel.types import ModelPlatformType, ModelType

from saltapp.agent import Agent
from saltapp.integrations.camel import SaltHumanToolkit
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
    toolkit = SaltHumanToolkit(agent=agent, chat_id=ctx.chat_id)
    model = ModelFactory.create(model_platform=ModelPlatformType.OPENAI, model_type=ModelType.GPT_5_2)
    chat_agent = ChatAgent(
        system_message=BaseMessage.make_assistant_message(
            role_name="Salt bot",
            content="You help the human message, ask questions, and request payments on Salt. Be brief.",
        ),
        model=model,
        tools=[*toolkit.get_tools()],
    )
    response = await asyncio.to_thread(chat_agent.step, ctx.text)
    await ctx.reply(response.msgs[0].content if response.msgs else "Done.")


async def main() -> None:
    await agent.ensure_identity()
    await agent.run_socket_async(cursor_store=FileCursorStore("./data/camel_cursor.txt"))


if __name__ == "__main__":
    asyncio.run(main())
