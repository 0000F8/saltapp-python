# Shared Salt tool business logic for every `saltapp.integrations.<framework>`
# module -- the six primitives the task brief names: send_message,
# ask_human, request_payment, send_invoice, post_card, get_payment_status.
# Each framework file wraps THESE, never re-implements them, so a framework
# file stays a thin shell (a schema + a call into `SaltTools`).
#
# Every tool below is scoped to ONE chat_id, fixed at construction time (the
# chat whatever `Agent` handler is currently running is answering in) -- an
# LLM is never asked to supply a chat_id itself. That's a deliberate
# narrowing: these are "act in the conversation I'm already in" tools, not
# "message chat <id> of your choosing" tools.
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, Field

from saltapp import cards as cards_module
from saltapp.agent import Agent, AskTimeout, tool_context  # noqa: F401 -- AskTimeout re-exported for framework modules

__all__ = [
    "SaltTools",
    "TOOL_NAMES",
    "TOOL_DESCRIPTIONS",
    "SendMessageInput",
    "AskHumanInput",
    "RequestPaymentInput",
    "LineItemInput",
    "SendInvoiceInput",
    "PostCardInput",
    "GetPaymentStatusInput",
    "build_plain_functions",
    "run_sync",
]

TOOL_NAMES = (
    "send_message",
    "ask_human",
    "request_payment",
    "send_invoice",
    "post_card",
    "get_payment_status",
)

TOOL_DESCRIPTIONS = {
    "send_message": "Send a plain-text message to the human in this Salt chat.",
    "ask_human": (
        "Ask the human a question in this Salt chat and wait for their reply -- "
        "a tapped button (if options are given) or typed text, whichever comes "
        "first. Returns the answer as plain text."
    ),
    "request_payment": (
        "Request a payment from the human on Salt's payment rail (a real "
        "in-chat payment-request bubble)."
    ),
    "send_invoice": "Send an itemized invoice to the human on Salt's payment rail.",
    "post_card": "Post an interactive card (a message with optional buttons) into the Salt chat.",
    "get_payment_status": "Look up a Salt payment's current status by transfer id.",
}


# ---- pydantic input schemas -----------------------------------------------
# For frameworks whose tool primitive wants an explicit args model
# (LangChain's StructuredTool.args_schema, CrewAI's BaseTool.args_schema,
# Pydantic AI's tools, which are pydantic-native already). Frameworks that
# build a schema by introspecting a plain function's type hints + docstring
# instead (ADK, the OpenAI Agents SDK, LlamaIndex's FunctionTool, smolagents,
# CAMEL, Agno) use `build_plain_functions()` below instead of these models.

class SendMessageInput(BaseModel):
    text: str = Field(description="The plain-text message to send to the human in this Salt chat.")


class AskHumanInput(BaseModel):
    question: str = Field(description="The question to ask the human.")
    options: Optional[list[str]] = Field(
        default=None,
        description="Short button labels to offer, e.g. ['Yes', 'No']. Omit for a free-text-only question.",
    )
    timeout_seconds: float = Field(default=120.0, description="How long to wait for an answer before giving up.")


class RequestPaymentInput(BaseModel):
    receiver_id: str = Field(description="Salt user id who should pay.")
    wallet_id: str = Field(description="Salt wallet id the payment should land in.")
    amount: str = Field(description='Human-decimal amount as a string, e.g. "4.50" -- never base units.')
    message: Optional[str] = Field(default=None, description="Optional note shown with the request.")


class LineItemInput(BaseModel):
    name: str = Field(description="What this line item is.")
    qty: float = Field(default=1, description="Quantity.")
    unit_price: str = Field(description='Human-decimal unit price, e.g. "4.50".')
    subtotal: str = Field(description="qty * unit_price, as a human-decimal string.")


class SendInvoiceInput(BaseModel):
    receiver_id: str = Field(description="Salt user id who should pay.")
    wallet_id: str = Field(description="Salt wallet id the payment should land in.")
    amount: str = Field(description="Total human-decimal amount; must equal the sum of the line items' subtotals.")
    line_items: list[LineItemInput] = Field(description="The invoice's itemized lines.")
    message: Optional[str] = Field(default=None, description="Optional note.")
    due_at: Optional[str] = Field(default=None, description="Optional ISO8601 due date.")


class PostCardInput(BaseModel):
    text: str = Field(description="The card's plain-text summary (also used as the message preview).")
    buttons: Optional[list[str]] = Field(
        default=None, description="Short button labels, e.g. ['Approve', 'Reject']. Omit for a text-only card."
    )


class GetPaymentStatusInput(BaseModel):
    transfer_id: str = Field(description="The transfer id to check.")


# ---- shared implementation --------------------------------------------------

@dataclass
class SaltTools:
    """Salt actions bound to one Agent identity and one chat. Every
    `saltapp.integrations.<framework>` module wraps these six async methods
    as that framework's own tool shape -- this dataclass is the one place
    the business logic (and the `ask_human` HITL bridge onto `Agent.ask()`)
    lives."""

    agent: Agent
    chat_id: str

    def __post_init__(self) -> None:
        self._ctx = tool_context(self.agent, self.chat_id)

    async def send_message(self, text: str) -> str:
        await self.agent.client.send_message(self.agent.identity, self.chat_id, text)
        return "Message sent."

    async def ask_human(
        self, question: str, options: Optional[list[str]] = None, timeout_seconds: float = 120.0
    ) -> str:
        """Post `question` (as buttons if `options` is given) and return
        whichever answers it first -- a tapped option's label, or typed
        text. Raises `saltapp.AskTimeout` if nothing answers in time; a
        framework's HITL bridge (see each integration module) is what
        turns that into ITS OWN pause/resume shape."""
        result = await self._ctx.ask(question, options=options, free_text=True, timeout=timeout_seconds)
        return result.value if result.kind == "option" else (result.text or "")

    async def request_payment(
        self, receiver_id: str, wallet_id: str, amount: str, message: Optional[str] = None
    ) -> dict[str, Any]:
        return await self._ctx.request_payment(receiver_id=receiver_id, wallet_id=wallet_id, amount=amount, message=message)

    async def send_invoice(
        self,
        receiver_id: str,
        wallet_id: str,
        amount: str,
        line_items: list[dict[str, Any]],
        message: Optional[str] = None,
        due_at: Optional[str] = None,
    ) -> dict[str, Any]:
        return await self.agent.client.create_invoice(
            self.agent.identity.api_key,
            chat_id=self.chat_id,
            receiver_id=receiver_id,
            wallet_id=wallet_id,
            amount=amount,
            line_items=line_items,
            message=message,
            due_at=due_at,
        )

    async def post_card(self, text: str, buttons: Optional[list[str]] = None) -> dict[str, Any]:
        blocks_list: list[dict[str, Any]] = [cards_module.section(text=text)]
        if buttons:
            btns = [cards_module.button(f"opt_{i}", label) for i, label in enumerate(buttons)]
            blocks_list.append(cards_module.actions(btns))
        return await self._ctx.post_card(blocks_list, text)

    async def get_payment_status(self, transfer_id: str) -> dict[str, Any]:
        return await self.agent.client.get_transfer(self.agent.identity.api_key, transfer_id)


def run_sync(coro: Awaitable[Any]) -> Any:
    """Bridge for purely-synchronous framework tool hooks (CrewAI's
    `BaseTool._run`, smolagents' sync `forward`). Uses a fresh event loop
    via `asyncio.run()` -- correct but, like `Agent.dispatch_sync`, not
    cheap; fine for a reference/hobby-scale tool call. Raises RuntimeError
    with a pointer to the async method if called from inside a loop that's
    already running (await the `SaltTools` method directly there instead)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(
        "run_sync() was called from inside a running event loop -- await the "
        "async SaltTools method directly instead of a sync tool wrapper here."
    )


def build_plain_functions(agent: Agent, chat_id: str) -> list[Callable[..., Awaitable[Any]]]:
    """The six tools as plain async python functions bound to one chat,
    with type hints + Google-style docstrings -- for frameworks that build
    a tool's schema by introspecting a function signature: ADK's
    `FunctionTool`, the OpenAI Agents SDK's `@tool`, LlamaIndex's
    `FunctionTool.from_defaults`, smolagents' `@tool`, CAMEL's
    `FunctionTool.from_function`, Agno's `@tool`."""
    tools = SaltTools(agent, chat_id)

    async def send_message(text: str) -> str:
        """Send a plain-text message to the human in this Salt chat.

        Args:
            text: The message to send.
        """
        return await tools.send_message(text)

    async def ask_human(question: str, options: Optional[list[str]] = None, timeout_seconds: float = 120.0) -> str:
        """Ask the human a question in this Salt chat and wait for the answer.

        Posts the question (as tappable buttons if `options` is given) and
        waits for either a button tap or a typed reply, whichever comes
        first.

        Args:
            question: The question to ask.
            options: Optional short button labels to offer, e.g. ["Yes", "No"].
            timeout_seconds: How long to wait before giving up.
        """
        return await tools.ask_human(question, options=options, timeout_seconds=timeout_seconds)

    async def request_payment(
        receiver_id: str, wallet_id: str, amount: str, message: Optional[str] = None
    ) -> dict[str, Any]:
        """Request a payment from the human, as a real Salt payment-request bubble.

        Args:
            receiver_id: Salt user id who should pay.
            wallet_id: Salt wallet id the payment should land in.
            amount: Human-decimal amount, e.g. "4.50" (never base units).
            message: Optional note shown with the request.
        """
        return await tools.request_payment(receiver_id, wallet_id, amount, message)

    async def send_invoice(
        receiver_id: str,
        wallet_id: str,
        amount: str,
        line_items: list[LineItemInput],
        message: Optional[str] = None,
        due_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Send an itemized invoice to the human on Salt's payment rail.

        Args:
            receiver_id: Salt user id who should pay.
            wallet_id: Salt wallet id the payment should land in.
            amount: Total human-decimal amount, e.g. "9.00".
            line_items: each item's subtotal = qty * unit_price, and the sum
                of every subtotal must equal amount.
            message: Optional note.
            due_at: Optional ISO8601 due date.
        """
        # `line_items` arrives as validated `LineItemInput` pydantic models
        # when a framework builds its schema from these type hints (a
        # `list[dict]` parameter is what tripped the OpenAI Agents SDK's
        # strict-schema check: additionalProperties on a bare dict isn't
        # allowed there) -- flatten back to plain dicts for the REST call.
        items = [li.model_dump() if hasattr(li, "model_dump") else li for li in line_items]
        return await tools.send_invoice(receiver_id, wallet_id, amount, items, message, due_at)

    async def post_card(text: str, buttons: Optional[list[str]] = None) -> dict[str, Any]:
        """Post an interactive card into the Salt chat.

        Args:
            text: The card's plain-text summary.
            buttons: Optional short button labels, e.g. ["Approve", "Reject"].
        """
        return await tools.post_card(text, buttons)

    async def get_payment_status(transfer_id: str) -> dict[str, Any]:
        """Look up a Salt payment's current status.

        Args:
            transfer_id: The transfer id to check.
        """
        return await tools.get_payment_status(transfer_id)

    return [send_message, ask_human, request_payment, send_invoice, post_card, get_payment_status]
