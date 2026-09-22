# Socket-mode short-poll client, per the K2 contract (an agent with no
# public URL receives exactly what a webhook would have delivered).
#
# AS OF 2026-09-22, `saltapp.agent.Agent.run_socket_async()` no longer uses
# `SocketClient` -- it runs on `saltapp.cable.CableClient`, a real Action
# Cable websocket connection, per the owner's ruling (via team lead): "DO
# NOT USE POLLING as a mechanic EVER: pull on demand, push on address."
# `SocketClient` stays here, unchanged, as a low-level fallback primitive
# (and because `saltapp.cable` reuses its CursorStore/DedupeStore
# implementations below) -- it is not what a new integration should reach
# for to receive events.
#
#   GET /api/v1/agent/updates?after=<cursor>&timeout=<0..2>&limit=<1..100>
#   -> 200 {updates: [{id, delivery_id, event, headers, body, created_at}],
#           cursor: <last id or after>}
#
# REVISED 2026-09-18 after a security review of the reference TS
# implementation (design-fleet/runs/2026-09-17-distribution/LANES.md's
# "Socket mode contract" section, salt-agent-sdk/src/socket.ts):
#   - `timeout` is clamped server-side to 0..2s regardless of what's sent
#     -- Action Cable is the real push path; this endpoint is a
#     fallback/backlog-catch-up, polled ADAPTIVELY (ACTIVE_POLL_DELAY_SECONDS
#     right after real activity, backing off one step at a time toward
#     IDLE_POLL_DELAY_SECONDS the longer nothing shows up).
#   - Verification tolerance is the SAME ~300s window the webhook path
#     uses (SOCKET_SIGNATURE_TOLERANCE_SECONDS). An outbox row can sit
#     unpolled for days, but it is re-signed at SERVE time with the agent's
#     current webhook secret, so what this client receives is always
#     freshly stamped. Replay protection still comes from the cursor PLUS a
#     persistent per-agent delivery_id dedupe set (DedupeStore), never from
#     the timestamp -- the timestamp is a staleness bound, not the defence.
#   - A verification failure that LOOKS transient (no signing secret
#     available yet, or the lookup itself raised -- a network blip, not a
#     bad/forged signature) halts the batch and does NOT advance the
#     cursor past that row: retried with backoff instead, or a real update
#     sitting behind a transient failure would be silently skipped
#     forever. A DEFINITIVE rejection (bad signature, malformed header,
#     genuinely stale timestamp) still advances past it -- retrying a
#     forged/bad envelope forever would just wedge the client on it.
#   - The default cursor AND dedupe stores are now FILE-based, at
#     `~/.salt/agents/<agentId>/cursor.json` and `.../seen.json` (dirs
#     0700, files 0600) -- memory is opt-in, not the default, since
#     silently losing both on every restart is exactly the kind of thing
#     that should be a deliberate choice. Pass `MemoryCursorStore()`/
#     `MemoryDedupeStore()` explicitly to opt out (tests must always do
#     this, so they never write to the real home directory).
#
# Each update's `headers`/`body` are exactly what the equivalent webhook
# POST would have carried (including a real X-Salt-Signature), so this
# module verifies every envelope with the same HMAC check `saltapp.webhook`
# applies to an actual webhook -- a relay sitting between this client and
# salt-api cannot forge an update, only replay or drop one.
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol

from saltapp.client import AsyncSaltClient
from saltapp.webhook import Event, WebhookVerificationError, handle

# The SAME ~300s window the webhook path uses. This was once RETENTION
# (7 days) + an hour, on the reasoning that an envelope can sit unpolled in
# the outbox for days, so its signing timestamp would routinely look stale.
# That reasoning stopped being true when salt-api moved to SERVE-TIME
# signing (LANES.md "fix A", round 3): an envelope is re-signed with the
# agent's current webhook secret at the moment it is served, so its
# timestamp is always fresh relative to when this client actually receives
# it -- measured at ~1.1s on the live production gate, not days. Keeping
# the week-wide window bought nothing and cost real replay resistance.
# (The constant keeps its name because adapters import it by name.)
SOCKET_SIGNATURE_TOLERANCE_SECONDS = 300

# H1 (security review): the server itself only ever holds a request for up
# to ~2s when there's nothing to return, so without a pause between polls
# an idle agent would still hit the endpoint every ~2s indefinitely.
DEFAULT_POLL_TIMEOUT_SECONDS = 2

# Adaptive polling (H1/LANES.md): poll again soon after real activity;
# back off toward IDLE_POLL_DELAY_SECONDS one step at a time the longer
# nothing shows up, snapping back to ACTIVE_POLL_DELAY_SECONDS the moment
# something does.
ACTIVE_POLL_DELAY_SECONDS = 1.0
IDLE_POLL_DELAY_SECONDS = 5.0

DEFAULT_DEDUPE_MAX = 5000


def default_state_dir(agent_id: str) -> Path:
    """`~/.salt/agents/<agentId>` -- the default home for BOTH the cursor
    and dedupe files for one identity, unless the caller passes its own
    stores explicitly (see SocketClient/Agent.run_socket_async). Created
    (and chmod'd 0700) if missing."""
    safe = re.sub(r"[^a-z0-9_-]", "_", str(agent_id).lower()) or "unknown"
    directory = Path.home() / ".salt" / "agents" / safe
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


# --- Cursor persistence -----------------------------------------------------


class CursorStore(Protocol):
    def get(self) -> int: ...
    def set(self, cursor: int) -> None: ...


class MemoryCursorStore:
    """Lost on restart -- fine for a quick script or a test. Opt in
    explicitly; the default is FileCursorStore (see SocketClient)."""

    def __init__(self, start: int = 0) -> None:
        self._cursor = start

    def get(self) -> int:
        return self._cursor

    def set(self, cursor: int) -> None:
        self._cursor = cursor


class FileCursorStore:
    """Persists the cursor as JSON (`{"cursor": N}`) in one file, written
    atomically (temp file + rename), mode 0600, in a directory forced to
    0700. A restart resumes from where it left off instead of re-delivering
    (or, worse, silently skipping) up to 7 days of retained updates."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def get(self) -> int:
        try:
            raw = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return 0
        if isinstance(raw, dict):
            cursor = raw.get("cursor", 0)
        else:
            cursor = raw  # tolerate the old plain-integer-text format
        try:
            return int(cursor)
        except (TypeError, ValueError):
            return 0

    def set(self, cursor: int) -> None:
        _atomic_write(self.path, json.dumps({"cursor": cursor}))


# --- Delivery-id dedupe (replay protection without trusting the envelope's
# --- own timestamp -- see this module's header comment) --------------------


class DedupeStore(Protocol):
    def has(self, delivery_id: str) -> bool: ...
    def add(self, delivery_id: str) -> None: ...


class MemoryDedupeStore:
    """In-memory dedupe set, bounded to the last `max_size` delivery ids
    (oldest dropped first). Opt in explicitly; the default is
    FileDedupeStore (see SocketClient)."""

    def __init__(self, max_size: int = DEFAULT_DEDUPE_MAX) -> None:
        self.max_size = max_size
        self._order: list[str] = []
        self._seen: set[str] = set()

    def has(self, delivery_id: str) -> bool:
        return delivery_id in self._seen

    def add(self, delivery_id: str) -> None:
        if delivery_id in self._seen:
            return
        self._order.append(delivery_id)
        self._seen.add(delivery_id)
        while len(self._order) > self.max_size:
            oldest = self._order.pop(0)
            self._seen.discard(oldest)


class FileDedupeStore:
    """One JSON file (an array of the last `max_size` delivery ids, oldest
    first), written atomically -- same convention as FileCursorStore, mode
    0600 on the file, 0700 on its directory."""

    def __init__(self, path: str | Path, max_size: int = DEFAULT_DEDUPE_MAX) -> None:
        self.path = Path(path)
        self.max_size = max_size
        self._cache: list[str] | None = None

    def _load(self) -> list[str]:
        if self._cache is not None:
            return self._cache
        try:
            raw = json.loads(self.path.read_text())
            self._cache = [str(x) for x in raw] if isinstance(raw, list) else []
        except (FileNotFoundError, ValueError):
            self._cache = []
        return self._cache

    def has(self, delivery_id: str) -> bool:
        return delivery_id in self._load()

    def add(self, delivery_id: str) -> None:
        items = self._load()
        if delivery_id in items:
            return
        items = items + [delivery_id]
        while len(items) > self.max_size:
            items.pop(0)
        self._cache = items
        _atomic_write(self.path, json.dumps(items))


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

# One round-trip's handling of a single update row: "advance" means the
# cursor may move past it (whether dispatched, deduped-skip, or a
# DEFINITIVE rejection like a bad signature); "halt" means a failure that
# might be transient (no signing secret available, or the lookup itself
# raised) -- the caller must not advance the cursor past it.
_RowOutcome = str  # "advance" | "halt"


class SocketClient:
    """Short-polls `GET /api/v1/agent/updates` and yields verified `Event`s,
    in cursor order. Transport concern only -- dedupe against the
    server-side event families, the mention rule and loop guards live in
    `saltapp.agent.Agent`.

    NOT what `Agent.run_socket_async()` uses (see `saltapp.cable.
    CableClient` instead, as of 2026-09-22) -- kept as a low-level
    fallback primitive for a caller that specifically wants HTTP-only
    long-polling with no persistent websocket.
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
        poll_timeout: int = DEFAULT_POLL_TIMEOUT_SECONDS,
        poll_limit: int = 100,
        active_poll_delay: float = ACTIVE_POLL_DELAY_SECONDS,
        idle_poll_delay: float = IDLE_POLL_DELAY_SECONDS,
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
        self.poll_timeout = poll_timeout
        self.poll_limit = poll_limit
        self.active_poll_delay = active_poll_delay
        self.idle_poll_delay = idle_poll_delay
        self._logger = logger or logging.getLogger("saltapp.socket")
        self._backoff = _Backoff()
        self._had_activity = False

    def _secret(self) -> str | None:
        if self._secret_provider is not None:
            return self._secret_provider()
        return self._static_secret

    def _classify_update(self, update: dict) -> tuple[_RowOutcome, Event | None]:
        """Verify + dedupe one update row. See this module's header comment
        for what "transient" (halt) vs. "definitive" (advance) means."""
        headers = update.get("headers") or {}
        raw_body = update.get("body")
        if isinstance(raw_body, str):
            raw_bytes = raw_body.encode("utf-8")
        else:
            raw_bytes = json.dumps(raw_body or {}).encode("utf-8")

        secret = None
        if self.verify_signatures:
            try:
                secret = self._secret()
            except Exception as exc:  # noqa: BLE001 -- a network blip fetching the secret
                self._logger.error(
                    "[socket] fetching signing secret failed (transient) for update id=%s: %s; will retry",
                    update.get("id"), exc,
                )
                return "halt", None
            if not secret:
                self._logger.error(
                    "[socket] no signing secret available yet for update id=%s (transient); will retry",
                    update.get("id"),
                )
                return "halt", None

        try:
            event = handle(
                headers, raw_bytes, secret=secret, verify=self.verify_signatures, tolerance_seconds=self.tolerance_seconds,
            )
        except WebhookVerificationError as exc:
            self._logger.error("[socket] rejected update id=%s: %s", update.get("id"), exc)
            return "advance", None

        if update.get("delivery_id") is not None:
            event.delivery_id = str(update["delivery_id"])
        if update.get("created_at") is not None:
            event.created_at = str(update["created_at"])

        if event.delivery_id:
            try:
                already_seen = self.dedupe_store.has(event.delivery_id)
            except Exception as exc:  # noqa: BLE001 -- fail open: a dedupe lookup failure must not block real delivery
                self._logger.error("[socket] dedupe lookup for %s failed (failing open): %s", event.delivery_id, exc)
                already_seen = False
            if already_seen:
                return "advance", None
            try:
                self.dedupe_store.add(event.delivery_id)
            except Exception as exc:  # noqa: BLE001
                self._logger.error("[socket] recording dedupe for %s failed: %s", event.delivery_id, exc)

        return "advance", event

    async def poll_once(self) -> list[Event]:
        """One short-poll round-trip. Returns zero or more verified,
        deduped events, in server order, and advances the cursor store past
        all of them -- UNLESS a transient verification failure halts the
        batch partway through, in which case the cursor stops just short of
        it (never re-dispatching what's already been handled, never
        skipping what hasn't) and this call sleeps out a backoff delay
        before returning. On a transport error, sleeps for the current
        backoff delay, extends it, and returns an empty list; any clean
        round trip (even an empty one) resets the backoff.
        """
        after = self.cursor_store.get()
        try:
            response = await self.client.get_agent_updates(
                self.api_key, after=after, timeout=self.poll_timeout, limit=self.poll_limit
            )
        except Exception as exc:  # noqa: BLE001 -- any transport/HTTP failure
            self._logger.error("[socket] poll failed: %s", exc)
            await asyncio.sleep(self._backoff.next())
            self._had_activity = False
            return []

        updates = response.get("updates") or []
        server_cursor = response.get("cursor", after)

        events: list[Event] = []
        advanced = after
        halted = False
        for update in updates:
            outcome, event = self._classify_update(update)
            if outcome == "halt":
                halted = True
                break
            update_id = update.get("id")
            if update_id is not None:
                advanced = update_id
            if event is not None:
                events.append(event)

        if halted:
            self.cursor_store.set(advanced)
            self._had_activity = bool(events)
            await asyncio.sleep(self._backoff.next())
            return events

        self._backoff.reset()
        self.cursor_store.set(server_cursor if server_cursor is not None else advanced)
        self._had_activity = len(updates) > 0
        return events

    async def run(self, on_event: OnEvent, *, stop: asyncio.Event | None = None) -> None:
        """Poll forever (until `stop` is set), adaptively: `active_poll_delay`
        between polls right after a poll returned real activity, ramping
        one step at a time toward `idle_poll_delay` the longer nothing
        does, snapping back the moment something does. Each event's
        `on_event` call is scheduled as its own task rather than awaited
        inline, so one handler blocked in `ctx.ask()` (see saltapp.agent)
        never stalls the poll loop -- new updates, including the answer
        that handler is waiting on, keep arriving.
        """
        idle_delay = self.active_poll_delay
        while stop is None or not stop.is_set():
            events = await self.poll_once()
            for event in events:
                asyncio.create_task(self._safe_call(on_event, event))
            if stop is not None and stop.is_set():
                break
            if self._had_activity:
                idle_delay = self.active_poll_delay
            else:
                idle_delay = min(idle_delay + self.active_poll_delay, self.idle_poll_delay)
            await asyncio.sleep(idle_delay)

    async def _safe_call(self, on_event: OnEvent, event: Event) -> None:
        try:
            await on_event(event)
        except Exception as exc:  # noqa: BLE001
            self._logger.error("[socket] handler failed for delivery %s: %s", event.delivery_id, exc)
