# OpenAI Agents SDK integration: Salt's six tools as `@tool`-decorated
# function tools, plus a resolver for the SDK's `needs_approval` human-in-
# the-loop hook.
#
# Needs the `openai_agents` extra: pip install "saltapp[openai_agents]"
#
# Verified against the `openai-agents` package (0.22.3, 2026-09): current
# docs lead with `from agents.decorators import tool` (the classic
# `from agents import function_tool` decorator still works and is exported,
# but every current example uses `@tool`); both build a tool's JSON schema
# from type hints + docstring. `@tool(needs_approval=True)` (or a callable
# `async def check(ctx, params, call_id) -> bool`) marks a tool as gated;
# `Runner.run(...)`'s result carries `.interruptions` (a list of
# `ToolApprovalItem`) when a gated tool was called. Resume: convert to a
# `RunState` via `result.to_state()`, call `state.approve(interruption)` /
# `state.reject(interruption, rejection_message=...)` for each pending
# item, then `await Runner.run(agent, state)` again -- repeating until
# `result.interruptions` is empty.
from __future__ import annotations

from typing import Any, Optional

try:
    from agents.decorators import tool
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "saltapp.integrations.openai_agents requires the 'openai_agents' extra: "
        "pip install 'saltapp[openai_agents]'"
    ) from exc

from saltapp.agent import Agent
from saltapp.integrations._tools import SaltTools, build_plain_functions

__all__ = ["build_tools", "MONEY_MOVING_TOOL_NAMES", "resolve_interruptions"]

# request_payment and send_invoice move real money; every other tool here
# either only sends a message/card or only reads a payment's status.
MONEY_MOVING_TOOL_NAMES = ("request_payment", "send_invoice")


def build_tools(agent: Agent, chat_id: str) -> list[Any]:
    """Salt's six tools as OpenAI Agents SDK function tools, bound to one
    chat. `request_payment` and `send_invoice` are wrapped with
    `needs_approval=True` (they move real money); resolve that with
    `resolve_interruptions()` below rather than assuming approval:

        my_agent = agents.Agent(name="salt-bot", tools=build_tools(salt_agent, ctx.chat_id))
    """
    fns = build_plain_functions(agent, chat_id)
    return [
        tool(fn, needs_approval=(fn.__name__ in MONEY_MOVING_TOOL_NAMES)) if fn.__name__ in MONEY_MOVING_TOOL_NAMES else tool(fn)
        for fn in fns
    ]


async def resolve_interruptions(result: Any, *, agent: Agent, chat_id: str, timeout_seconds: float = 120.0) -> Any:
    """Given a `Runner.run(...)` result carrying pending `.interruptions`
    (`ToolApprovalItem`s from a `needs_approval=True` tool), asks on Salt
    for each one's approval and returns the resumed `RunState` (pass it
    straight back into `Runner.run(agent, state)`; repeat while
    `.interruptions` is still non-empty):

        result = await Runner.run(my_agent, "Send $5 to @dan for coffee.")
        while result.interruptions:
            state = await resolve_interruptions(result, agent=salt_agent, chat_id=ctx.chat_id)
            result = await Runner.run(my_agent, state)
    """
    tools = SaltTools(agent, chat_id)
    state = result.to_state()
    for interruption in result.interruptions:
        tool_name = getattr(interruption, "name", None) or getattr(interruption, "tool_name", "a tool")
        question = f'Approve tool call "{tool_name}"?'
        answer = await tools.ask_human(question, options=["Approve", "Reject"], timeout_seconds=timeout_seconds)
        if answer.strip().lower().startswith("approve"):
            state.approve(interruption)
        else:
            state.reject(interruption, rejection_message=f"Denied by human on Salt: {answer}")
    return state
