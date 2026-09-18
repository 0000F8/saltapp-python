# Google Agent Development Kit (ADK) integration: Salt's six tools as
# `FunctionTool`s (with `request_payment`/`send_invoice` gated by ADK's
# `require_confirmation=True`), plus a resolver that answers a pending
# `ToolConfirmation` by asking on Salt.
#
# Needs the `adk` extra: pip install "saltapp[adk]"
#
# Verified against google-adk 2.9.x docs (2026-09): plain functions passed
# to `tools=[...]` on an `LlmAgent` are auto-wrapped into
# `google.adk.tools.FunctionTool`; ADK infers a tool's schema from its
# type hints + docstring. `FunctionTool(func, require_confirmation=True)`
# pauses the run for a plain yes/no; the run loop resumes it by returning
# a `google.genai.types.FunctionResponse` named `"adk_request_confirmation"`
# (matching the pending call's id) with `response={"confirmed": bool}` fed
# back as `new_message` to `runner.run_async(...)`. A tool can also call
# `tool_context.request_confirmation(hint=..., payload=...)` itself for a
# richer payload than plain yes/no -- not used here since Salt's six tools
# are plain REST calls with nothing tool-specific to negotiate.
from __future__ import annotations

from typing import Any, Optional

try:
    from google.adk.tools import FunctionTool
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError("saltapp.integrations.adk requires the 'adk' extra: pip install 'saltapp[adk]'") from exc

from saltapp.agent import Agent
from saltapp.integrations._tools import SaltTools, build_plain_functions

__all__ = ["build_tools", "build_confirmation_response", "resolve_confirmation_via_salt"]

CONFIRMATION_TOOL_NAME = "adk_request_confirmation"
_CONFIRMATION_REQUIRED = ("request_payment", "send_invoice")


def build_tools(agent: Agent, chat_id: str) -> list[FunctionTool]:
    """Salt's six tools as ADK `FunctionTool`s, bound to one chat.
    `request_payment` and `send_invoice` are wrapped with
    `require_confirmation=True` (they move real money); resolve that pause
    with `resolve_confirmation_via_salt()` below rather than a console
    prompt:

        agent = google.adk.agents.LlmAgent(
            model="gemini-2.5-flash", tools=build_tools(salt_agent, ctx.chat_id),
        )
    """
    fns = build_plain_functions(agent, chat_id)
    return [FunctionTool(fn, require_confirmation=fn.__name__ in _CONFIRMATION_REQUIRED) for fn in fns]


def build_confirmation_response(call_id: str, confirmed: bool) -> Any:
    """The `google.genai.types.FunctionResponse` ADK expects back to
    resume a paused `require_confirmation=True` tool call, given the
    pending call's id and the human's yes/no decision."""
    from google.genai import types

    return types.FunctionResponse(id=call_id, name=CONFIRMATION_TOOL_NAME, response={"confirmed": confirmed})


async def resolve_confirmation_via_salt(
    *, call_id: str, hint: str, agent: Agent, chat_id: str, timeout_seconds: float = 120.0
) -> Any:
    """Asks on Salt for a yes/no ("Approve" / "Reject") and returns the
    `FunctionResponse` to resume the paused tool call with:

        async for event in runner.run_async(...):
            call = find_pending_confirmation(event)  # your own event inspection
            if call is not None:
                response = await resolve_confirmation_via_salt(
                    call_id=call.id, hint=call.args.get("hint", ""), agent=salt_agent, chat_id=ctx.chat_id,
                )
                async for event in runner.run_async(new_message=response, ...):
                    ...
    """
    tools = SaltTools(agent, chat_id)
    question = hint or "An agent tool needs your confirmation. Approve?"
    answer = await tools.ask_human(question, options=["Approve", "Reject"], timeout_seconds=timeout_seconds)
    confirmed = answer.strip().lower().startswith("approve")
    return build_confirmation_response(call_id, confirmed)
