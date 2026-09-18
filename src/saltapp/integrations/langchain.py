# LangChain / LangGraph integration: `SaltToolkit` (a `BaseToolkit`
# exposing the six shared Salt tools) plus a LangGraph `interrupt()` bridge
# so a graph can pause, ask a human on Salt (buttons or free text), and
# resume with `Command(resume=answer)` the moment they answer.
#
# Needs the `langchain` extra: pip install "saltapp[langchain]"
# (langchain-core for the toolkit; langgraph for the interrupt bridge --
# only imported lazily, inside the functions that need it, so importing
# this module doesn't require langgraph if you only want the toolkit).
#
# Verified against langchain-core 1.6.x / langgraph 1.2.x docs (2026-09):
# `BaseToolkit` lives at `langchain_core.tools.base.BaseToolkit` (also
# re-exported from `langchain_core.tools`), is a pydantic model with one
# abstract method `get_tools() -> list[BaseTool]`. `interrupt`/`Command`
# live in `langgraph.types`; `MemorySaver` was renamed `InMemorySaver`
# (`langgraph.checkpoint.memory`) -- a graph using `interrupt()` MUST be
# compiled with a checkpointer or it raises.
from __future__ import annotations

from typing import Any, Optional

try:
    from langchain_core.tools import BaseTool, BaseToolkit, StructuredTool
    from pydantic import ConfigDict
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "saltapp.integrations.langchain requires the 'langchain' extra: "
        "pip install 'saltapp[langchain]'"
    ) from exc

from saltapp.agent import Agent
from saltapp.integrations._tools import (
    TOOL_DESCRIPTIONS,
    AskHumanInput,
    GetPaymentStatusInput,
    PostCardInput,
    RequestPaymentInput,
    SaltTools,
    SendInvoiceInput,
    SendMessageInput,
)

__all__ = ["SaltToolkit", "ask_via_interrupt", "SaltInterruptRunner"]


class SaltToolkit(BaseToolkit):
    """A LangChain `BaseToolkit` exposing Salt's six tools
    (`send_message`, `ask_human`, `request_payment`, `send_invoice`,
    `post_card`, `get_payment_status`), all scoped to one chat:

        toolkit = SaltToolkit(agent=agent, chat_id=ctx.chat_id)
        tools = toolkit.get_tools()
        model_with_tools = model.bind_tools(tools)
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    agent: Agent
    chat_id: str

    def get_tools(self) -> list[BaseTool]:
        tools = SaltTools(self.agent, self.chat_id)
        return [
            StructuredTool.from_function(
                name="send_message",
                description=TOOL_DESCRIPTIONS["send_message"],
                args_schema=SendMessageInput,
                coroutine=tools.send_message,
                func=None,
            ),
            StructuredTool.from_function(
                name="ask_human",
                description=TOOL_DESCRIPTIONS["ask_human"],
                args_schema=AskHumanInput,
                coroutine=lambda question, options=None, timeout_seconds=120.0: tools.ask_human(
                    question, options=options, timeout_seconds=timeout_seconds
                ),
                func=None,
            ),
            StructuredTool.from_function(
                name="request_payment",
                description=TOOL_DESCRIPTIONS["request_payment"],
                args_schema=RequestPaymentInput,
                coroutine=lambda receiver_id, wallet_id, amount, message=None: tools.request_payment(
                    receiver_id, wallet_id, amount, message
                ),
                func=None,
            ),
            StructuredTool.from_function(
                name="send_invoice",
                description=TOOL_DESCRIPTIONS["send_invoice"],
                args_schema=SendInvoiceInput,
                coroutine=lambda receiver_id, wallet_id, amount, line_items, message=None, due_at=None: tools.send_invoice(
                    receiver_id,
                    wallet_id,
                    amount,
                    [li.model_dump() if hasattr(li, "model_dump") else li for li in line_items],
                    message,
                    due_at,
                ),
                func=None,
            ),
            StructuredTool.from_function(
                name="post_card",
                description=TOOL_DESCRIPTIONS["post_card"],
                args_schema=PostCardInput,
                coroutine=lambda text, buttons=None: tools.post_card(text, buttons),
                func=None,
            ),
            StructuredTool.from_function(
                name="get_payment_status",
                description=TOOL_DESCRIPTIONS["get_payment_status"],
                args_schema=GetPaymentStatusInput,
                coroutine=lambda transfer_id: tools.get_payment_status(transfer_id),
                func=None,
            ),
        ]


# ---- LangGraph interrupt()/Command(resume=...) bridge ----------------------

def ask_via_interrupt(question: str, *, options: Optional[list[str]] = None) -> Any:
    """Call from inside a LangGraph node or tool to pause the graph and ask
    a human. Requires the graph to be compiled with a checkpointer (any
    `interrupt()` call does). Returns whatever value the resuming
    `Command(resume=...)` carried -- when driven by `SaltInterruptRunner`
    below, that's the human's answer as plain text.

        @tool
        def request_approval(action: str) -> str:
            return ask_via_interrupt(f"Approve: {action}?", options=["Yes", "No"])
    """
    from langgraph.types import interrupt

    payload: dict[str, Any] = {"type": "salt_ask", "question": question}
    if options:
        payload["options"] = list(options)
    return interrupt(payload)


class SaltInterruptRunner:
    """Drives a compiled LangGraph graph whose nodes call
    `ask_via_interrupt()`: runs it, and whenever it pauses on an interrupt,
    posts the interrupt's question to Salt (as buttons if it carried
    `options`), waits for the human's answer, then resumes the graph with
    `Command(resume=answer)` -- repeating until the graph finishes.

    The graph MUST be compiled with a checkpointer and invoked against a
    stable `thread_id` (LangGraph resumes a paused run by thread, not by
    return value):

        from langgraph.checkpoint.memory import InMemorySaver
        graph = builder.compile(checkpointer=InMemorySaver())
        runner = SaltInterruptRunner(graph, agent=agent, chat_id=ctx.chat_id)
        result = await runner.arun({"messages": [...]}, thread_id=ctx.chat_id)
    """

    def __init__(self, graph: Any, *, agent: Agent, chat_id: str) -> None:
        self.graph = graph
        self._tools = SaltTools(agent, chat_id)

    async def arun(self, input_: Any, *, thread_id: str, timeout_seconds: float = 120.0) -> Any:
        from langgraph.types import Command

        config = {"configurable": {"thread_id": thread_id}}
        result = await self.graph.ainvoke(input_, config=config)
        while isinstance(result, dict) and result.get("__interrupt__"):
            interrupt_obj = result["__interrupt__"][0]
            payload = getattr(interrupt_obj, "value", interrupt_obj)
            if isinstance(payload, dict):
                question = str(payload.get("question", ""))
                options = payload.get("options")
            else:
                question, options = str(payload), None
            answer = await self._tools.ask_human(question, options=options, timeout_seconds=timeout_seconds)
            result = await self.graph.ainvoke(Command(resume=answer), config=config)
        return result

    def run(self, input_: Any, *, thread_id: str, timeout_seconds: float = 120.0) -> Any:
        """Sync convenience wrapper around `arun()` (fresh event loop per call --
        see `saltapp.integrations._tools.run_sync`'s docstring for the tradeoff)."""
        import asyncio

        return asyncio.run(self.arun(input_, thread_id=thread_id, timeout_seconds=timeout_seconds))
