# Push, not poll (2026-09-22, owner via team lead: "DO NOT USE POLLING as a
# mechanic EVER: pull on demand, push on address"). `CableClient` opens a
# real websocket to salt-api's Action Cable (`AgentUpdatesChannel`) and
# stays connected; salt-api PUSHES each envelope the instant it's written.
# An idle, caught-up agent makes ZERO requests -- there is no interval
# timer anywhere in this module. `saltapp.socket.SocketClient.drain_once`
# is reused here ONLY for the on-demand backfill below (never as this
# client's primary transport -- see BACKFILL) -- this module is what
# `saltapp.agent.Agent.run_socket_async` actually runs.
#
# Ported from salt-agent-sdk/src/socket.ts's createSocketClient (K2 socket
# mode, design-fleet/runs/2026-09-17-distribution/LANES.md, then rewritten
# onto Action Cable 2026-09-22 -- see that file's own header for the full
# protocol narrative). This module implements the same wire contract with
# one deliberate simplification and one deliberate strengthening, both
# noted below.
#
# WIRE PROTOCOL (`wss://<host>/cable`, this agent's own `api-key` on the
# handshake HEADER -- never a query param, same ALB/CloudWatch-access-log
# reasoning the REST api-key path documents; see salt-api's
# ApplicationCable::Connection and AgentUpdatesChannel):
#   1. Connect. Server sends {type:"welcome"}.
#   2. Subscribe: {command:"subscribe", identifier: json.dumps({channel:
#      "AgentUpdatesChannel", after: <local cursor>})} -- `after` OMITTED
#      entirely when there is no local cursor yet, so salt-api's own
#      server-side ack applies instead of replaying from scratch.
#   3. Server replies {type:"confirm_subscription"} or
#      {type:"reject_subscription"}, then REPLAYS the backlog from the
#      resolved cursor as ordinary envelope frames -- {identifier, message:
#      {id, delivery_id, event, headers, body, created_at}} -- ending with
#      {identifier, message: {type:"replay_done", cursor, more?}}. Live
#      broadcasts arrive as that identical envelope shape, interleaved with
#      (and continuing after) the replay.
#   4. {type:"ping", message:<unix ts>} arrives roughly every 3s -- the
#      liveness signal. No ping for PING_TIMEOUT_SECONDS means the
#      connection is presumed dead (a half-open TCP socket may never emit
#      its own close) -- torn down and reconnected.
#   5. {type:"disconnect", reason, reconnect} means salt-api is closing this
#      connection on purpose (e.g. a deploy) -- the close that follows
#      drives reconnect the same as any other close, regardless of the
#      `reconnect` value.
#
# CURSOR PERSISTENCE: the local cursor_store is written ONLY from a
# `replay_done` frame's `cursor` or a backfill page's `cursor` -- NEVER
# from a live envelope frame's own `id` directly. Two different live
# broadcasts for the same agent have no cross-broadcast ordering guarantee
# (different Puma processes/transactions), so persisting from whichever
# arrived first risks writing a cursor ahead of one still in flight, which
# a later reconnect would then never replay. Live frames are still
# DISPATCHED the moment they arrive -- this rule is only about what gets
# written to disk / used to resume.
#
# ACK: after catching up (the first replay_done seen on a connection,
# whether or not it needed backfill), every further row processed schedules
# a single, coalesced `GET /api/v1/agent/updates?after=<highest processed
# id>&timeout=0&limit=1` -- there is no dedicated ack action on the
# channel; `after` on that endpoint already IS salt-api's ack (see
# AgentUpdatesController). Event-driven: at most one ack in flight, and at
# most one more queued behind it for whatever arrived while it was out --
# never a timer.
#
# BACKFILL: `replay_done.more: true` means AgentUpdatesChannel's own
# MAX_BACKLOG_REPLAY (500 rows) truncated the backlog. `_backfill` reaches
# for `saltapp.socket.SocketClient.drain_once` (`self._drain`, sharing this
# instance's own cursor_store/dedupe_store) ONLY here, ONLY when `more`
# said so -- one on-demand call that itself pages `GET /api/v1/agent/
# updates?after=<cursor>&timeout=0` until a page comes back empty. Never on
# an interval, never as a standing loop.
#
# DELIBERATE SIMPLIFICATION vs. the TS reference: that client buffers any
# live frame arriving WHILE a backfill's HTTP round trips are in flight, so
# it can drain them in strict id order once backfill finishes. This client
# does not buffer -- it processes every frame in arrival order, including
# ones interleaved with backfill, and relies on the SAME safety net the TS
# reference's own comment names for why the buffering is optional in the
# first place: DedupeStore makes any overlap between a live frame and a
# backfilled row harmless either way. What buffering buys is stricter
# ordering, not correctness; the reference implementation duplicates every
# row from `saltapp.webhook.handle`'s HMAC check regardless of ordering, so
# a delivery is verified, deduped, and dispatched exactly once either way.
#
# DELIBERATE STRENGTHENING vs. the TS reference: `on_event` is dispatched
# via `asyncio.create_task` rather than awaited inline in the frame-reading
# loop. `saltapp.agent.Agent.dispatch` can call `ctx.ask()`/`ctx.approve()`,
# which blocks a handler on a REPLY that arrives over this very connection
# -- the TS SDK has no such primitive as of this writing (see agent.py's
# module docstring), so its inline-await shape never has to worry about a
# handler blocking on input that can only arrive by this same loop
# continuing to run. Scheduling dispatch as its own task keeps frames
# flowing while a handler waits.
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from saltapp.client import AsyncSaltClient
from saltapp.socket import (
    DEFAULT_DRAIN_LIMIT,
    SOCKET_SIGNATURE_TOLERANCE_SECONDS,
    CursorStore,
    DedupeStore,
    FileCursorStore,
    FileDedupeStore,
    SocketClient,
    default_state_dir,
)
from saltapp.webhook import Event, WebhookVerificationError, handle

# Exponential reconnect backoff bounds (jittered) -- only the wait between
# one dropped/closed connection and the next attempt. Not a poll interval.
RECONNECT_MIN_DELAY_SECONDS = 1.0
RECONNECT_MAX_DELAY_SECONDS = 60.0

# No Action Cable {type:"ping"} for this long means the connection is
# presumed dead (a half-open TCP socket may never emit its own close).
PING_TIMEOUT_SECONDS = 30.0

OnEvent = Callable[[Event], Awaitable[None]]

# What one verified/deduped row's handling came to -- "advance" covers
# dispatched, deduped-skip, AND a definitive rejection (bad signature);
# "halt" is a transient verification failure (no signing secret available,
# or the lookup itself raised) that must not be treated as processed.
_RowStatus = str


class CableHandshakeRejected(Exception):
    """The websocket handshake itself was refused (a non-101 HTTP
    response) -- most notably a 429 from the same rate limiter that guards
    every other endpoint (rack_attack.rb applies to `/cable` too).
    `retry_after_seconds`, when the server sent one, overrides the
    ordinary reconnect backoff for this one wait, same as everywhere else
    in this SDK that honours Retry-After."""

    def __init__(self, status_code: int, retry_after_seconds: float | None = None) -> None:
        super().__init__(f"cable handshake rejected: HTTP {status_code}")
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    now = datetime.now(dt.tzinfo or timezone.utc)
    return max(0.0, (dt - now).total_seconds())


def _jitter(seconds: float) -> float:
    # Half fixed, half random -- avoids a thundering herd of identical
    # reconnect timings without making the wait unpredictably short.
    return seconds / 2 + random.random() * (seconds / 2)


def cable_url(host: str) -> str:
    """`https://saltapp.ai` -> `wss://saltapp.ai/cable` (and the http/ws
    equivalent for local dev). No trailing slash required either way."""
    trimmed = host.rstrip("/")
    if trimmed.startswith("https://"):
        return "wss://" + trimmed[len("https://") :] + "/cable"
    if trimmed.startswith("http://"):
        return "ws://" + trimmed[len("http://") :] + "/cable"
    return trimmed + "/cable"


class _WebsocketsConnector:
    """Default production connector: a thin async context manager around
    `websockets.connect`, translating a rejected handshake into
    CableHandshakeRejected so the rest of this module never needs to know
    which websocket library is in use -- a test's fake connector raises
    the same exception directly, with no `websockets` import needed at
    all."""

    def __init__(self, uri: str, headers: dict[str, str]) -> None:
        self._uri = uri
        self._headers = headers
        self._cm: Any = None

    async def __aenter__(self) -> Any:
        import websockets
        from websockets.exceptions import InvalidStatus

        self._cm = websockets.connect(self._uri, additional_headers=self._headers)
        try:
            return await self._cm.__aenter__()
        except InvalidStatus as exc:
            status = exc.response.status_code
            retry_after = _parse_retry_after(exc.response.headers.get("Retry-After"))
            raise CableHandshakeRejected(status, retry_after) from exc

    async def __aexit__(self, *exc_info: object) -> Any:
        if self._cm is not None:
            return await self._cm.__aexit__(*exc_info)
        return False


Connector = Callable[[str, dict[str, str]], Any]


class CableClient:
    """Stays connected to salt-api's `AgentUpdatesChannel` over Action
    Cable, verifying each envelope with the exact same HMAC check
    `saltapp.webhook.handle` applies to a webhook POST, and yielding
    verified `Event`s to `on_event` in `run()` -- what `saltapp.agent.
    Agent.run_socket_async` actually runs. Transport concern only: dedupe
    against the server-side event families, the mention rule, and loop
    guards live in `saltapp.agent.Agent`. `saltapp.socket.SocketClient.
    drain_once` is reused for the on-demand backfill only (see BACKFILL
    in this module's header) -- there is no other polling anywhere.
    """

    def __init__(
        self,
        client: AsyncSaltClient,
        api_key: str,
        *,
        agent_id: str = "",
        webhook_secret: str | None = None,
        webhook_secret_provider: Callable[[], Optional[str]] | None = None,
        verify_signatures: bool = True,
        tolerance_seconds: int = SOCKET_SIGNATURE_TOLERANCE_SECONDS,
        cursor_store: CursorStore | None = None,
        dedupe_store: DedupeStore | None = None,
        backfill_limit: int = DEFAULT_DRAIN_LIMIT,
        min_backoff: float = RECONNECT_MIN_DELAY_SECONDS,
        max_backoff: float = RECONNECT_MAX_DELAY_SECONDS,
        ping_timeout: float = PING_TIMEOUT_SECONDS,
        connector: Connector | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.api_key = api_key
        self.agent_id = agent_id
        self._static_secret = webhook_secret
        self._secret_provider = webhook_secret_provider
        self.verify_signatures = verify_signatures
        self.tolerance_seconds = tolerance_seconds
        self.cursor_store = cursor_store if cursor_store is not None else FileCursorStore(default_state_dir(agent_id) / "cursor.json")
        self.dedupe_store = dedupe_store if dedupe_store is not None else FileDedupeStore(default_state_dir(agent_id) / "seen.json")
        self.backfill_limit = backfill_limit
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self.ping_timeout = ping_timeout
        self._connector: Connector = connector or (lambda uri, headers: _WebsocketsConnector(uri, headers))
        self._logger = logger or logging.getLogger("saltapp.cable")

        # On-demand backfill only (see _backfill below) -- reuses this
        # instance's OWN cursor_store/dedupe_store (not a second, separate
        # pair pointed at the same files) so a row seen via a backfill page
        # and a row seen via a live frame share one dedupe set and one
        # cursor, never two independently-cached views of the same state.
        self._drain = SocketClient(
            client, api_key,
            agent_id=agent_id,
            webhook_secret=webhook_secret,
            webhook_secret_provider=webhook_secret_provider,
            verify_signatures=verify_signatures,
            tolerance_seconds=tolerance_seconds,
            cursor_store=self.cursor_store,
            dedupe_store=self.dedupe_store,
            limit=backfill_limit,
            logger=self._logger,
        )

        self._stopped = True
        self._highest_processed = 0
        self._ack_task: asyncio.Task | None = None
        self._ack_pending = False
        # Set once per run() call, before the connection loop starts --
        # _run_ack (scheduled from inside _one_connection, which only ever
        # runs inside run()) always finds this populated.
        self._current_on_event: OnEvent | None = None

    def _secret(self) -> str | None:
        if self._secret_provider is not None:
            return self._secret_provider()
        return self._static_secret

    # -- one row: verify + dedupe (transport-only; dispatch happens in the
    # -- caller, via _process_row/_backfill, as its own task) -------------

    async def _handle_row(self, row: dict[str, Any]) -> tuple[_RowStatus, Event | None]:
        headers = row.get("headers") or {}
        raw_body = row.get("body")
        raw_bytes = raw_body.encode("utf-8") if isinstance(raw_body, str) else json.dumps(raw_body or {}).encode("utf-8")

        secret = None
        if self.verify_signatures:
            try:
                secret = self._secret()
            except Exception as exc:  # noqa: BLE001 -- a network blip fetching the secret
                self._logger.error("[cable] fetching signing secret failed (transient) for update id=%s: %s", row.get("id"), exc)
                return "halt", None
            if not secret:
                self._logger.error("[cable] no signing secret available yet for update id=%s (transient)", row.get("id"))
                return "halt", None

        try:
            event = handle(headers, raw_bytes, secret=secret, verify=self.verify_signatures, tolerance_seconds=self.tolerance_seconds)
        except WebhookVerificationError as exc:
            self._logger.error("[cable] rejected update id=%s: %s", row.get("id"), exc)
            return "advance", None

        if row.get("delivery_id") is not None:
            event.delivery_id = str(row["delivery_id"])
        if row.get("created_at") is not None:
            event.created_at = str(row["created_at"])

        if event.delivery_id:
            try:
                already_seen = self.dedupe_store.has(event.delivery_id)
            except Exception as exc:  # noqa: BLE001 -- fail open: a lookup failure must not block real delivery
                self._logger.error("[cable] dedupe lookup for %s failed (failing open): %s", event.delivery_id, exc)
                already_seen = False
            if already_seen:
                return "advance", None
            try:
                self.dedupe_store.add(event.delivery_id)
            except Exception as exc:  # noqa: BLE001
                self._logger.error("[cable] recording dedupe for %s failed: %s", event.delivery_id, exc)

        return "advance", event

    def _note_processed(self, row_id: Any) -> None:
        if isinstance(row_id, int) and row_id > self._highest_processed:
            self._highest_processed = row_id

    def _persist_cursor(self, cursor: Any) -> None:
        if not isinstance(cursor, int):
            return
        try:
            self.cursor_store.set(cursor)
        except Exception as exc:  # noqa: BLE001
            self._logger.error("[cable] persisting cursor %s failed: %s", cursor, exc)

    async def _process_row(self, row: dict[str, Any], on_event: OnEvent) -> None:
        status, event = await self._handle_row(row)
        if status != "advance":
            return
        self._note_processed(row.get("id"))
        if event is not None:
            asyncio.create_task(self._safe_call(on_event, event))

    async def _safe_call(self, on_event: OnEvent, event: Event) -> None:
        try:
            await on_event(event)
        except Exception as exc:  # noqa: BLE001
            self._logger.error("[cable] handler failed for delivery %s: %s", event.delivery_id, exc)

    # -- ack (coalesced, event-driven) -------------------------------------

    def _schedule_ack(self) -> None:
        if self._stopped or self._highest_processed == 0:
            return
        if self._ack_task is not None and not self._ack_task.done():
            self._ack_pending = True
            return
        self._ack_task = asyncio.create_task(self._run_ack())

    async def _run_ack(self) -> None:
        after = self._highest_processed
        try:
            response = await self.client.get_agent_updates(self.api_key, after=after, timeout=0, limit=1)
        except Exception as exc:  # noqa: BLE001
            self._logger.error("[cable] ack request failed: %s", exc)
        else:
            for row in response.get("updates") or []:
                status, event = await self._handle_row(row)
                if status == "advance":
                    self._note_processed(row.get("id"))
                    if event is not None:
                        asyncio.create_task(self._safe_call(self._current_on_event, event))
            cursor = response.get("cursor")
            self._persist_cursor(cursor if isinstance(cursor, int) else after)
        finally:
            if self._ack_pending:
                self._ack_pending = False
                self._schedule_ack()

    # -- backfill (replay_done.more only -- ON DEMAND, never on an interval) --

    async def _backfill(self, on_event: OnEvent, stop: Optional[asyncio.Event] = None) -> None:
        """Only reached when a `replay_done` frame said AgentUpdatesChannel's
        own MAX_BACKLOG_REPLAY (500 rows) truncated the backlog -- drains
        the rest via `saltapp.socket.SocketClient.drain_once` (this
        instance's `self._drain`, sharing the SAME cursor_store/
        dedupe_store as the live-frame path), retried with backoff on a
        transport failure. `drain_once` already pages until a page comes
        back empty and persists the cursor as it goes, so one call here
        either finishes the whole backfill or raises; the cursor is
        already seeded correctly before this is ever called (the caller
        persists `replay_done`'s own cursor first -- see _one_connection),
        so `drain_once`'s default (`after=None` -> its own cursor_store
        position) is exactly right and there's nothing to pass it."""
        backoff = self.min_backoff
        while not self._stopped and (stop is None or not stop.is_set()):
            try:
                events = await self._drain.drain_once()
            except Exception as exc:  # noqa: BLE001
                self._logger.error("[cable] backfill request failed: %s; retrying in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            self._note_processed(self.cursor_store.get())
            for event in events:
                asyncio.create_task(self._safe_call(on_event, event))

            # self._drain.exhausted distinguishes "genuinely caught up"
            # from "stopped early on a transient halt" -- an empty
            # `events` list alone can't (see SocketClient.drain_once's
            # docstring); only the former means this backfill is done.
            if self._drain.exhausted:
                return

            self._logger.error("[cable] backfill halted on a transient failure; retrying in %.1fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    # -- one websocket connection's lifecycle ------------------------------

    async def _one_connection(self, on_event: OnEvent, stop: Optional[asyncio.Event]) -> tuple[Optional[float], bool]:
        try:
            local_cursor = self.cursor_store.get()
        except Exception as exc:  # noqa: BLE001
            self._logger.error("[cable] loading cursor failed, starting from 0: %s", exc)
            local_cursor = 0

        url = cable_url(self.client.host)
        headers = {"api-key": self.api_key}
        subscribed = False
        caught_up = False

        try:
            connection = self._connector(url, headers)
        except CableHandshakeRejected as exc:
            self._logger.error("[cable] handshake failed: HTTP %s", exc.status_code)
            return exc.retry_after_seconds, False

        try:
            async with connection as ws:
                identifier: dict[str, Any] = {"channel": "AgentUpdatesChannel"}
                if local_cursor > 0:
                    identifier["after"] = local_cursor
                await ws.send(json.dumps({"command": "subscribe", "identifier": json.dumps(identifier)}))

                last_ping = time.monotonic()
                while stop is None or not stop.is_set():
                    remaining = self.ping_timeout - (time.monotonic() - last_ping)
                    if remaining <= 0:
                        self._logger.error("[cable] no ping for %.0fs; treating the connection as dead", self.ping_timeout)
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 1.0))
                    except asyncio.TimeoutError:
                        continue

                    try:
                        frame = json.loads(raw)
                    except (ValueError, TypeError) as exc:
                        self._logger.error("[cable] unparseable frame: %s", exc)
                        continue

                    ftype = frame.get("type")
                    if ftype in ("ping", "welcome"):
                        last_ping = time.monotonic()
                        continue
                    if ftype == "confirm_subscription":
                        subscribed = True
                        last_ping = time.monotonic()
                        self._logger.info("[cable] subscribed as agent %s (cursor %s)", self.agent_id or "(unknown id)", local_cursor)
                        continue
                    if ftype == "reject_subscription":
                        self._logger.error("[cable] subscription rejected; reconnecting")
                        break
                    if ftype == "disconnect":
                        reason = frame.get("reason")
                        self._logger.info("[cable] server requested disconnect%s", f" ({reason})" if reason else "")
                        continue

                    payload = frame.get("message")
                    if not isinstance(payload, dict):
                        continue

                    if payload.get("type") == "replay_done":
                        server_cursor = payload.get("cursor")
                        cursor_to_persist = server_cursor if isinstance(server_cursor, int) else local_cursor
                        self._persist_cursor(cursor_to_persist)
                        if payload.get("more"):
                            await self._backfill(on_event, stop)
                        caught_up = True
                        continue

                    if "id" not in payload:
                        continue

                    await self._process_row(payload, on_event)
                    if caught_up:
                        self._schedule_ack()
        except CableHandshakeRejected as exc:
            return exc.retry_after_seconds, subscribed
        except Exception as exc:  # noqa: BLE001 -- any transport error; reconnect
            self._logger.error("[cable] connection error: %s", exc)

        return None, subscribed

    # -- the public loop ----------------------------------------------------

    async def run(self, on_event: OnEvent, *, stop: asyncio.Event | None = None) -> None:
        """Connects and stays connected (until `stop` is set), reconnecting
        with jittered exponential backoff on any close. Each verified event
        is dispatched via `on_event` as its own task (see this module's
        header comment for why) -- new frames, including a reply an
        `ctx.ask()` is waiting on, keep arriving while a handler runs."""
        self._stopped = False
        self._current_on_event = on_event
        backoff = self.min_backoff
        self._logger.info("[cable] connecting to %s as agent %s", cable_url(self.client.host), self.agent_id or "(unknown id)")
        try:
            while stop is None or not stop.is_set():
                retry_after, subscribed = await self._one_connection(on_event, stop)
                if stop is not None and stop.is_set():
                    break
                if subscribed:
                    backoff = self.min_backoff  # a clean connection resets the failure backoff
                wait = retry_after if retry_after is not None else _jitter(backoff)
                self._logger.error(
                    "[cable] reconnecting in %.1fs%s", wait, " (Retry-After)" if retry_after is not None else ""
                )
                if stop is not None:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=wait)
                        break  # stop was set while waiting out the backoff
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(wait)
                backoff = min(backoff * 2, self.max_backoff)
        finally:
            self._stopped = True
            if self._ack_task is not None:
                try:
                    await self._ack_task
                except Exception:  # noqa: BLE001
                    pass
