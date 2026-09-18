"""Agno example: an agent whose `request_payment` tool requires
confirmation, resolved by asking on Salt (never a console prompt).

Setup: pip install "saltapp[agno]" and an LLM Agno can use (e.g.
OPENAI_API_KEY), then:

    export SALT_API_KEY=...  APP_PUBLIC_KEY=...  APP_PRIVATE_KEY=...  PGP_PASSPHRASE=...
    python examples/agno_confirmation.py

Message the agent "request $5 from bob for wallet w1"; it calls
request_payment, which pauses for your confirmation on Salt before it
actually posts the payment request.
"""

from __future__ import annotations

import asyncio
import os

from agno.agent import Agent as AgnoAgent
from agno.db.sqlite import SqliteDb
from agno.models.openai import OpenAIChat

from saltapp.agent import Agent
from saltapp.integrations.agno import SaltToolkit, resolve_agno_run

agent = Agent(
    host=os.environ.get("SALT_HOST", "https://saltapp.ai"),
    api_key=os.environ["SALT_API_KEY"],
    public_key=os.environ["APP_PUBLIC_KEY"],
    private_key=os.environ["APP_PRIVATE_KEY"],
    passphrase=os.environ["PGP_PASSPHRASE"],
)

# continue_run() resumes a paused run by run_id -- Agno needs a db to find
# it again.
db = SqliteDb(db_file="./data/agno_runs.db")


@agent.on_message
async def on_message(ctx) -> None:
    agno_agent = AgnoAgent(
        model=OpenAIChat(id="gpt-5.2"),
        tools=[SaltToolkit(agent=agent, chat_id=ctx.chat_id)],
        db=db,
        instructions="You help the human message and request payments on Salt. Be brief.",
    )

    run_response = await asyncio.to_thread(agno_agent.run, ctx.text)
    run_response = await asyncio.to_thread(
        resolve_agno_run, agno_agent, run_response, agent=agent, chat_id=ctx.chat_id
    )
    await ctx.reply(run_response.content or "Done.")


async def main() -> None:
    await agent.ensure_identity()
    await agent.run_socket_async()


if __name__ == "__main__":
    asyncio.run(main())
