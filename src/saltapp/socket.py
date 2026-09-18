# Socket-mode long-poll client, per the K2 contract (an agent with no
# public URL receives exactly what a webhook would have delivered):
#
#   GET /api/v1/agent/updates?after=<cursor>&timeout=<0..25>&limit=<1..100>
#   -> 200 {updates: [{id, delivery_id, event, headers, body, created_at}],
#           cursor: <last id or after>}
#
# Each update's `headers`/`body` are exactly what the equivalent webhook
# POST would have carried (including a real X-Salt-Signature), so this
# module verifies every envelope with the same HMAC check `saltapp.webhook`
# applies to an actual webhook -- a relay sitting between this client and
# salt-api cannot forge an update, only replay or drop one, and a stale or
# tampered envelope is silently dropped (logged, never delivered).
#
# Transport errors back off (capped exponential); a successful poll resets
# the backoff. The cursor is the ack: `saltapp.client`'s `after` on the next
# call is only advanced once this class has finished handing that batch to
# the caller.
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol

from saltapp.client import AsyncSaltClient
from saltapp.webhook import Event, WebhookVerificationError, handle


class CursorStore(Protocol):
    def get(self) -> int: ...
    def set(self, cursor: int) -> None: ...


class MemoryCursorStore:
    """Lost on restart -- fine for a quick script or a test."""

    def __init__(self, start: int = 0) -> None:
        self._cursor = start

    def get(self) -> int:
        return self._cursor

    def set(self, cursor: int) -> None:
        self._cursor = cursor


class FileCursorStore:
    """Persists the cursor as plain text in one file, so a restart resumes
    from where it left off instead of re-delivering (or, worse, silently
    skipping) up to 7 days of retained updates."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def get(self) -> int:
        try:
            return int(self.path.read_text().strip() or "0")
        except (FileNotFoundError, ValueError):
            return 0

    def set(self, cursor: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(cursor))


@dataclass
class _Backoff:
    initial: float = 1.0
    maximum: float = 30.0
    factor: float = 2.0

    def __post_init__(self) -> None:
        self._current = self.initial

    def next(self) -> float:
        wait = self._current
        self._current = min(self._current * self.factor, self.maximum)
        return wait

    def reset(self) -> None:
        self._current = self.initial


OnEvent = Callable[[Event], Awaitable[None]]


class SocketClient:
    """Long-polls `GET /api/v1/agent/updates` and yields verified `Event`s,
    in cursor order. Transport concern only -- dedupe, the mention rule and
    loop guards live in `saltapp.agent.Agent`, which is the intended caller.
    """

    def __init__(
        self,
        client: AsyncSaltClient,
        api_key: str,
        *,
        webhook_secret: str | None = None,
        webhook_secret_provider: Callable[[], Optional[str]] | None = None,
        verify_signatures: bool = True,
        tolerance_seconds: int = 300,
        cursor_store: CursorStore | None = None,
        poll_timeout: int = 25,
        poll_limit: int = 100,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.api_key = api_key
        self._static_secret = webhook_secret
        self._secret_provider = webhook_secret_provider
        self.verify_signatures = verify_signatures
        self.tolerance_seconds = tolerance_seconds
        self.cursor_store = cursor_store or MemoryCursorStore()
        self.poll_timeout = poll_timeout
        self.poll_limit = poll_limit
        self._logger = logger or logging.getLogger("saltapp.socket")
        self._backoff = _Backoff()

    def _secret(self) -> str | None:
        if self._secret_provider is not None:
            return self._secret_provider()
        return self._static_secret

    def _verify_and_parse(self, update: dict) -> Event | None:
        headers = update.get("headers") or {}
        raw_body = update.get("body")
        if isinstance(raw_body, str):
            raw_bytes = raw_body.encode("utf-8")
        else:
            raw_bytes = json.dumps(raw_body or {}).encode("utf-8")
        try:
            event = handle(
                headers,
                raw_bytes,
                secret=self._secret(),
                verify=self.verify_signatures,
                tolerance_seconds=self.tolerance_seconds,
            )
        except WebhookVerificationError as exc:
            self._logger.error("[socket] dropping update id=%s: %s", update.get("id"), exc)
            return None
        if update.get("delivery_id") is not None:
            event.delivery_id = str(update["delivery_id"])
        if update.get("created_at") is not None:
            event.created_at = str(update["created_at"])
        return event

    async def poll_once(self) -> list[Event]:
        """One long-poll round-trip. Returns zero or more verified events,
        in the order the server returned them, and advances the cursor
        store past all of them (even ones dropped for a bad signature --
        those envelopes still had a real id and must not be re-delivered
        forever). On a transport error, sleeps for the current backoff
        delay, extends it, and returns an empty list; a successful call
        resets the backoff.
        """
        after = self.cursor_store.get()
        try:
            response = await self.client.get_agent_updates(
                self.api_key, after=after, timeout=self.poll_timeout, limit=self.poll_limit
            )
        except Exception as exc:  # noqa: BLE001 -- any transport/HTTP failure
            self._logger.error("[socket] poll failed: %s", exc)
            await asyncio.sleep(self._backoff.next())
            return []

        self._backoff.reset()
        updates = response.get("updates") or []
        cursor = response.get("cursor", after)

        events: list[Event] = []
        for update in updates:
            event = self._verify_and_parse(update)
            if event is not None:
                events.append(event)

        self.cursor_store.set(cursor)
        return events

    async def run(self, on_event: OnEvent, *, stop: asyncio.Event | None = None) -> None:
        """Poll forever (until `stop` is set). Each event's `on_event` call
        is scheduled as its own task rather than awaited inline, so one
        handler blocked in `ctx.ask()` (see saltapp.agent) never stalls the
        poll loop -- new updates, including the answer that handler is
        waiting on, keep arriving.
        """
        while stop is None or not stop.is_set():
            events = await self.poll_once()
            for event in events:
                asyncio.create_task(self._safe_call(on_event, event))
            if not events:
                await asyncio.sleep(0)  # yield to scheduled tasks before the next long poll

    async def _safe_call(self, on_event: OnEvent, event: Event) -> None:
        try:
            await on_event(event)
        except Exception as exc:  # noqa: BLE001
            self._logger.error("[socket] handler failed for delivery %s: %s", event.delivery_id, exc)
