# Pydantic AI integration: a `Toolset` exposing Salt's six tools, plus a
# deferred-tool approval handler that asks on Salt whenever a tool marked
# `requires_approval=True` needs a human decision.
#
# Needs the `pydantic_ai` extra: pip install "saltapp[pydantic_ai]"
#
# Verified against pydantic-ai's current docs (2026-09): `FunctionToolset`
# (`pydantic_ai.FunctionToolset`) groups plain functions into one toolset,
# passed to `Agent(toolsets=[...])`. The deferred-tool approval flow: a
# tool declared via `agent.tool_plain(requires_approval=True)` (or one that
# raises `ApprovalRequired` itself) makes a run whose `output_type`
# includes `DeferredToolRequests` return that type instead of finishing;
# `requests.approvals` lists the pending `ToolCallPart`s, and a
# `DeferredToolResults` (each entry either `True`/`False` or a
# `ToolDenied(message)`) fed back via `agent.run(...,
# deferred_tool_results=...)` resumes the run.
from __future__ import annotations

from typing import Any

try:
    from pydantic_ai import DeferredToolResults, ToolDenied
    from pydantic_ai.toolsets import FunctionToolset
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "saltapp.integrations.pydantic_ai requires the 'pydantic_ai' extra: "
        "pip install 'saltapp[pydantic_ai]'"
    ) from exc

from saltapp.agent import Agent
from saltapp.integrations._tools import SaltTools, build_plain_functions

__all__ = ["build_toolset", "resolve_deferred_approvals"]


def build_toolset(agent: Agent, chat_id: str) -> FunctionToolset:
    """A pydantic-ai `FunctionToolset` exposing Salt's six tools, bound to
    one chat:

        toolset = build_toolset(agent, ctx.chat_id)
        pai_agent = Agent("openai:gpt-5.2", toolsets=[toolset])
    """
    return FunctionToolset(tools=build_plain_functions(agent, chat_id))


async def resolve_deferred_approvals(
    requests: Any,
    *,
    agent: Agent,
    chat_id: str,
    timeout_seconds: float = 120.0,
) -> DeferredToolResults:
    """Given a `DeferredToolRequests` a pydantic-ai run paused on, asks a
    human on Salt to approve or deny each pending call in
    `requests.approvals` and returns the `DeferredToolResults` to resume
    the run with:

        result = pai_agent.run_sync(
            "Delete __init__.py", output_type=[str, DeferredToolRequests]
        )
        if isinstance(result.output, DeferredToolRequests):
            results = await resolve_deferred_approvals(
                result.output, agent=agent, chat_id=ctx.chat_id
            )
            result = pai_agent.run_sync(
                message_history=result.all_messages(), deferred_tool_results=results
            )
    """
    tools = SaltTools(agent, chat_id)
    results = DeferredToolResults()
    for call in requests.approvals:
        question = f'Approve tool call "{call.tool_name}"({call.args})?'
        answer = await tools.ask_human(question, options=["Approve", "Deny"], timeout_seconds=timeout_seconds)
        if answer.strip().lower() in ("approve", "yes", "y"):
            results.approvals[call.tool_call_id] = True
        else:
            results.approvals[call.tool_call_id] = ToolDenied(f"Denied by human on Salt: {answer}")
    return results
