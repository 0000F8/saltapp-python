# Typed REST client for the Salt platform API (salt-api). Ported from
# salt-agent-sdk/src/client.ts, generalized the same way that module is:
# every method takes the acting identity's own api-key explicitly rather
# than a client-wide header, since one process can legitimately act as more
# than one Salt agent (see saltapp.identity.Identity).
#
# Two classes, `SaltClient` (sync, httpx.Client) and `AsyncSaltClient`
# (async, httpx.AsyncClient), with the same method set and the same request
# shapes -- pick whichever fits your program. `saltapp.agent.Agent` uses
# the async one internally so `ctx.reply()` etc. can be awaited from either
# webhook (ASGI) or socket mode.
from __future__ import annotations

import time
from typing import Any, Mapping, Sequence

import httpx

from saltapp import crypto
from saltapp.errors import SaltApiError
from saltapp.identity import Identity

_JSON = dict[str, Any]


def _cache_bust(path: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}_={int(time.time() * 1000)}"


def _parse_error_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return response.text


def _encryption_recipients(members: Sequence[Mapping[str, Any]], self_agent_id: str) -> list[str]:
    self_id = str(self_agent_id).lower()
    return [
        m["public_key"]
        for m in members
        if str(m.get("id", "")).lower() != self_id and m.get("public_key")
    ]


class SaltClient:
    """Synchronous REST client bound to one Salt deployment's host."""

    def __init__(self, host: str, *, http: httpx.Client | None = None, timeout: float = 30.0) -> None:
        self.host = host.rstrip("/")
        self._owns_http = http is None
        self.http = http or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> "SaltClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- internals --

    def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        json_body: _JSON | None = None,
        *,
        idempotency_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.host}{path}"
        # Open rooms: a falsy api_key (None or "") sends NO api-key header at
        # all, rather than the literal string "None"/"" -- salt-api's
        # try_current_user only treats a header as present when it's
        # non-blank, so this is how an anonymous read of a public,
        # unencrypted room (see get_chat) is expressed. Every call that
        # actually needs auth still fails the normal way (401/403) when the
        # header is missing.
        headers = {"api-key": api_key} if api_key else {}
        if extra_headers:
            headers.update(extra_headers)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = self.http.request(method, url, json=json_body, headers=headers, timeout=timeout)
        if response.status_code >= 400:
            raise SaltApiError(method, url, response.status_code, _parse_error_body(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # -- identity / agent admin --

    def who_am_i(self, api_key: str) -> dict[str, Any]:
        """Which agent does this api key belong to, and its webhook signing
        secret. Cache-busted: the answer changes on rotation/re-keying."""
        return self._request("GET", _cache_bust("/api/v1/agents/webhook_secret"), api_key)

    def get_webhook_secret(self, api_key: str) -> str | None:
        return self.who_am_i(api_key).get("webhook_secret")

    def rotate_webhook_secret(self, api_key: str, agent_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/agents/{agent_id}/rotate_webhook_secret", api_key)

    def rotate_api_key(self, api_key: str, agent_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/agents/{agent_id}/rotate_api_key", api_key)

    def create_agent(self, api_key: str, params: _JSON) -> dict[str, Any]:
        """Register a new Salt agent, owned by whoever's api-key calls this.
        The response carries the new agent's raw api key EXACTLY ONCE --
        capture `api_key` now; a lost key means rotate_api_key, not re-reading."""
        return self._request("POST", "/api/v1/agents", api_key, params)

    def set_callback(self, api_key: str, webhook: str) -> dict[str, Any]:
        """An agent sets its OWN webhook callback (self-service, api-key auth)."""
        return self._request("PATCH", "/api/v1/agents/callback", api_key, {"webhook": webhook})

    def set_delivery_mode(self, api_key: str, mode: str) -> dict[str, Any]:
        """`"webhook"` or `"socket"` -- see saltapp.cable for the real-time
        Action Cable connection `Agent.run_socket_async()` opens once an
        agent is in socket mode."""
        return self._request("PATCH", "/api/v1/agents/delivery", api_key, {"mode": mode})

    def list_agents(self, api_key: str) -> list[dict[str, Any]]:
        return self._request("GET", "/api/v1/agents", api_key)

    def get_agent(self, api_key: str, agent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/agents/{agent_id}", api_key)

    # -- chats / messages --

    def get_chat(self, api_key: str, chat_id: str, *, last: Any = None) -> dict[str, Any]:
        """`last` (a message `seq`) asks for a catch-up window newer than
        that cursor instead of the ten most recent -- see
        Api::V1::ChatsController#show.

        Open rooms: a `public && !encrypted` room is readable with NO
        identity at all -- pass an empty `api_key` ("" or None) and this
        still returns the room read-only (no PGP recipient list, no
        per-viewer membership state; see salt-api's resolve_readable_chat).
        Anything else with a blank api_key -- private, encrypted, or
        nonexistent -- still 404s, indistinguishably, on purpose."""
        path = f"/api/v1/chats/{chat_id}"
        if last is not None:
            path += f"?last={last}"
        return self._request("GET", _cache_bust(path), api_key)

    def get_chat_members(self, api_key: str, chat_id: str) -> list[dict[str, Any]]:
        chat = self.get_chat(api_key, chat_id)
        return ((chat or {}).get("session") or {}).get("users") or []

    def get_chat_messages(self, api_key: str, chat_id: str) -> list[dict[str, Any]]:
        chat = self.get_chat(api_key, chat_id)
        return (chat or {}).get("messages") or []

    def get_attachment(self, api_key: str, message_id: str) -> bytes:
        url = f"{self.host}/api/v1/messages/{message_id}/attachment"
        response = self.http.get(url, headers={"api-key": api_key})
        if response.status_code >= 400:
            raise SaltApiError("GET", url, response.status_code, _parse_error_body(response))
        return response.content

    def post_message(
        self,
        api_key: str,
        chat_id: str,
        message: str,
        sender_message: str | None = None,
        *,
        delegations: list[dict[str, Any]] | None = None,
        mentions: list[str] | None = None,
        quiet: bool = False,
    ) -> dict[str, Any]:
        """Post an already-PGP-encrypted reply into a chat. `message` is the
        ciphertext every human recipient reads; `sender_message` is the copy
        encrypted to the sender's own key (so its own history stays legible)."""
        body: _JSON = {"chat_id": chat_id, "message": message}
        if sender_message is not None:
            body["sender_message"] = sender_message
        if delegations:
            body["delegations"] = delegations
        if mentions:
            body["mentions"] = mentions
        if quiet:
            body["quiet"] = True
        return self._request("POST", "/api/v1/messages", api_key, body)

    def send_message(
        self,
        identity: Identity,
        chat_id: str,
        text: str,
        *,
        mentions: list[str] | None = None,
        quiet: bool = False,
    ) -> dict[str, Any]:
        """Convenience wrapper matching salt-agent-sdk's makeReply: fetches
        the chat's current members, encrypts `text` for every one of them
        (everyone but the sender) plus a copy for the sender's own key, and
        posts it. Raises SaltApiError if no recipient has a public key."""
        members = self.get_chat_members(identity.api_key, chat_id)
        recipient_keys = _encryption_recipients(members, identity.agent_id)
        if not recipient_keys:
            raise SaltApiError(
                "POST", f"{self.host}/api/v1/messages", 0,
                {"error": "no recipient public keys for this chat; not sending."},
            )
        encrypted = crypto.encrypt_for(text, recipient_keys)
        sender_copy = crypto.encrypt_for(text, [identity.public_key])
        return self.post_message(identity.api_key, chat_id, encrypted, sender_copy, mentions=mentions, quiet=quiet)

    def post_plain_message(
        self,
        api_key: str,
        chat_id: str,
        message: str,
        *,
        mentions: list[str] | None = None,
        quiet: bool = False,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Post PLAIN TEXT into an open (`encrypted: false`) room -- no PGP
        anywhere near it, no `sender_message`. `encrypted: false` rides
        explicitly in the request body (rather than just posting
        `message` as-is and hoping the chat happens to be open) so that
        misusing this against an actually-encrypted chat gets salt-api's
        real refusal instead of silently storing plaintext where ciphertext
        was expected: `SaltApiError` carries the server's own sentence,
        "This room is encrypted. Messages must be sent encrypted." (see
        Api::V1::MessagesController#create and saltapp.errors.SaltApiError).
        An open room's own cap on message length
        (`Message::MAX_PLAIN_TEXT_LENGTH`, 4000 chars) is enforced
        server-side, not here."""
        body: _JSON = {"chat_id": chat_id, "message": message, "encrypted": False}
        if mentions:
            body["mentions"] = mentions
        if quiet:
            body["quiet"] = True
        if reply_to_message_id:
            body["reply_to_message_id"] = reply_to_message_id
        return self._request("POST", "/api/v1/messages", api_key, body)

    def get_chat_subscription(self, api_key: str, chat_id: str) -> dict[str, Any]:
        """Interests (open rooms): the caller's OWN follow settings for this
        chat -- `{chat_id, mode, keywords}`, `mode` one of "addressed"
        (today's mention/reply rule -- the default, even before this is
        ever called), "keywords", or "all". Refused 422 ("Salt cannot read
        an encrypted room, so it cannot follow it for you.") against an
        encrypted chat -- see Api::V1::ChatsController#subscription."""
        return self._request("GET", f"/api/v1/chats/{chat_id}/subscription", api_key)

    def set_chat_subscription(
        self, api_key: str, chat_id: str, mode: str, *, keywords: list[str] | None = None
    ) -> dict[str, Any]:
        """Upsert -- idempotent, same shape as the server's
        find_or_initialize_by. `mode`: "addressed" | "keywords" | "all".
        `keywords` only matters in "keywords" mode (server-normalized:
        lowercased, deduped, 2-40 chars each, at most 20)."""
        body: _JSON = {"mode": mode}
        if keywords is not None:
            body["keywords"] = keywords
        return self._request("PUT", f"/api/v1/chats/{chat_id}/subscription", api_key, body)

    def clear_chat_subscription(self, api_key: str, chat_id: str) -> dict[str, Any]:
        """Back to the unwritten default ("addressed"), same shape as never
        having set one."""
        return self._request("DELETE", f"/api/v1/chats/{chat_id}/subscription", api_key)

    def create_or_get_chat(self, api_key: str, contact_id: str) -> dict[str, Any]:
        return self._request("POST", "/api/v1/chats", api_key, {"contact_id": contact_id})

    def open_sidechain(self, api_key: str, chat_id: str, with_id: str) -> dict[str, Any]:
        return self._request("POST", _cache_bust(f"/api/v1/chats/{chat_id}/sidechain"), api_key, {"with_id": with_id})

    def signal_typing(self, api_key: str, chat_id: str) -> None:
        """Ephemeral "is typing" ping. Fire-and-forget by design -- a failed
        ping must never raise into a caller's reply flow."""
        try:
            self._request("POST", f"/api/v1/chats/{chat_id}/typing", api_key, {})
        except Exception:  # noqa: BLE001 -- non-fatal by design
            pass

    # -- cards --

    def post_card(self, api_key: str, chat_id: str, blocks: list[dict[str, Any]], text: str) -> dict[str, Any]:
        return self._request("POST", "/api/v1/cards", api_key, {"chat_id": chat_id, "blocks": blocks, "text": text})

    def update_card(self, api_key: str, card_id: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("PATCH", f"/api/v1/cards/{card_id}", api_key, {"blocks": blocks})

    # -- money: payment requests, invoices, products, usage --

    def request_payment(
        self,
        api_key: str,
        *,
        chat_id: str,
        receiver_id: str,
        wallet_id: str,
        amount: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        """A plain payment request on the TransferRequest rail -- `amount`
        is a HUMAN-DECIMAL string (e.g. "1.50"), never base units."""
        body: _JSON = {"chat_id": chat_id, "receiver_id": receiver_id, "wallet_id": wallet_id, "amount": str(amount)}
        if message is not None:
            body["message"] = message
        return self._request("POST", "/api/v1/transfer_requests", api_key, body)

    def create_invoice(
        self,
        api_key: str,
        *,
        chat_id: str,
        receiver_id: str,
        wallet_id: str,
        amount: str,
        line_items: list[dict[str, Any]],
        message: str | None = None,
        due_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """An itemized invoice, same rail as request_payment plus
        `request_type: "invoice"` + `line_items`. NOTE (ported verbatim from
        client.ts): salt-api's transfer_requests_controller does not
        implement Idempotent-concern dedup for this path -- a retried call
        with the same idempotency_key still creates a second invoice.
        Retries are the caller's own responsibility to guard against."""
        body: _JSON = {
            "chat_id": chat_id,
            "receiver_id": receiver_id,
            "wallet_id": wallet_id,
            "amount": str(amount),
            "request_type": "invoice",
            "line_items": line_items,
        }
        if message is not None:
            body["message"] = message
        if due_at is not None:
            body["due_at"] = due_at
        return self._request("POST", "/api/v1/transfer_requests", api_key, body, idempotency_key=idempotency_key)

    def add_usage(
        self,
        api_key: str,
        *,
        product_id: str,
        chat_id: str,
        qty: float,
        description: str | None = None,
        payer_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Record metered usage against the payer's prepaid credits. Pass
        idempotency_key to make a retry safe; see create_invoice's docstring
        for the contrast -- THIS endpoint does dedupe on it server-side."""
        body: _JSON = {"product_id": product_id, "chat_id": chat_id, "qty": qty}
        if description is not None:
            body["description"] = description
        if payer_id is not None:
            body["payer_id"] = payer_id
        return self._request("POST", "/api/v1/usage_events", api_key, body, idempotency_key=idempotency_key)

    def list_products(self, api_key: str, seller_id: str | None = None) -> list[dict[str, Any]]:
        path = f"/api/v1/products?seller_id={seller_id}" if seller_id else "/api/v1/products"
        return self._request("GET", path, api_key)

    def create_product(self, api_key: str, params: _JSON) -> dict[str, Any]:
        return self._request("POST", "/api/v1/products", api_key, params)

    def share_product(self, api_key: str, product_id: str, chat_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/products/{product_id}/share", api_key, {"chat_id": chat_id})

    def get_transfer(self, api_key: str, transfer_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/transfers/{transfer_id}", api_key)

    # -- hand-offs --

    def hand_off(self, api_key: str, chat_id: str, to_agent_id: str, reason: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/chats/{chat_id}/hand_off", api_key, {"to_agent_id": to_agent_id, "reason": reason})

    def hand_back(self, api_key: str, chat_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/chats/{chat_id}/hand_off/back", api_key)

    # -- socket mode (K2 contract) --

    def get_agent_updates(
        self, api_key: str, *, after: int = 0, timeout: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """The raw REST call behind socket-mode's on-demand drain: `GET
        /api/v1/agent/updates?after=&timeout=&limit=`. Action Cable is the
        real push path (see saltapp.cable); this is the on-demand
        backfill/ack/drain call underneath `saltapp.socket.SocketClient.
        drain_once` -- see that module rather than calling this directly
        in new code. `timeout` defaults to 0 (an immediate answer -- what
        every caller in this SDK actually wants: "what's there right now,"
        never "hold this connection open and wait") and is clamped
        server-side to 0..2s regardless of what's sent; a caller that
        wants the server's old long-poll grace window can still pass
        `timeout=2` explicitly. The underlying HTTP request is still given
        a small margin over `timeout` (added below)."""
        path = f"/api/v1/agent/updates?after={after}&timeout={timeout}&limit={limit}"
        return self._request("GET", path, api_key, timeout=timeout + 10)

    # -- metrics (fire-and-forget) --

    def track_event(self, api_key: str, name: str, properties: _JSON | None = None) -> None:
        try:
            self._request("POST", "/api/v1/events", api_key, {"name": name, "properties": properties or {}})
        except Exception:  # noqa: BLE001 -- an analytics post must never fail a reply
            pass


class AsyncSaltClient:
    """Asynchronous mirror of `SaltClient`, same method set, same request
    shapes -- see that class's docstrings for what each call does."""

    def __init__(self, host: str, *, http: httpx.AsyncClient | None = None, timeout: float = 30.0) -> None:
        self.host = host.rstrip("/")
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def __aenter__(self) -> "AsyncSaltClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        json_body: _JSON | None = None,
        *,
        idempotency_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.host}{path}"
        # See SaltClient._request's comment: a falsy api_key sends no
        # api-key header, the anonymous-read shape get_chat relies on.
        headers = {"api-key": api_key} if api_key else {}
        if extra_headers:
            headers.update(extra_headers)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = await self.http.request(method, url, json=json_body, headers=headers, timeout=timeout)
        if response.status_code >= 400:
            raise SaltApiError(method, url, response.status_code, _parse_error_body(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def who_am_i(self, api_key: str) -> dict[str, Any]:
        return await self._request("GET", _cache_bust("/api/v1/agents/webhook_secret"), api_key)

    async def get_webhook_secret(self, api_key: str) -> str | None:
        return (await self.who_am_i(api_key)).get("webhook_secret")

    async def rotate_webhook_secret(self, api_key: str, agent_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/agents/{agent_id}/rotate_webhook_secret", api_key)

    async def rotate_api_key(self, api_key: str, agent_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/agents/{agent_id}/rotate_api_key", api_key)

    async def create_agent(self, api_key: str, params: _JSON) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/agents", api_key, params)

    async def set_callback(self, api_key: str, webhook: str) -> dict[str, Any]:
        return await self._request("PATCH", "/api/v1/agents/callback", api_key, {"webhook": webhook})

    async def set_delivery_mode(self, api_key: str, mode: str) -> dict[str, Any]:
        return await self._request("PATCH", "/api/v1/agents/delivery", api_key, {"mode": mode})

    async def list_agents(self, api_key: str) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/v1/agents", api_key)

    async def get_agent(self, api_key: str, agent_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/agents/{agent_id}", api_key)

    async def get_chat(self, api_key: str, chat_id: str, *, last: Any = None) -> dict[str, Any]:
        """See SaltClient.get_chat for `last` and the anonymous-read shape
        (blank `api_key` still works for a `public && !encrypted` room)."""
        path = f"/api/v1/chats/{chat_id}"
        if last is not None:
            path += f"?last={last}"
        return await self._request("GET", _cache_bust(path), api_key)

    async def get_chat_members(self, api_key: str, chat_id: str) -> list[dict[str, Any]]:
        chat = await self.get_chat(api_key, chat_id)
        return ((chat or {}).get("session") or {}).get("users") or []

    async def get_chat_messages(self, api_key: str, chat_id: str) -> list[dict[str, Any]]:
        chat = await self.get_chat(api_key, chat_id)
        return (chat or {}).get("messages") or []

    async def get_attachment(self, api_key: str, message_id: str) -> bytes:
        url = f"{self.host}/api/v1/messages/{message_id}/attachment"
        response = await self.http.get(url, headers={"api-key": api_key})
        if response.status_code >= 400:
            raise SaltApiError("GET", url, response.status_code, _parse_error_body(response))
        return response.content

    async def post_message(
        self,
        api_key: str,
        chat_id: str,
        message: str,
        sender_message: str | None = None,
        *,
        delegations: list[dict[str, Any]] | None = None,
        mentions: list[str] | None = None,
        quiet: bool = False,
    ) -> dict[str, Any]:
        body: _JSON = {"chat_id": chat_id, "message": message}
        if sender_message is not None:
            body["sender_message"] = sender_message
        if delegations:
            body["delegations"] = delegations
        if mentions:
            body["mentions"] = mentions
        if quiet:
            body["quiet"] = True
        return await self._request("POST", "/api/v1/messages", api_key, body)

    async def send_message(
        self,
        identity: Identity,
        chat_id: str,
        text: str,
        *,
        mentions: list[str] | None = None,
        quiet: bool = False,
    ) -> dict[str, Any]:
        members = await self.get_chat_members(identity.api_key, chat_id)
        recipient_keys = _encryption_recipients(members, identity.agent_id)
        if not recipient_keys:
            raise SaltApiError(
                "POST", f"{self.host}/api/v1/messages", 0,
                {"error": "no recipient public keys for this chat; not sending."},
            )
        encrypted = crypto.encrypt_for(text, recipient_keys)
        sender_copy = crypto.encrypt_for(text, [identity.public_key])
        return await self.post_message(identity.api_key, chat_id, encrypted, sender_copy, mentions=mentions, quiet=quiet)

    async def post_plain_message(
        self,
        api_key: str,
        chat_id: str,
        message: str,
        *,
        mentions: list[str] | None = None,
        quiet: bool = False,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        """See SaltClient.post_plain_message."""
        body: _JSON = {"chat_id": chat_id, "message": message, "encrypted": False}
        if mentions:
            body["mentions"] = mentions
        if quiet:
            body["quiet"] = True
        if reply_to_message_id:
            body["reply_to_message_id"] = reply_to_message_id
        return await self._request("POST", "/api/v1/messages", api_key, body)

    async def get_chat_subscription(self, api_key: str, chat_id: str) -> dict[str, Any]:
        """See SaltClient.get_chat_subscription."""
        return await self._request("GET", f"/api/v1/chats/{chat_id}/subscription", api_key)

    async def set_chat_subscription(
        self, api_key: str, chat_id: str, mode: str, *, keywords: list[str] | None = None
    ) -> dict[str, Any]:
        """See SaltClient.set_chat_subscription."""
        body: _JSON = {"mode": mode}
        if keywords is not None:
            body["keywords"] = keywords
        return await self._request("PUT", f"/api/v1/chats/{chat_id}/subscription", api_key, body)

    async def clear_chat_subscription(self, api_key: str, chat_id: str) -> dict[str, Any]:
        """See SaltClient.clear_chat_subscription."""
        return await self._request("DELETE", f"/api/v1/chats/{chat_id}/subscription", api_key)

    async def create_or_get_chat(self, api_key: str, contact_id: str) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/chats", api_key, {"contact_id": contact_id})

    async def open_sidechain(self, api_key: str, chat_id: str, with_id: str) -> dict[str, Any]:
        return await self._request("POST", _cache_bust(f"/api/v1/chats/{chat_id}/sidechain"), api_key, {"with_id": with_id})

    async def signal_typing(self, api_key: str, chat_id: str) -> None:
        try:
            await self._request("POST", f"/api/v1/chats/{chat_id}/typing", api_key, {})
        except Exception:  # noqa: BLE001
            pass

    async def post_card(self, api_key: str, chat_id: str, blocks: list[dict[str, Any]], text: str) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/cards", api_key, {"chat_id": chat_id, "blocks": blocks, "text": text})

    async def update_card(self, api_key: str, card_id: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._request("PATCH", f"/api/v1/cards/{card_id}", api_key, {"blocks": blocks})

    async def request_payment(
        self,
        api_key: str,
        *,
        chat_id: str,
        receiver_id: str,
        wallet_id: str,
        amount: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        body: _JSON = {"chat_id": chat_id, "receiver_id": receiver_id, "wallet_id": wallet_id, "amount": str(amount)}
        if message is not None:
            body["message"] = message
        return await self._request("POST", "/api/v1/transfer_requests", api_key, body)

    async def create_invoice(
        self,
        api_key: str,
        *,
        chat_id: str,
        receiver_id: str,
        wallet_id: str,
        amount: str,
        line_items: list[dict[str, Any]],
        message: str | None = None,
        due_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: _JSON = {
            "chat_id": chat_id,
            "receiver_id": receiver_id,
            "wallet_id": wallet_id,
            "amount": str(amount),
            "request_type": "invoice",
            "line_items": line_items,
        }
        if message is not None:
            body["message"] = message
        if due_at is not None:
            body["due_at"] = due_at
        return await self._request("POST", "/api/v1/transfer_requests", api_key, body, idempotency_key=idempotency_key)

    async def add_usage(
        self,
        api_key: str,
        *,
        product_id: str,
        chat_id: str,
        qty: float,
        description: str | None = None,
        payer_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: _JSON = {"product_id": product_id, "chat_id": chat_id, "qty": qty}
        if description is not None:
            body["description"] = description
        if payer_id is not None:
            body["payer_id"] = payer_id
        return await self._request("POST", "/api/v1/usage_events", api_key, body, idempotency_key=idempotency_key)

    async def list_products(self, api_key: str, seller_id: str | None = None) -> list[dict[str, Any]]:
        path = f"/api/v1/products?seller_id={seller_id}" if seller_id else "/api/v1/products"
        return await self._request("GET", path, api_key)

    async def create_product(self, api_key: str, params: _JSON) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/products", api_key, params)

    async def share_product(self, api_key: str, product_id: str, chat_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/products/{product_id}/share", api_key, {"chat_id": chat_id})

    async def get_transfer(self, api_key: str, transfer_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/transfers/{transfer_id}", api_key)

    async def hand_off(self, api_key: str, chat_id: str, to_agent_id: str, reason: str | None = None) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/chats/{chat_id}/hand_off", api_key, {"to_agent_id": to_agent_id, "reason": reason})

    async def hand_back(self, api_key: str, chat_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/chats/{chat_id}/hand_off/back", api_key)

    async def get_agent_updates(
        self, api_key: str, *, after: int = 0, timeout: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """See SaltClient.get_agent_updates."""
        path = f"/api/v1/agent/updates?after={after}&timeout={timeout}&limit={limit}"
        return await self._request("GET", path, api_key, timeout=timeout + 10)

    async def track_event(self, api_key: str, name: str, properties: _JSON | None = None) -> None:
        try:
            await self._request("POST", "/api/v1/events", api_key, {"name": name, "properties": properties or {}})
        except Exception:  # noqa: BLE001
            pass
