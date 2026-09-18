# LlamaIndex integration: a `BaseToolSpec` exposing Salt's six tools.
#
# Needs the `llamaindex` extra: pip install "saltapp[llamaindex]"
#
# Verified against llama-index-core's current source (2026-09):
# `BaseToolSpec` (`llama_index.core.tools.tool_spec.base`) is a plain class
# with a `spec_functions: list[str]` attribute naming which methods become
# tools, and `to_tool_list()` builds a `FunctionTool` per name via
# `FunctionTool.from_defaults(fn=...)` (schema from type hints + docstring,
# same as every real LlamaHub tool spec, e.g. `SlackToolSpec`).
#
# `ask_human` is listed in `spec_functions` as `("ask_human", "aask_human")`
# -- LlamaIndex's `FunctionTool.from_defaults` accepts a sync/async pair so
# an async-native agent (e.g. `FunctionAgent`) calls the coroutine directly
# instead of a thread-bridged sync wrapper.
from __future__ import annotations

from typing import Any, Optional

try:
    from llama_index.core.tools.tool_spec.base import BaseToolSpec
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "saltapp.integrations.llamaindex requires the 'llamaindex' extra: "
        "pip install 'saltapp[llamaindex]'"
    ) from exc

from saltapp.agent import Agent
from saltapp.integrations._tools import SaltTools, run_sync

__all__ = ["SaltToolSpec"]


class SaltToolSpec(BaseToolSpec):
    """A LlamaIndex `BaseToolSpec` exposing Salt's six tools, bound to one
    chat:

        tool_spec = SaltToolSpec(agent=salt_agent, chat_id=ctx.chat_id)
        agent = FunctionAgent(llm=llm, tools=tool_spec.to_tool_list())
    """

    spec_functions = [
        ("send_message", "asend_message"),
        ("ask_human", "aask_human"),
        ("request_payment", "arequest_payment"),
        ("send_invoice", "asend_invoice"),
        ("post_card", "apost_card"),
        ("get_payment_status", "aget_payment_status"),
    ]

    def __init__(self, agent: Agent, chat_id: str) -> None:
        self._salt = SaltTools(agent, chat_id)

    # -- send_message --

    def send_message(self, text: str) -> str:
        """Send a plain-text message to the human in this Salt chat.

        Args:
            text: The message to send.
        """
        return run_sync(self._salt.send_message(text))

    async def asend_message(self, text: str) -> str:
        """Async form of `send_message`."""
        return await self._salt.send_message(text)

    # -- ask_human --

    def ask_human(self, question: str, options: Optional[list[str]] = None, timeout_seconds: float = 120.0) -> str:
        """Ask the human a question in this Salt chat and wait for the answer.

        Posts the question (as buttons if `options` is given) and waits
        for either a button tap or a typed reply, whichever comes first.

        Args:
            question: The question to ask.
            options: Optional short button labels, e.g. ["Yes", "No"].
            timeout_seconds: How long to wait before giving up.
        """
        return run_sync(self._salt.ask_human(question, options=options, timeout_seconds=timeout_seconds))

    async def aask_human(self, question: str, options: Optional[list[str]] = None, timeout_seconds: float = 120.0) -> str:
        """Async form of `ask_human`."""
        return await self._salt.ask_human(question, options=options, timeout_seconds=timeout_seconds)

    # -- request_payment --

    def request_payment(self, receiver_id: str, wallet_id: str, amount: str, message: Optional[str] = None) -> dict:
        """Request a payment from the human, as a real Salt payment-request bubble.

        Args:
            receiver_id: Salt user id who should pay.
            wallet_id: Salt wallet id the payment should land in.
            amount: Human-decimal amount, e.g. "4.50".
            message: Optional note.
        """
        return run_sync(self._salt.request_payment(receiver_id, wallet_id, amount, message))

    async def arequest_payment(
        self, receiver_id: str, wallet_id: str, amount: str, message: Optional[str] = None
    ) -> dict:
        """Async form of `request_payment`."""
        return await self._salt.request_payment(receiver_id, wallet_id, amount, message)

    # -- send_invoice --

    def send_invoice(
        self,
        receiver_id: str,
        wallet_id: str,
        amount: str,
        line_items: list[dict],
        message: Optional[str] = None,
        due_at: Optional[str] = None,
    ) -> dict:
        """Send an itemized invoice to the human on Salt's payment rail.

        Args:
            receiver_id: Salt user id who should pay.
            wallet_id: Salt wallet id the payment should land in.
            amount: Total human-decimal amount.
            line_items: [{"name", "qty", "unit_price", "subtotal"}, ...].
            message: Optional note.
            due_at: Optional ISO8601 due date.
        """
        return run_sync(self._salt.send_invoice(receiver_id, wallet_id, amount, line_items, message, due_at))

    async def asend_invoice(
        self,
        receiver_id: str,
        wallet_id: str,
        amount: str,
        line_items: list[dict],
        message: Optional[str] = None,
        due_at: Optional[str] = None,
    ) -> dict:
        """Async form of `send_invoice`."""
        return await self._salt.send_invoice(receiver_id, wallet_id, amount, line_items, message, due_at)

    # -- post_card --

    def post_card(self, text: str, buttons: Optional[list[str]] = None) -> dict:
        """Post an interactive card into the Salt chat.

        Args:
            text: The card's plain-text summary.
            buttons: Optional short button labels.
        """
        return run_sync(self._salt.post_card(text, buttons))

    async def apost_card(self, text: str, buttons: Optional[list[str]] = None) -> dict:
        """Async form of `post_card`."""
        return await self._salt.post_card(text, buttons)

    # -- get_payment_status --

    def get_payment_status(self, transfer_id: str) -> dict:
        """Look up a Salt payment's current status.

        Args:
            transfer_id: The transfer id to check.
        """
        return run_sync(self._salt.get_payment_status(transfer_id))

    async def aget_payment_status(self, transfer_id: str) -> dict:
        """Async form of `get_payment_status`."""
        return await self._salt.get_payment_status(transfer_id)
