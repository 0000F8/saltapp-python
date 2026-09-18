"""Google ADK example: an LlmAgent whose `request_payment` tool needs
confirmation, resolved by asking on Salt instead of a console prompt.

Setup: pip install "saltapp[adk]" and Gemini access (e.g. GOOGLE_API_KEY),
then:

    export SALT_API_KEY=...  APP_PUBLIC_KEY=...  APP_PRIVATE_KEY=...  PGP_PASSPHRASE=...
    python examples/adk_confirmation.py

Message the agent "request $5 from bob for wallet w1"; it calls
request_payment, which pauses for your confirmation on Salt.
"""

from __future__ import annotations

import asyncio
import os

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

from saltapp.agent import Agent
from saltapp.integrations.adk import build_tools, resolve_confirmation_via_salt
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
    adk_agent = LlmAgent(
        name="salt_bot", model="gemini-2.5-flash", tools=build_tools(agent, ctx.chat_id),
        instruction="You help the human message and request payments on Salt. Be brief.",
    )
    runner = InMemoryRunner(agent=adk_agent)
    session = await runner.session_service.create_session(app_name=runner.app_name, user_id=ctx.sender_id)

    message = types.Content(role="user", parts=[types.Part(text=ctx.text)])
    reply_text = ""
    async for event in runner.run_async(user_id=ctx.sender_id, session_id=session.id, new_message=message):
        for call in event.get_function_calls() or []:
            if call.name == "adk_request_confirmation":
                response = await resolve_confirmation_via_salt(
                    call_id=call.id, hint=call.args.get("hint", ""), agent=agent, chat_id=ctx.chat_id,
                )
                resume = types.Content(role="user", parts=[types.Part(function_response=response)])
                async for event2 in runner.run_async(user_id=ctx.sender_id, session_id=session.id, new_message=resume):
                    if event2.content and event2.content.parts:
                        reply_text = event2.content.parts[0].text or reply_text
        if event.content and event.content.parts and event.content.parts[0].text:
            reply_text = event.content.parts[0].text

    await ctx.reply(reply_text or "Done.")


async def main() -> None:
    await agent.ensure_identity()
    await agent.run_socket_async(cursor_store=FileCursorStore("./data/adk_cursor.txt"))


if __name__ == "__main__":
    asyncio.run(main())
