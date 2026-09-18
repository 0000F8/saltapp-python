# Agno integration: a `Toolkit` exposing Salt's six tools, with
# `request_payment`/`send_invoice` gated by Agno's own `requires_confirmation`
# (money-moving calls should be confirmed) resolved through Salt, plus a
# driver for any tool's `requires_user_input` pause.
#
# Needs the `agno` extra: pip install "saltapp[agno]"
#
# Verified against Agno 3.0.x docs (2026-09): `Toolkit` (`agno.tools`)
# registers a fixed list of bound methods via `super().__init__(tools=[...])`
# (no auto-discovery); `@tool(requires_confirmation=True)` /
# `@tool(requires_user_input=True, user_input_fields=[...])` gate individual
# methods (or `Toolkit.__init__`'s `requires_confirmation_tools=[...]` /
# `external_execution_required_tools=[...]` gate by name). `agent.run(...)`
# returns a `RunResponse`; when paused, `response.is_paused` is True and
# `response.active_requirements` lists each `RunRequirement` (`.needs_confirmation`
# / `.needs_user_input` / `.needs_external_execution`, `.tool_execution`,
# `.user_input_schema`); resolve each (`.confirm()`/`.reject()`, or set
# `field.value` on every `UserInputField`) then call
# `agent.continue_run(run_id=response.run_id, requirements=response.requirements)`.
# `agent.db` must be configured for `continue_run` to find the paused run.
from __future__ import annotations

from typing import Any, Optional

try:
    from agno.tools import Toolkit
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "saltapp.integrations.agno requires the 'agno' extra: pip install 'saltapp[agno]'"
    ) from exc

from saltapp.agent import Agent
from saltapp.integrations._tools import SaltTools, run_sync

__all__ = ["SaltToolkit", "resolve_agno_run"]


class SaltToolkit(Toolkit):
    """An Agno `Toolkit` exposing Salt's six tools, bound to one chat:

        agent = agno.agent.Agent(tools=[SaltToolkit(agent=salt_agent, chat_id=ctx.chat_id)], db=...)

    Agno's tool methods run synchronously in Agno's own run loop today
    (verified examples are all sync); each method below bridges into
    `SaltTools`'s async implementation via `run_sync` (a fresh event loop
    per call, same tradeoff as `Agent.dispatch_sync` -- fine for
    reference/hobby scale).
    """

    def __init__(self, agent: Agent, chat_id: str, **kwargs: Any) -> None:
        self._salt = SaltTools(agent, chat_id)
        super().__init__(
            name="salt_tools",
            tools=[
                self.send_message,
                self.ask_human,
                self.request_payment,
                self.send_invoice,
                self.post_card,
                self.get_payment_status,
            ],
            requires_confirmation_tools=["request_payment", "send_invoice"],
            **kwargs,
        )

    def send_message(self, text: str) -> str:
        """Send a plain-text message to the human in this Salt chat.

        Args:
            text: The message to send.
        """
        return run_sync(self._salt.send_message(text))

    def ask_human(self, question: str, options: Optional[list[str]] = None, timeout_seconds: float = 120.0) -> str:
        """Ask the human a question in this Salt chat and wait for the answer.

        Args:
            question: The question to ask.
            options: Optional short button labels, e.g. ["Yes", "No"].
            timeout_seconds: How long to wait before giving up.
        """
        return run_sync(self._salt.ask_human(question, options=options, timeout_seconds=timeout_seconds))

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

    def post_card(self, text: str, buttons: Optional[list[str]] = None) -> dict:
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


def resolve_agno_run(agno_agent: Any, run_response: Any, *, agent: Agent, chat_id: str, timeout_seconds: float = 120.0) -> Any:
    """Drives a paused Agno `run_response` through however many
    `requires_confirmation` / `requires_user_input` requirements it hits,
    asking on Salt for each one, resuming via `agno_agent.continue_run(...)`
    until the run finishes (`response.is_paused` is False). `external_execution`
    requirements are NOT bridged here -- they run arbitrary local code, not
    a human decision, so this raises rather than guessing.

        run_response = agno_agent.run("Send $5 to @dan for coffee.")
        run_response = resolve_agno_run(agno_agent, run_response, agent=salt_agent, chat_id=ctx.chat_id)
    """
    tools = SaltTools(agent, chat_id)
    response = run_response
    while getattr(response, "is_paused", False):
        for requirement in response.active_requirements:
            tool_name = getattr(requirement.tool_execution, "tool_name", "a tool")
            tool_args = getattr(requirement.tool_execution, "tool_args", {})
            if requirement.needs_confirmation:
                answer = run_sync(
                    tools.ask_human(
                        f'Confirm tool call "{tool_name}"({tool_args})?',
                        options=["Approve", "Reject"],
                        timeout_seconds=timeout_seconds,
                    )
                )
                if answer.strip().lower().startswith("approve"):
                    requirement.confirm()
                else:
                    requirement.reject()
            elif requirement.needs_user_input:
                for field in requirement.user_input_schema:
                    field.value = run_sync(
                        tools.ask_human(
                            f'{tool_name} needs "{field.name}" ({field.description or "no description"}):',
                            timeout_seconds=timeout_seconds,
                        )
                    )
            elif requirement.needs_external_execution:
                raise NotImplementedError(
                    "external_execution requirements aren't bridged to Salt -- "
                    "they run arbitrary local code, not a human decision."
                )
        response = agno_agent.continue_run(run_id=response.run_id, requirements=response.requirements)
    return response
