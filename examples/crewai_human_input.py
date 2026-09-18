"""CrewAI example: a one-agent crew whose task needs `human_input=True`
review, routed through Salt instead of a console prompt -- plus the
documented `SaltAskHumanTool` any crew can call directly.

Setup: pip install "saltapp[crewai]" and an LLM CrewAI can use (e.g.
OPENAI_API_KEY for the default model), then:

    export SALT_API_KEY=...  APP_PUBLIC_KEY=...  APP_PRIVATE_KEY=...  PGP_PASSPHRASE=...
    python examples/crewai_human_input.py

Message the agent anything; it drafts a one-line reply, then asks you (on
Salt) to approve it before actually sending.
"""

from __future__ import annotations

import asyncio
import os

from crewai import Agent as CrewAgent, Crew, Task

from saltapp.agent import Agent
from saltapp.integrations.crewai import install_salt_human_input, salt_tools

agent = Agent(
    host=os.environ.get("SALT_HOST", "https://saltapp.ai"),
    api_key=os.environ["SALT_API_KEY"],
    public_key=os.environ["APP_PUBLIC_KEY"],
    private_key=os.environ["APP_PRIVATE_KEY"],
    passphrase=os.environ["PGP_PASSPHRASE"],
)


@agent.on_message
async def on_message(ctx) -> None:
    # install_salt_human_input() is the undocumented-but-real CrewAI hook
    # (see saltapp.integrations.crewai's module docstring) -- every
    # Task(human_input=True) review in this crew now asks on Salt.
    install_salt_human_input(agent, ctx.chat_id)

    writer = CrewAgent(
        role="Reply drafter",
        goal="Draft a short, friendly one-line reply to the human's message.",
        backstory="You draft replies for a Salt agent; a human reviews every one before it sends.",
        tools=salt_tools(agent, ctx.chat_id),
    )
    draft_task = Task(
        description=f'Draft a one-line reply to: "{ctx.text}"',
        expected_output="One short sentence.",
        agent=writer,
        human_input=True,  # -> reviewed on Salt via install_salt_human_input above
    )
    crew = Crew(agents=[writer], tasks=[draft_task])
    result = await asyncio.to_thread(crew.kickoff)
    await ctx.reply(str(result))


async def main() -> None:
    await agent.ensure_identity()
    await agent.run_socket_async()


if __name__ == "__main__":
    asyncio.run(main())
