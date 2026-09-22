# The `Agent` class: hosts exactly one Salt identity, wires together
# `saltapp.client`, `saltapp.webhook`/`saltapp.socket`, and the Salt-protocol
# mechanics ported from salt-agent-sdk/src/webhook.ts -- delivery-id dedupe,
# the mention rule (an agent-authored message in a chat with a real human
# present is only yours to answer when you're @mentioned), the
# agent-to-agent loop guard, and GACM (Global Agent Chat Mode: stay silent
# when the chat names another agent as active) -- so a handler only ever
# has to decide WHAT to say, never WHETHER to say it.
#
# `ctx.ask()`/`ctx.approve()` are this SDK's own addition (not yet in the TS
# SDK as of this writing): built entirely on primitives that already exist
# -- post a card, wait for the matching card_interaction or a plain chat
# reply -- with no new server endpoint assumed. See AGENTS.md for why.
from __future__ import annotations

import asyncio
import json
import logging
import re as _re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from saltapp import cards as cards_module
from saltapp import crypto
from saltapp.cable import (
    PING_TIMEOUT_SECONDS,
    RECONNECT_MAX_DELAY_SECONDS,
    RECONNECT_MIN_DELAY_SECONDS,
    CableClient,
    Connector,
)
from saltapp.client import AsyncSaltClient
from saltapp.errors import SaltAppError
from saltapp.identity import Identity
from saltapp.socket import (
    SOCKET_SIGNATURE_TOLERANCE_SECONDS,
    CursorStore,
    DedupeStore,
)
from saltapp.webhook import Event, create_asgi_app

DEFAULT_ASK_TIMEOUT_SECONDS = 120.0

# M2 (security review, 2026-09-18): an exact yes/no word (with at most one
# trailing "." or "!"), not a prefix/substring match -- "yesterday I..."
# must never read as an approval. Governs only the FREE-TEXT reply path; a
# tapped "Yes"/"No" button resolves by its own label, matched exactly
# below, never through this regex.
_APPROVE_YES_RE = _re.compile(r"^(y|yes)[.!]?$", _re.IGNORECASE)

# Only in a group (> 2 members): a reply is addressed back to whoever it
# answers by @handle, same as salt-agent-sdk's makeReply -- being spoken to
# by name is how a person tells which of several messages a reply is for.
_MENTION_RE_TEMPLATE = r"(^|\s)@{handle}(?![\w.-])"

MAX_AGENT_TO_AGENT_REPLIES_PER_CHAT = 2
CONSULT_RUNAWAY_LIMIT = 20


class AskTimeout(SaltAppError):
    """`ctx.ask()`/`ctx.approve()` got no answer within the timeout."""


class _BoundedSet:
    """A bounded "have I seen this before" set, oldest-evicted-first --
    ported from webhook.ts's seenMessageIds/seenChatOpened pattern, applied
    here to X-Salt-Delivery-Id so every event family (not just messages)
    gets the same dedupe for free."""

    def __init__(self, max_size: int = 2000) -> None:
        self._max_size = max_size
        self._seen: dict[str, None] = {}
        self._lock = threading.Lock()

    def seen(self, key: str) -> bool:
        with self._lock:
            if key in self._seen:
                return True
            self._seen[key] = None
            if len(self._seen) > self._max_size:
                oldest = next(iter(self._seen))
                del self._seen[oldest]
            return False


@dataclass
class _Waiter:
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None


class _AskRegistry:
    """Correlates a posted "ask" card (or a free-text question) with the
    interaction/message that answers it. Purely in-process, purely built on
    existing primitives -- see the module docstring.

    M2 (security review, 2026-09-18) parity with the TS SDK's ask.ts: a
    pending ask names one expected answerer (`expected_answerer_id`) when
    the caller has one (see `_BaseContext.ask()`'s default derivation) and
    a tap/reply from anyone else -- or from ANY agent, regardless of id --
    never resolves it. There's no separate identity dimension on the key
    here the way ask.ts needs one: the TS SDK's IdentityStore can host
    several identities sharing one process/registry, but
    `saltapp.agent.Agent` hosts exactly one identity per instance (see
    AGENTS.md) and `_AskRegistry` is a per-Agent attribute, so "this
    identity's own pending asks" is already the whole registry -- keying by
    (identity, chat) would just repeat information the instance boundary
    already carries.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # card_id -> (waiter, expected_answerer_id_or_None)
        self._by_card_id: dict[str, tuple[_Waiter, Optional[str]]] = {}
        # chat_id -> (waiter, expected_answerer_id_or_None)
        self._by_chat_text: dict[str, tuple[_Waiter, Optional[str]]] = {}

    def register_card(self, card_id: str, expected_answerer_id: Optional[str] = None) -> _Waiter:
        waiter = _Waiter()
        with self._lock:
            self._by_card_id[card_id] = (waiter, expected_answerer_id)
        return waiter

    def register_free_text(self, chat_id: str, expected_answerer_id: Optional[str]) -> _Waiter:
        waiter = _Waiter()
        with self._lock:
            self._by_chat_text[chat_id] = (waiter, expected_answerer_id)
        return waiter

    def cancel_card(self, card_id: str) -> None:
        with self._lock:
            self._by_card_id.pop(card_id, None)

    def cancel_free_text(self, chat_id: str) -> None:
        with self._lock:
            self._by_chat_text.pop(chat_id, None)

    def try_resolve_card_interaction(self, card_id: str, action_id: str, user: dict, blocks: Any) -> bool:
        """True when this tap actually resolved the pending ask. A tap from
        an agent (any id) is ignored outright; a tap from someone other
        than the ask's named answerer (when one was named) is ignored too
        -- in both cases the waiter is left pending (not consumed), so the
        real answerer's later tap/reply can still resolve it, and this tap
        falls through to the caller's own on_card_interaction handler, if
        any, exactly as if no ask() were pending."""
        with self._lock:
            entry = self._by_card_id.get(card_id)
        if entry is None:
            return False
        waiter, expected_answerer_id = entry
        if user.get("account_type") == "Agent":
            return False
        if expected_answerer_id is not None and str(user.get("id", "")).lower() != str(expected_answerer_id).lower():
            return False
        with self._lock:
            # Re-check under the lock: another thread may have already
            # resolved (and popped) this exact card between the read above
            # and here.
            if self._by_card_id.pop(card_id, None) is None:
                return False
        waiter.result = {"kind": "option", "action_id": action_id, "user": user, "blocks": blocks}
        waiter.event.set()
        return True

    def try_resolve_message(self, chat_id: str, sender_id: str, text: str, *, sender_is_agent: bool = False) -> bool:
        """True when this message actually resolved the pending free-text
        ask. A message from an agent is ignored outright; a message from
        someone other than the ask's named answerer (when one was named)
        is ignored too -- the waiter is left pending in both cases."""
        if sender_is_agent:
            return False
        with self._lock:
            entry = self._by_chat_text.get(chat_id)
            if entry is None:
                return False
            waiter, expected_answerer_id = entry
            if expected_answerer_id is not None and str(sender_id).lower() != str(expected_answerer_id).lower():
                return False
            del self._by_chat_text[chat_id]
        waiter.result = {"kind": "text", "text": text, "sender_id": sender_id}
        waiter.event.set()
        return True

    @staticmethod
    async def wait(waiter: _Waiter, timeout: float) -> dict[str, Any]:
        ok = await asyncio.to_thread(waiter.event.wait, timeout)
        if not ok:
            raise AskTimeout(f"no answer within {timeout}s")
        assert waiter.result is not None
        return waiter.result


@dataclass
class AskResult:
    """The answer to `ctx.ask()`: either a tapped button (`kind="option"`,
    `action_id`/`value` set) or a typed reply (`kind="text"`, `text` set)."""

    kind: str
    action_id: str | None = None
    value: str | None = None
    text: str | None = None
    user: dict[str, Any] | None = None


class _BaseContext:
    def __init__(self, agent: "Agent", chat_id: str) -> None:
        self.agent = agent
        self.chat_id = chat_id

    def _default_answerer_id(self) -> Optional[str]:
        """The human who triggered whatever this context is handling, when
        there's a natural one to name -- overridden by MessageContext (the
        sender), ChatOpenedContext (whoever opened the chat), and
        InvoicePaidContext (the buyer), each only when that party isn't an
        agent (see AskOptions.answererId in ask.ts: "always the HUMAN one;
        an agent sender/buyer/opener yields no default"). A bare context
        (`saltapp.agent.tool_context`, used by every
        `saltapp.integrations.<framework>` tool) has no triggering message
        behind it at all, so this stays None there -- callers of `ask()`
        through that path must pass `from_user_id` explicitly for the
        answerer restriction to apply; without one, `ask()` falls back to
        the permissive "anyone in the chat may answer" behavior it always
        had, a deliberate, documented divergence from the TS SDK's hard
        requirement (see AGENTS.md)."""
        return None

    async def post_card(self, blocks: list[dict[str, Any]], text: str) -> dict[str, Any]:
        return await self.agent.client.post_card(self.agent.identity.api_key, self.chat_id, blocks, text)

    async def update_card(self, card_id: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        return await self.agent.client.update_card(self.agent.identity.api_key, card_id, blocks)

    async def request_payment(
        self, *, receiver_id: str, wallet_id: str, amount: str, message: str | None = None
    ) -> dict[str, Any]:
        return await self.agent.client.request_payment(
            self.agent.identity.api_key,
            chat_id=self.chat_id,
            receiver_id=receiver_id,
            wallet_id=wallet_id,
            amount=amount,
            message=message,
        )

    async def ask(
        self,
        question: str,
        *,
        options: list[str] | None = None,
        free_text: bool = False,
        from_user_id: str | None = None,
        timeout: float = DEFAULT_ASK_TIMEOUT_SECONDS,
    ) -> AskResult:
        """Ask a human-in-the-loop question, built on cards + the
        interaction/message that answers it (no new server endpoint
        assumed -- see the module docstring):

        - `options` (a list of short labels): posts a card with one button
          per option; resolves with `kind="option"` the moment one is
          tapped.
        - `free_text=True` (may be combined with `options`): also accepts a
          plain chat message as the answer; resolves with `kind="text"`.
        - `from_user_id`: the ONE person allowed to answer (M2, security
          review 2026-09-18) -- a tap or typed reply from anyone else, or
          from any agent regardless of id, is ignored outright and never
          resolves this ask. Defaults to whoever triggered the context
          this `ask()` is called from (`MessageContext`'s sender,
          `ChatOpenedContext`'s opener, `InvoicePaidContext`'s buyer -- see
          `_default_answerer_id()`); when that party is itself an agent, or
          this is a bare context with no natural trigger (e.g.
          `saltapp.integrations.*`'s `tool_context()`), there is no
          default, and `ask()` falls back to its old permissive behavior
          (anyone non-agent in the chat may answer) rather than raising --
          pass `from_user_id` explicitly to restrict it there too.

        Raises `AskTimeout` if nothing answers within `timeout` seconds.
        """
        if not options and not free_text:
            raise ValueError("ask() needs options, free_text=True, or both")

        answerer_id = from_user_id if from_user_id is not None else self._default_answerer_id()

        blocks: list[dict[str, Any]] = [cards_module.section(text=question)]
        action_ids: dict[str, str] = {}
        if options:
            buttons = []
            for i, label in enumerate(options):
                action_id = f"ask_{uuid.uuid4().hex[:8]}_{i}"
                action_ids[action_id] = label
                # Server-enforced too (cards_controller#actions/Card#find_button):
                # a tap from anyone not on restricted_to is refused 403
                # before it ever becomes a card_interaction event.
                buttons.append(cards_module.button(action_id, label, restricted_to=[answerer_id] if answerer_id else None))
            blocks.append(cards_module.actions(buttons))

        card = await self.post_card(blocks, question)
        card_id = str(card.get("id") or card.get("card_id"))

        card_waiter = self.agent._ask_registry.register_card(card_id, answerer_id) if options else None
        text_waiter = self.agent._ask_registry.register_free_text(self.chat_id, answerer_id) if free_text else None

        try:
            waiters = [w for w in (card_waiter, text_waiter) if w is not None]
            if len(waiters) == 1:
                result = await _AskRegistry.wait(waiters[0], timeout)
            else:
                result = await self._wait_first(waiters, timeout)
        finally:
            if card_waiter is not None:
                self.agent._ask_registry.cancel_card(card_id)
            if text_waiter is not None:
                self.agent._ask_registry.cancel_free_text(self.chat_id)

        if result["kind"] == "option":
            return AskResult(
                kind="option",
                action_id=result["action_id"],
                value=action_ids.get(result["action_id"], result["action_id"]),
                user=result.get("user"),
            )
        return AskResult(kind="text", text=result["text"])

    @staticmethod
    async def _wait_first(waiters: list[_Waiter], timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AskTimeout(f"no answer within {timeout}s")
            for waiter in waiters:
                if waiter.event.is_set():
                    assert waiter.result is not None
                    return waiter.result
            await asyncio.sleep(min(0.05, remaining))

    async def approve(self, summary: str, *, from_user_id: str | None = None, timeout: float = DEFAULT_ASK_TIMEOUT_SECONDS) -> bool:
        """`ask()` specialized to a Yes/No decision: two buttons, and (M2,
        security review 2026-09-18) an EXACT "y"/"yes" typed reply
        (case-insensitive, an optional trailing "." or "!") also counts as
        approval -- anything else typed, including a near-miss like
        "yeah", resolves False, same as tapping "No". A tapped button
        matches its own label exactly (never through the typed-reply
        regex)."""
        result = await self.ask(summary, options=["Yes", "No"], free_text=True, from_user_id=from_user_id, timeout=timeout)
        if result.kind == "option":
            return (result.value or "").strip().lower() == "yes"
        return bool(_APPROVE_YES_RE.match((result.text or "").strip()))


class MessageContext(_BaseContext):
    def __init__(
        self,
        agent: "Agent",
        *,
        chat_id: str,
        sender_id: str,
        sender: dict[str, Any],
        text: str,
        room_id: str,
        chat_meta: dict[str, Any],
        raw_message: dict[str, Any],
        encrypted: bool = True,
        delivered_because: str | None = None,
    ) -> None:
        super().__init__(agent, chat_id)
        self.sender_id = sender_id
        self.sender = sender
        self.text = text
        self.room_id = room_id
        self.chat_meta = chat_meta
        self.raw_message = raw_message
        # Open rooms (2026-09-22): whether this message (and therefore this
        # chat, at least right now -- see Message#encrypted's own header)
        # was plain text (False) or PGP ciphertext this agent's key already
        # decrypted (True). reply() reads this to decide how to send back;
        # a handler that wants to know for its own reasons (e.g. logging,
        # or choosing not to say anything sensitive in an open room) reads
        # it directly.
        self.encrypted = encrypted
        # Interests (open rooms): why THIS agent got THIS delivery --
        # "mention"|"reply"|"keyword"|"all" -- present only for an open
        # chat's delivery (see Message#formatted_message); None for an
        # encrypted chat (today's mention/reply rule, unlabeled) or an open
        # chat delivered before this field existed.
        self.delivered_because = delivered_because

    def _default_answerer_id(self) -> Optional[str]:
        if self.sender.get("account_type") == "Agent":
            return None
        return self.sender_id

    async def reply(self, text: str) -> None:
        addressee = self.sender if self.sender.get("account_type") != "Agent" else None
        mentions = [self.sender_id] if addressee and addressee.get("username") else None
        outgoing = text
        if addressee and mentions:
            import re

            pattern = _MENTION_RE_TEMPLATE.format(handle=re.escape(addressee["username"]))
            members = await self.agent.client.get_chat_members(self.agent.identity.api_key, self.chat_id)
            if len(members) > 2 and not re.search(pattern, text):
                outgoing = f"@{addressee['username']} {text}"
            else:
                mentions = None
        if self.encrypted:
            await self.agent.client.send_message(self.agent.identity, self.chat_id, outgoing, mentions=mentions)
        else:
            # Open room: plain text, no PGP -- see
            # AsyncSaltClient.post_plain_message.
            await self.agent.client.post_plain_message(
                self.agent.identity.api_key, self.chat_id, outgoing, mentions=mentions
            )


class CardInteractionContext(_BaseContext):
    def __init__(
        self, agent: "Agent", *, chat_id: str, card_id: str, action_id: str, user: dict[str, Any], blocks: Any
    ) -> None:
        super().__init__(agent, chat_id)
        self.card_id = card_id
        self.action_id = action_id
        self.user = user
        self.blocks = blocks


class ChatOpenedContext(_BaseContext):
    def __init__(
        self, agent: "Agent", *, chat_id: str, chat: dict[str, Any], opened_by: dict[str, Any], members: list[dict[str, Any]]
    ) -> None:
        super().__init__(agent, chat_id)
        self.chat = chat
        self.opened_by = opened_by
        self.members = members

    def _default_answerer_id(self) -> Optional[str]:
        if self.opened_by.get("account_type") == "Agent":
            return None
        return self.opened_by.get("id")

    async def reply(self, text: str) -> None:
        await self.agent.client.send_message(self.agent.identity, self.chat_id, text)


class InvoicePaidContext(_BaseContext):
    def __init__(
        self, agent: "Agent", *, chat_id: str, buyer: dict[str, Any], line_items: list[dict[str, Any]],
        amount: Any, is_top_up: bool, transfer_request_id: Any,
    ) -> None:
        super().__init__(agent, chat_id)
        self.buyer = buyer
        self.line_items = line_items
        self.amount = amount
        self.is_top_up = is_top_up
        self.transfer_request_id = transfer_request_id

    def _default_answerer_id(self) -> Optional[str]:
        if self.buyer.get("account_type") == "Agent":
            return None
        return self.buyer.get("id")

    async def reply(self, text: str) -> None:
        await self.agent.client.send_message(self.agent.identity, self.chat_id, text)


Handler = Callable[[Any], Awaitable[None]]


class Agent:
    """Hosts exactly one Salt agent identity. Register handlers with the
    decorators, then either mount `asgi_app()` behind a public URL, or call
    `run_socket()` from a laptop with none."""

    def __init__(
        self,
        *,
        host: str,
        api_key: str,
        public_key: str,
        private_key: str,
        passphrase: str = "",
        agent_id: str = "",
        verify_signatures: bool = True,
        signature_tolerance_seconds: int = 300,
        client: AsyncSaltClient | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.identity = Identity(
            api_key=api_key, public_key=public_key, private_key=private_key,
            passphrase=passphrase, agent_id=agent_id,
        )
        self.client = client or AsyncSaltClient(host)
        self.verify_signatures = verify_signatures
        self.signature_tolerance_seconds = signature_tolerance_seconds
        self.webhook_secret: str | None = None
        self._logger = logger or logging.getLogger("saltapp.agent")

        self._handlers: dict[str, Handler] = {}
        self._seen_delivery_ids = _BoundedSet()
        self._ask_registry = _AskRegistry()
        self._agent_to_agent_counts: dict[str, int] = {}
        self._lane_room_of: dict[str, str] = {}

    # -- registration --

    def on_message(self, fn: Handler) -> Handler:
        self._handlers["message"] = fn
        return fn

    def on_card_interaction(self, fn: Handler) -> Handler:
        self._handlers["card_interaction"] = fn
        return fn

    def on_chat_opened(self, fn: Handler) -> Handler:
        self._handlers["chat_opened"] = fn
        return fn

    def on_invoice_paid(self, fn: Handler) -> Handler:
        self._handlers["invoice_paid"] = fn
        return fn

    def on_handoff_confirmed(self, fn: Handler) -> Handler:
        self._handlers["handoff_confirmed"] = fn
        return fn

    def on_handoff_received(self, fn: Handler) -> Handler:
        self._handlers["handoff_received"] = fn
        return fn

    # -- setup --

    async def ensure_identity(self) -> Identity:
        """Fills in `agent_id` and `webhook_secret` from salt-api if not
        already known -- salt-api is the only authority on which id an
        api-key belongs to."""
        info = await self.client.who_am_i(self.identity.api_key)
        if info.get("agent_id"):
            self.identity.agent_id = str(info["agent_id"])
        if info.get("webhook_secret"):
            self.webhook_secret = str(info["webhook_secret"])
        return self.identity

    def health_extra(self) -> dict[str, Any]:
        return {"agent_id": self.identity.agent_id or None}

    # -- dispatch --

    async def dispatch(self, event: Event) -> None:
        """Route one verified Event: delivery-id dedupe, then to the event
        family's own handling (mention rule + loop guard for `message`;
        the rest are dispatched directly)."""
        if event.delivery_id and self._seen_delivery_ids.seen(event.delivery_id):
            return

        try:
            if event.type == "message":
                await self._handle_message(event.body)
            elif event.type == "card_interaction":
                await self._handle_card_interaction(event.body)
            elif event.type == "chat_opened":
                await self._handle_chat_opened(event.body)
            elif event.type == "invoice_paid":
                await self._handle_invoice_paid(event.body)
            elif event.type == "handoff_confirmed":
                await self._call_simple_handler("handoff_confirmed", event.body)
            elif event.type == "handoff_received":
                await self._call_simple_handler("handoff_received", event.body)
        except Exception as exc:  # noqa: BLE001
            self._logger.error("[dispatch] handling %s failed: %s", event.type, exc)

    def dispatch_sync(self, event: Event) -> None:
        """Sync entry point for a sync web framework (Flask). Runs the same
        async dispatch to completion on a fresh event loop."""
        asyncio.run(self.dispatch(event))

    async def _call_simple_handler(self, name: str, body: dict[str, Any]) -> None:
        handler = self._handlers.get(name)
        if handler is None:
            return
        await handler(body)

    async def _handle_message(self, body: dict[str, Any]) -> None:
        message = body.get("message") or {}
        chat_meta = body.get("chat") or {}
        chat_id = message.get("chat_id")
        if message.get("event_type"):
            return  # system events aren't prompts

        sender = message.get("user") or {}
        sender_id = sender.get("id")
        if not sender_id:
            return
        if str(sender_id).lower() == str(self.identity.agent_id).lower():
            return  # never reply to our own message (webhook loop-back)

        # Open rooms (2026-09-22): `encrypted` rides on every message now
        # (Message#formatted_message) -- False means plain text, no PGP
        # anywhere near it; True, or absent for an envelope shape that
        # predates this field, means the usual PGP ciphertext, decrypted
        # here with this agent's own private key (every chat member's
        # public key is a recipient of every send -- see
        # crypto.encrypt_for/AsyncSaltClient.send_message). A message this
        # agent's key genuinely can't open -- wrong recipient, corrupt
        # blob, wrong passphrase -- is dropped right here rather than
        # handed to on_message as raw ciphertext (which is what happened
        # before this fix: nothing ever called crypto.decrypt at all).
        raw_text = message.get("message") or ""
        encrypted = bool(message.get("encrypted", True))
        if encrypted:
            try:
                text = crypto.decrypt(raw_text, self.identity.private_key, self.identity.passphrase)
            except crypto.CryptoError as exc:
                self._logger.error(
                    "[chat %s] could not decrypt message %s: %s", chat_id, message.get("message_id"), exc
                )
                return
        else:
            text = raw_text

        sender_is_agent = sender.get("account_type") == "Agent"

        # An ask() waiting on free text from this chat gets first refusal --
        # it must not also be treated as a fresh prompt for on_message. An
        # agent's own message never resolves someone else's pending ask
        # (M2, security review 2026-09-18).
        if self._ask_registry.try_resolve_message(chat_id, sender_id, text, sender_is_agent=sender_is_agent):
            return

        # GACM: another agent is declared active in this chat -- stay silent.
        active_agent_id = chat_meta.get("active_agent_id")
        if active_agent_id and str(active_agent_id).lower() != str(self.identity.agent_id).lower():
            return

        reply_count_key = str(chat_id).lower()
        if sender_is_agent:
            if await self._chat_has_non_observer_human(chat_id, chat_meta):
                # The mention rule: an agent-authored message is only ours
                # to answer when it @mentions us, same gate salt-api applies
                # to whether the webhook is even sent in a group chat.
                mentions = [str(m) for m in (message.get("mentions") or [])]
                if not any(m.lower() == str(self.identity.agent_id).lower() for m in mentions):
                    return
            else:
                limit = CONSULT_RUNAWAY_LIMIT if chat_meta.get("lane_kind") == "consult" else MAX_AGENT_TO_AGENT_REPLIES_PER_CHAT
                count = self._agent_to_agent_counts.get(reply_count_key, 0) + 1
                self._agent_to_agent_counts[reply_count_key] = count
                if count > limit:
                    self._logger.error("[chat %s] agent-to-agent reply cap reached; not auto-replying again.", chat_id)
                    return
        else:
            self._agent_to_agent_counts.pop(reply_count_key, None)

        handler = self._handlers.get("message")
        if handler is None:
            return

        room_id = chat_meta.get("coaching_for_chat_id") or chat_id
        ctx = MessageContext(
            self, chat_id=chat_id, sender_id=sender_id, sender=sender, text=text,
            room_id=room_id, chat_meta=chat_meta, raw_message=message,
            encrypted=encrypted, delivered_because=message.get("delivered_because"),
        )
        await handler(ctx)

    async def _chat_has_non_observer_human(self, chat_id: str, chat_meta: dict[str, Any]) -> bool:
        inline_members = chat_meta.get("users")
        members = inline_members
        if not isinstance(members, list):
            try:
                members = await self.client.get_chat_members(self.identity.api_key, chat_id)
            except Exception as exc:  # noqa: BLE001
                self._logger.error("[chat %s] fetching members for mention-gating failed: %s", chat_id, exc)
                return False
        return any(m.get("account_type") != "Agent" and not m.get("observer") for m in members)

    async def _handle_card_interaction(self, body: dict[str, Any]) -> None:
        card_id = str(body.get("card_id"))
        action_id = str(body.get("action_id"))
        user = body.get("user") or {}
        blocks = (body.get("state") or {}).get("blocks")

        if self._ask_registry.try_resolve_card_interaction(card_id, action_id, user, blocks):
            return

        handler = self._handlers.get("card_interaction")
        if handler is None:
            return
        ctx = CardInteractionContext(
            self, chat_id=body.get("chat_id"), card_id=card_id, action_id=action_id, user=user, blocks=blocks,
        )
        await handler(ctx)

    async def _handle_chat_opened(self, body: dict[str, Any]) -> None:
        handler = self._handlers.get("chat_opened")
        if handler is None:
            return
        chat = body.get("chat") or {}
        ctx = ChatOpenedContext(
            self, chat_id=body.get("chat_id") or chat.get("id"), chat=chat,
            opened_by=body.get("opened_by") or {}, members=body.get("members") or [],
        )
        await handler(ctx)

    async def _handle_invoice_paid(self, body: dict[str, Any]) -> None:
        handler = self._handlers.get("invoice_paid")
        if handler is None:
            return
        ctx = InvoicePaidContext(
            self, chat_id=body.get("chat_id"), buyer=body.get("buyer") or {},
            line_items=body.get("line_items") or [], amount=body.get("amount"),
            is_top_up=bool(body.get("billing_account_id")), transfer_request_id=body.get("transfer_request_id"),
        )
        await handler(ctx)

    # -- webhook mode --

    def asgi_app(self) -> Any:
        """A dependency-free ASGI3 app implementing the webhook (`POST /`)
        and `GET /health`. Run it directly under uvicorn:

            uvicorn.run(agent.asgi_app(), host="0.0.0.0", port=8000)
        """
        return create_asgi_app(
            self.dispatch,
            verify_signatures=self.verify_signatures,
            secret=self.webhook_secret,
            tolerance_seconds=self.signature_tolerance_seconds,
            health_extra=self.health_extra(),
        )

    # -- socket mode (no public URL -- a real Action Cable connection, not
    # -- a poll loop; see saltapp.cable's header for the full contract) ----

    def run_socket(
        self,
        *,
        cursor_store: CursorStore | None = None,
        dedupe_store: DedupeStore | None = None,
        backfill_limit: int = 100,
    ) -> None:
        """Blocking entry point for socket mode: no public URL needed. Sets
        `users.delivery_mode = "socket"` on salt-api (idempotent), then
        connects to Action Cable and stays connected -- see
        `run_socket_async`. Ctrl-C to stop."""
        asyncio.run(
            self.run_socket_async(cursor_store=cursor_store, dedupe_store=dedupe_store, backfill_limit=backfill_limit)
        )

    async def run_socket_async(
        self,
        *,
        cursor_store: CursorStore | None = None,
        dedupe_store: DedupeStore | None = None,
        backfill_limit: int = 100,
        min_backoff: float = RECONNECT_MIN_DELAY_SECONDS,
        max_backoff: float = RECONNECT_MAX_DELAY_SECONDS,
        ping_timeout: float = PING_TIMEOUT_SECONDS,
        connector: Connector | None = None,
        stop: asyncio.Event | None = None,
    ) -> None:
        """No public URL needed: opens a websocket to salt-api's Action
        Cable (`AgentUpdatesChannel`) and stays connected -- salt-api
        PUSHES each event the instant it happens (see `saltapp.cable`'s
        header comment for the full wire contract). An idle, caught-up
        agent makes zero requests; there is no polling anywhere in this
        path, ever -- not here, not as a fallback.

        Leaving `cursor_store`/`dedupe_store` unset defaults to
        FileCursorStore/FileDedupeStore under `~/.salt/agents/<agent_id>/`
        (dirs 0700, files 0600) -- a restart resumes instead of
        re-delivering or silently skipping up to 7 days of retained
        updates. Pass `saltapp.socket.MemoryCursorStore()`/
        `MemoryDedupeStore()` explicitly to opt out of persistence (always
        do this in a test). `backfill_limit` is the page size for the ONE
        on-demand catch-up call this ever makes on its own (rows per
        `GET /api/v1/agent/updates` page, only when a `replay_done` frame
        says the backlog replay was truncated -- see saltapp.cable)."""
        await self.ensure_identity()
        try:
            await self.client.set_delivery_mode(self.identity.api_key, "socket")
        except Exception as exc:  # noqa: BLE001 -- an agent with a blank callback is socket by default anyway
            self._logger.error("[cable] set_delivery_mode failed (continuing anyway): %s", exc)

        cable_client = CableClient(
            self.client,
            self.identity.api_key,
            agent_id=self.identity.agent_id,
            webhook_secret_provider=lambda: self.webhook_secret,
            verify_signatures=self.verify_signatures,
            # SOCKET_SIGNATURE_TOLERANCE_SECONDS rather than
            # self.signature_tolerance_seconds: the socket/cable path keeps
            # its own named constant because adapters import it, but since
            # serve-time signing landed both are ~300s. See saltapp.socket's
            # header.
            tolerance_seconds=SOCKET_SIGNATURE_TOLERANCE_SECONDS,
            cursor_store=cursor_store,
            dedupe_store=dedupe_store,
            backfill_limit=backfill_limit,
            min_backoff=min_backoff,
            max_backoff=max_backoff,
            ping_timeout=ping_timeout,
            connector=connector,
            logger=self._logger,
        )
        await cable_client.run(self.dispatch, stop=stop)


def tool_context(agent: "Agent", chat_id: str) -> _BaseContext:
    """Public constructor for a bare `_BaseContext` (`post_card`/`ask`/
    `approve`/`request_payment`), scoped to one chat, with no live message
    event backing it. This is what `saltapp.integrations._tools.SaltTools`
    (and, through it, every `saltapp.integrations.<framework>` module)
    builds its tool functions on -- a framework's own tool-calling loop
    doesn't hand us a `MessageContext`, only "the chat this agent run is
    answering in", so this is the smallest context that still gets
    `ask()`'s card-or-text correlation for free."""
    return _BaseContext(agent, chat_id)
