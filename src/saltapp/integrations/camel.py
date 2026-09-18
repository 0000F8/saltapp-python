# CAMEL-AI integration: a `HumanToolkit`-compatible toolkit whose ask goes
# to Salt, plus the other five Salt tools.
#
# Needs the `camel` extra: pip install "saltapp[camel]"
#
# Verified against camel-ai's current source (`camel/toolkits/{base,human_toolkit}.py`,
# 2026-09): `BaseToolkit` (`camel.toolkits`) requires only `get_tools() ->
# List[FunctionTool]`; a subclass wraps its own bound methods in
# `FunctionTool(self.method)` (schema from type hints + docstring). CAMEL's
# built-in `HumanToolkit` exposes exactly two methods --
# `ask_human_via_console(question: str) -> str` (blocking, returns the
# reply) and `send_message_to_user(message: str) -> str` (fire-and-forget,
# returns a confirmation string) -- and is wired into an agent via
# `ChatAgent(tools=[*HumanToolkit().get_tools()])`. `SaltHumanToolkit` below
# exposes the SAME two method names/signatures (so it drops in wherever
# `HumanToolkit` is used today, just routing to a Salt chat instead of the
# console) plus Salt's other four tools (`request_payment`, `send_invoice`,
# `post_card`, `get_payment_status`).
from __future__ import annotations

from typing import Any, List, Optional

try:
    from camel.toolkits import BaseToolkit, FunctionTool
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError("saltapp.integrations.camel requires the 'camel' extra: pip install 'saltapp[camel]'") from exc

from saltapp.agent import Agent
from saltapp.integrations._tools import SaltTools, run_sync

__all__ = ["SaltHumanToolkit"]


class SaltHumanToolkit(BaseToolkit):
    """Drop-in replacement for CAMEL's `HumanToolkit`: exposes the SAME
    `ask_human_via_console` / `send_message_to_user` method names and
    signatures, routed to a Salt chat instead of the console, plus Salt's
    other four tools:

        toolkit = SaltHumanToolkit(agent=salt_agent, chat_id=ctx.chat_id)
        chat_agent = ChatAgent(tools=[*toolkit.get_tools()])
    """

    def __init__(self, agent: Agent, chat_id: str, timeout: Optional[float] = None) -> None:
        super().__init__(timeout=timeout) if timeout is not None else super().__init__()
        self._salt = SaltTools(agent, chat_id)

    def ask_human_via_console(self, question: str) -> str:
        """Ask the human a question and wait for their reply -- routed to
        the bound Salt chat instead of a console prompt. Same name/signature
        as `camel.toolkits.HumanToolkit.ask_human_via_console` on purpose,
        so this drops in wherever that toolkit is used.

        Args:
            question: The question to ask the human user.
        """
        return run_sync(self._salt.ask_human(question))

    def send_message_to_user(self, message: str) -> str:
        """Send a one-way message to the human -- no reply expected. Same
        name/signature as `camel.toolkits.HumanToolkit.send_message_to_user`.

        Args:
            message: The message to send.
        """
        run_sync(self._salt.send_message(message))
        return f"Message successfully sent to user: '{message}'"

    def request_payment(self, receiver_id: str, wallet_id: str, amount: str, message: Optional[str] = None) -> dict:
        """Request a payment from the human, as a real Salt payment-request bubble.

        Args:
            receiver_id: Salt user id who should pay.
            wallet_id: Salt wallet id the payment should land in.
            amount: Human-decimal amount, e.g. "4.50".
            message: Optional note.
        """
        return run_sync(self._salt.request_payment(receiver_id, wallet_id, amount, message))

    def send_invoice(
        self,
        receiver_id: str,
        wallet_id: str,
        amount: str,
        line_items: list,
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

    def post_card(self, text: str, buttons: Optional[list] = None) -> dict:
        """Post an interactive card into the Salt chat.

        Args:
            text: The card's plain-text summary.
            buttons: Optional short button labels.
        """
        return run_sync(self._salt.post_card(text, buttons))

    def get_payment_status(self, transfer_id: str) -> dict:
        """Look up a Salt payment's current status.

        Args:
            transfer_id: The transfer id to check.
        """
        return run_sync(self._salt.get_payment_status(transfer_id))

    def get_tools(self) -> List[FunctionTool]:
        return [
            FunctionTool(self.ask_human_via_console),
            FunctionTool(self.send_message_to_user),
            FunctionTool(self.request_payment),
            FunctionTool(self.send_invoice),
            FunctionTool(self.post_card),
            FunctionTool(self.get_payment_status),
        ]
