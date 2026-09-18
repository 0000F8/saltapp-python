"""Flagship example: a LangGraph graph that pauses mid-run, asks a human a
question ON SALT (with tappable buttons), and resumes the graph the moment
they answer -- no console, no web inbox, just the chat they're already in.

Setup: pip install "saltapp[langchain]" and register a Salt agent (see the
package README's "Quickstart: register an agent"), then:

    export SALT_API_KEY=...  APP_PUBLIC_KEY=...  APP_PRIVATE_KEY=...  PGP_PASSPHRASE=...
    python examples/langgraph_interrupt.py

Runs entirely in socket mode -- no public URL needed. Message the agent
"deploy" from the Salt app; it will ask you which environment, as buttons,
and reply once you tap one.
"""

from __future__ import annotations

import asyncio
import os
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from saltapp.agent import Agent
from saltapp.integrations.langchain import SaltInterruptRunner, ask_via_interrupt
from saltapp.socket import FileCursorStore


class DeployState(TypedDict):
    request: str
    environment: str


def ask_environment(state: DeployState) -> dict:
    # Pauses the WHOLE graph here -- see SaltInterruptRunner.arun below,
    # which is what actually posts this to Salt and resumes on the answer.
    answer = ask_via_interrupt("Which environment should I deploy to?", options=["staging", "production"])
    return {"environment": answer}


def confirm(state: DeployState) -> dict:
    return {"environment": state["environment"]}


builder = StateGraph(DeployState)
builder.add_node("ask_environment", ask_environment)
builder.add_node("confirm", confirm)
builder.add_edge(START, "ask_environment")
builder.add_edge("ask_environment", "confirm")
builder.add_edge("confirm", END)

# interrupt() requires a checkpointer -- InMemorySaver is fine for a single
# process; swap in a real one (Postgres, SQLite, ...) to survive a restart
# mid-question.
graph = builder.compile(checkpointer=InMemorySaver())

agent = Agent(
    host=os.environ.get("SALT_HOST", "https://saltapp.ai"),
    api_key=os.environ["SALT_API_KEY"],
    public_key=os.environ["APP_PUBLIC_KEY"],
    private_key=os.environ["APP_PRIVATE_KEY"],
    passphrase=os.environ["PGP_PASSPHRASE"],
)


@agent.on_message
async def on_message(ctx) -> None:
    if "deploy" not in ctx.text.lower():
        await ctx.reply("Say \"deploy\" and I'll walk through it.")
        return

    runner = SaltInterruptRunner(graph, agent=agent, chat_id=ctx.chat_id)
    # thread_id scopes the paused run -- one deploy conversation per chat.
    result = await runner.arun({"request": ctx.text, "environment": ""}, thread_id=ctx.chat_id)
    await ctx.reply(f"Deploying to {result['environment']}. \U0001f680")


async def main() -> None:
    await agent.ensure_identity()
    await agent.run_socket_async(cursor_store=FileCursorStore("./data/langgraph_cursor.txt"))


if __name__ == "__main__":
    asyncio.run(main())
