# smolagents (Hugging Face) integration: Salt's six tools as `Tool`
# instances.
#
# Needs the `smolagents` extra: pip install "saltapp[smolagents]"
#
# Verified against smolagents docs (v1.26.0, 2026-09): `Tool` (from
# `smolagents`) is a class with `name`/`description`/`inputs`
# (`dict[str, {"type", "description"}]`)/`output_type` class attributes
# and a `forward(self, ...)` method; `smolagents.Tool.from_function(fn)`
# builds one from a plain function's type hints + docstring (an `Args:`
# section is required). smolagents' `forward()` is called synchronously by
# its own agent loop (no documented async tool path as of this version),
# so every tool below bridges into `SaltTools`'s async implementation via
# `run_sync`.
#
# Publishing to the Hub: `tool_instance.push_to_hub(repo_id, ...)` /
# `Tool.from_hub(repo_id, trust_remote_code=True)` -- see `PUSH_TO_HUB_NOTE`
# below for what that means for a Salt-backed tool specifically.
from __future__ import annotations

from typing import Any, Optional

try:
    from smolagents import Tool
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "saltapp.integrations.smolagents requires the 'smolagents' extra: pip install 'saltapp[smolagents]'"
    ) from exc

from saltapp.agent import Agent
from saltapp.integrations._tools import SaltTools, run_sync

__all__ = ["build_tools", "PUSH_TO_HUB_NOTE"]

PUSH_TO_HUB_NOTE = """\
Pushing one of these tools to the Hugging Face Hub with `tool.push_to_hub(repo_id)`
publishes it as a Space repo containing this tool's `forward()` source --
which closes over a live `saltapp.agent.Agent` (holding this agent's PGP
private key and api-key) and a fixed `chat_id`. Do NOT push a tool built by
`build_tools()` here as-is: anyone who loads it via `Tool.from_hub(repo_id,
trust_remote_code=True)` would run YOUR closure, scoped to YOUR chat and
YOUR credentials, inside their own process. If you want a shareable,
credential-free Salt tool on the Hub, write a version whose `forward()`
reads `SALT_API_KEY`/`SALT_APP_ID`/etc. from the environment at call time
instead of closing over a pre-built `Agent`, so a person who loads it
supplies their own agent's credentials, not yours.
"""


class _SaltTool(Tool):
    def __init__(self, salt: SaltTools) -> None:
        self._salt = salt
        super().__init__()


class SendMessageTool(_SaltTool):
    name = "send_message"
    description = "Send a plain-text message to the human in this Salt chat."
    inputs = {"text": {"type": "string", "description": "The message to send."}}
    output_type = "string"

    def forward(self, text: str) -> str:
        return run_sync(self._salt.send_message(text))


class AskHumanTool(_SaltTool):
    name = "ask_human"
    description = (
        "Ask the human a question in this Salt chat and wait for their reply -- a "
        "tapped button (if options are given) or typed text, whichever comes first."
    )
    inputs = {
        "question": {"type": "string", "description": "The question to ask."},
        "options": {
            "type": "array",
            "description": "Optional short button labels, e.g. ['Yes', 'No'].",
            "nullable": True,
        },
        "timeout_seconds": {
            "type": "number",
            "description": "How long to wait for an answer before giving up.",
            "nullable": True,
        },
    }
    output_type = "string"

    def forward(self, question: str, options: Optional[list[str]] = None, timeout_seconds: float = 120.0) -> str:
        return run_sync(self._salt.ask_human(question, options=options, timeout_seconds=timeout_seconds))


class RequestPaymentTool(_SaltTool):
    name = "request_payment"
    description = "Request a payment from the human, as a real Salt payment-request bubble."
    inputs = {
        "receiver_id": {"type": "string", "description": "Salt user id who should pay."},
        "wallet_id": {"type": "string", "description": "Salt wallet id the payment should land in."},
        "amount": {"type": "string", "description": 'Human-decimal amount, e.g. "4.50".'},
        "message": {"type": "string", "description": "Optional note.", "nullable": True},
    }
    output_type = "object"

    def forward(self, receiver_id: str, wallet_id: str, amount: str, message: Optional[str] = None) -> dict:
        return run_sync(self._salt.request_payment(receiver_id, wallet_id, amount, message))


class SendInvoiceTool(_SaltTool):
    name = "send_invoice"
    description = "Send an itemized invoice to the human on Salt's payment rail."
    inputs = {
        "receiver_id": {"type": "string", "description": "Salt user id who should pay."},
        "wallet_id": {"type": "string", "description": "Salt wallet id the payment should land in."},
        "amount": {"type": "string", "description": "Total human-decimal amount."},
        "line_items": {
            "type": "array",
            "description": '[{"name", "qty", "unit_price", "subtotal"}, ...].',
        },
        "message": {"type": "string", "description": "Optional note.", "nullable": True},
        "due_at": {"type": "string", "description": "Optional ISO8601 due date.", "nullable": True},
    }
    output_type = "object"

    def forward(
        self,
        receiver_id: str,
        wallet_id: str,
        amount: str,
        line_items: list[dict],
        message: Optional[str] = None,
        due_at: Optional[str] = None,
    ) -> dict:
        return run_sync(self._salt.send_invoice(receiver_id, wallet_id, amount, line_items, message, due_at))


class PostCardTool(_SaltTool):
    name = "post_card"
    description = "Post an interactive card (a message with optional buttons) into the Salt chat."
    inputs = {
        "text": {"type": "string", "description": "The card's plain-text summary."},
        "buttons": {"type": "array", "description": "Optional short button labels.", "nullable": True},
    }
    output_type = "object"

    def forward(self, text: str, buttons: Optional[list[str]] = None) -> dict:
        return run_sync(self._salt.post_card(text, buttons))


class GetPaymentStatusTool(_SaltTool):
    name = "get_payment_status"
    description = "Look up a Salt payment's current status by transfer id."
    inputs = {"transfer_id": {"type": "string", "description": "The transfer id to check."}}
    output_type = "object"

    def forward(self, transfer_id: str) -> dict:
        return run_sync(self._salt.get_payment_status(transfer_id))


def build_tools(agent: Agent, chat_id: str) -> list[Tool]:
    """All six Salt tools as smolagents `Tool` instances bound to one chat:

        agent = smolagents.ToolCallingAgent(tools=build_tools(salt_agent, ctx.chat_id), model=model)
    """
    salt = SaltTools(agent, chat_id)
    return [
        SendMessageTool(salt),
        AskHumanTool(salt),
        RequestPaymentTool(salt),
        SendInvoiceTool(salt),
        PostCardTool(salt),
        GetPaymentStatusTool(salt),
    ]
