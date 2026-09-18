# CrewAI integration: Salt tools as `BaseTool` subclasses, plus routing
# `Task(human_input=True)`'s feedback prompt through Salt via CrewAI's
# internal (undocumented) HumanInputProvider extension point.
#
# Needs the `crewai` extra: pip install "saltapp[crewai]"
#
# Verified against crewai 1.15.22 source (2026-09): `BaseTool`
# (`crewai.tools`) is a pydantic model with an abstract sync `_run(...)`
# and an optional async `_arun(...)` (default raises NotImplementedError;
# override it and callers use `await tool.arun(...)`).
#
# `Task.human_input=True`'s feedback prompt is NOT hardcoded to a console
# `input()` -- it is routed through `crewai.core.providers.human_input`'s
# `get_provider()`/`set_provider()`, and `crew_agent_executor.py` calls
# `get_provider().handle_feedback(...)` at the point human_input triggers.
# This hook is REAL and present in the released source, but it is NOT
# documented anywhere in CrewAI's public docs (only Enterprise webhooks and
# the Flow-only `@human_feedback` decorator are documented) -- treat it as
# an internal extension point CrewAI could change or remove without notice,
# not a stable contract. `install_salt_human_input()` raises a clear
# ImportError (rather than a confusing AttributeError) if a given CrewAI
# version doesn't have it, and points at `SaltAskHumanTool` as the
# documented alternative.
from __future__ import annotations

from typing import Any, Optional, Type

try:
    from crewai.tools import BaseTool
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "saltapp.integrations.crewai requires the 'crewai' extra: pip install 'saltapp[crewai]'"
    ) from exc

from pydantic import BaseModel, ConfigDict, PrivateAttr

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
    run_sync,
)

__all__ = [
    "SaltSendMessageTool",
    "SaltAskHumanTool",
    "SaltRequestPaymentTool",
    "SaltSendInvoiceTool",
    "SaltPostCardTool",
    "SaltGetPaymentStatusTool",
    "salt_tools",
    "SaltHumanInputProvider",
    "install_salt_human_input",
]


class _SaltBoundTool(BaseTool):
    """Shared plumbing: every Salt `BaseTool` below binds one `SaltTools`
    instance at construction, held as a pydantic private attribute so it
    doesn't leak into the tool's own args schema."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    _salt: SaltTools = PrivateAttr()

    def __init__(self, agent: Agent, chat_id: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._salt = SaltTools(agent, chat_id)


class SaltSendMessageTool(_SaltBoundTool):
    name: str = "send_message"
    description: str = TOOL_DESCRIPTIONS["send_message"]
    args_schema: Type[BaseModel] = SendMessageInput

    def _run(self, text: str) -> str:
        return run_sync(self._salt.send_message(text))

    async def _arun(self, text: str) -> str:
        return await self._salt.send_message(text)


class SaltAskHumanTool(_SaltBoundTool):
    name: str = "ask_human"
    description: str = TOOL_DESCRIPTIONS["ask_human"]
    args_schema: Type[BaseModel] = AskHumanInput

    def _run(self, question: str, options: Optional[list[str]] = None, timeout_seconds: float = 120.0) -> str:
        return run_sync(self._salt.ask_human(question, options=options, timeout_seconds=timeout_seconds))

    async def _arun(self, question: str, options: Optional[list[str]] = None, timeout_seconds: float = 120.0) -> str:
        return await self._salt.ask_human(question, options=options, timeout_seconds=timeout_seconds)


class SaltRequestPaymentTool(_SaltBoundTool):
    name: str = "request_payment"
    description: str = TOOL_DESCRIPTIONS["request_payment"]
    args_schema: Type[BaseModel] = RequestPaymentInput

    def _run(self, receiver_id: str, wallet_id: str, amount: str, message: Optional[str] = None) -> dict:
        return run_sync(self._salt.request_payment(receiver_id, wallet_id, amount, message))

    async def _arun(self, receiver_id: str, wallet_id: str, amount: str, message: Optional[str] = None) -> dict:
        return await self._salt.request_payment(receiver_id, wallet_id, amount, message)


class SaltSendInvoiceTool(_SaltBoundTool):
    name: str = "send_invoice"
    description: str = TOOL_DESCRIPTIONS["send_invoice"]
    args_schema: Type[BaseModel] = SendInvoiceInput

    def _run(self, receiver_id, wallet_id, amount, line_items, message=None, due_at=None) -> dict:
        items = [li.model_dump() if hasattr(li, "model_dump") else li for li in line_items]
        return run_sync(self._salt.send_invoice(receiver_id, wallet_id, amount, items, message, due_at))

    async def _arun(self, receiver_id, wallet_id, amount, line_items, message=None, due_at=None) -> dict:
        items = [li.model_dump() if hasattr(li, "model_dump") else li for li in line_items]
        return await self._salt.send_invoice(receiver_id, wallet_id, amount, items, message, due_at)


class SaltPostCardTool(_SaltBoundTool):
    name: str = "post_card"
    description: str = TOOL_DESCRIPTIONS["post_card"]
    args_schema: Type[BaseModel] = PostCardInput

    def _run(self, text: str, buttons: Optional[list[str]] = None) -> dict:
        return run_sync(self._salt.post_card(text, buttons))

    async def _arun(self, text: str, buttons: Optional[list[str]] = None) -> dict:
        return await self._salt.post_card(text, buttons)


class SaltGetPaymentStatusTool(_SaltBoundTool):
    name: str = "get_payment_status"
    description: str = TOOL_DESCRIPTIONS["get_payment_status"]
    args_schema: Type[BaseModel] = GetPaymentStatusInput

    def _run(self, transfer_id: str) -> dict:
        return run_sync(self._salt.get_payment_status(transfer_id))

    async def _arun(self, transfer_id: str) -> dict:
        return await self._salt.get_payment_status(transfer_id)


def salt_tools(agent: Agent, chat_id: str) -> list[BaseTool]:
    """All six Salt tools as CrewAI `BaseTool` instances bound to one chat --
    pass straight to a CrewAI `Agent(tools=salt_tools(agent, ctx.chat_id))`
    (CrewAI's own `Agent` class, not `saltapp.agent.Agent`)."""
    return [
        SaltSendMessageTool(agent, chat_id),
        SaltAskHumanTool(agent, chat_id),
        SaltRequestPaymentTool(agent, chat_id),
        SaltSendInvoiceTool(agent, chat_id),
        SaltPostCardTool(agent, chat_id),
        SaltGetPaymentStatusTool(agent, chat_id),
    ]


# ---- human_input=True bridge (undocumented CrewAI extension point) --------

class SaltHumanInputProvider:
    """Routes a CrewAI `Task(human_input=True)`'s feedback prompt through
    Salt instead of the console -- see this module's docstring for the
    "undocumented but real" caveat. Matches
    `crewai.core.providers.human_input.HumanInputProvider`'s Protocol
    shape (`handle_feedback`, `handle_feedback_async`) by duck typing
    rather than importing it, so this class itself doesn't hard-depend on
    that module existing."""

    def __init__(self, agent: Agent, chat_id: str) -> None:
        self._salt = SaltTools(agent, chat_id)

    def handle_feedback(self, formatted_answer: Any, context: Any = None) -> str:
        return run_sync(self._ask(formatted_answer))

    async def handle_feedback_async(self, formatted_answer: Any, context: Any = None) -> str:
        return await self._ask(formatted_answer)

    async def _ask(self, formatted_answer: Any) -> str:
        summary = getattr(formatted_answer, "output", None) or str(formatted_answer)
        return await self._salt.ask_human(f'Review this and reply with feedback (or "looks good"):\n\n{summary}')


def install_salt_human_input(agent: Agent, chat_id: str) -> SaltHumanInputProvider:
    """Sets CrewAI's process-wide (ContextVar-scoped) human-input provider
    to one that asks on Salt. Call this before `crew.kickoff()` for any
    `Task(human_input=True)` to be reviewed on Salt instead of a console
    prompt. Raises `ImportError` with a clear message -- pointing at
    `SaltAskHumanTool` as the documented alternative -- if the installed
    CrewAI version doesn't expose the extension point this was verified
    against (crewai 1.15.22)."""
    try:
        from crewai.core.providers.human_input import set_provider
    except ImportError as exc:  # pragma: no cover - depends on CrewAI's internals
        raise ImportError(
            "This CrewAI version doesn't expose "
            "crewai.core.providers.human_input.set_provider -- the "
            "human_input=True bridge was built against crewai 1.15.22's "
            "(undocumented) extension point and may not exist in your "
            "installed version. Task.human_input will fall back to a "
            "console prompt; use saltapp.integrations.crewai.SaltAskHumanTool "
            "as a documented alternative instead."
        ) from exc
    provider = SaltHumanInputProvider(agent, chat_id)
    set_provider(provider)
    return provider
