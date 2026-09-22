# Socket mode's on-demand drain -- an agent with no public URL and no open
# Action Cable connection (see `saltapp.cable.CableClient`, the actual
# real-time transport) can still catch up on what it missed by asking,
# ONCE, for exactly what's sitting in its outbox right now. There is no
# loop anywhere in this module that runs on its own schedule -- per the
# owner's ruling (via team lead): "DO NOT USE POLLING as a mechanic EVER:
# pull on demand, push on address." `SocketClient.drain_once` pages
#
#   GET /api/v1/agent/updates?after=<cursor>&timeout=0&limit=<1..100>
#   -> 200 {updates: [{id, delivery_id, event, headers, body, created_at}],
#           cursor: <last id or after>}
#
# (`timeout` is always 0: this never holds a connection open waiting for
# something to show up, it only ever asks what's already there) until a
# page comes back empty, verifying and deduping each row exactly like a
# webhook POST, then returns every verified `Event` and stops -- it makes
# exactly as many requests as it takes to empty the backlog, never more.
#
# Two callers: `saltapp.cable.CableClient` uses this for its own on-demand
# backfill, and ONLY when a Cable replay said the backlog was truncated
# (`replay_done.more`) -- never on an interval, never as its primary
# transport. A tool-shaped host -- a Langflow/Dify-style integration whose
# code only runs when an LLM invokes it, never in the background -- can
# call `drain_once()` directly each time it's invoked, to pull whatever
# arrived since the last call; that's still "pull on demand," not polling,
# because nothing here decides to call it again on its own.
#
# Verification tolerance is the SAME ~300s window the webhook path uses
# (`SOCKET_SIGNATURE_TOLERANCE_SECONDS`). An outbox row can sit un-drained
# for days, but it is re-signed at SERVE time with the agent's current
# webhook secret, so what this module receives is always freshly stamped
# (see `AgentUpdate#as_client_json` in salt-api). Replay protection comes
# from the cursor PLUS a persistent per-agent delivery_id dedupe set
# (DedupeStore), never from the timestamp -- the timestamp is a staleness
# bound, not the defence.
#
# A verification failure that LOOKS transient (no signing secret available
# yet, or the lookup itself raised -- a network blip, not a bad/forged
# signature) stops the drain right there and does NOT advance the cursor
# past that row: the next call to `drain_once` (whenever the caller next
# makes one -- on demand, not scheduled) picks it up again, or a real
# update sitting behind a transient failure would be silently skipped
# forever. A DEFINITIVE rejection (bad signature, malformed header,
# genuinely stale timestamp) still advances past it -- retrying a
# forged/bad envelope forever would just wedge the caller on it. A
# transport error (the HTTP call itself failing) is NOT caught here --
# it propagates to the caller, which decides whether and how to retry
# (see `saltapp.cable.CableClient._backfill` for an example with backoff).
#
# The default cursor AND dedupe stores are FILE-based, at
# `~/.salt/agents/<agentId>/cursor.json` and `.../seen.json` (dirs 0700,
# files 0600) -- memory is opt-in, not the default, since silently losing
# both on every restart is exactly the kind of thing that should be a
# deliberate choice. Pass `MemoryCursorStore()`/`MemoryDedupeStore()`
# explicitly to opt out (tests must always do this, so they never write to
# the real home directory).
#
# Each update's `headers`/`body` are exactly what the equivalent webhook
# POST would have carried (including a real X-Salt-Signature), so this
# module verifies every envelope with the same HMAC check `saltapp.webhook`
# applies to an actual webhook -- a relay sitting between this client and
# salt-api cannot forge an update, only replay or drop one.
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Callable, Optional, Protocol

from saltapp.client import AsyncSaltClient
from saltapp.webhook import Event, WebhookVerificationError, handle

# The SAME ~300s window the webhook path uses. This was once RETENTION
# (7 days) + an hour, on the reasoning that an envelope can sit un-drained
# in the outbox for days, so its signing timestamp would routinely look
# stale. That reasoning stopped being true when salt-api moved to
# SERVE-TIME signing (LANES.md "fix A", round 3): an envelope is re-signed
# with the agent's current webhook secret at the moment it is served, so
# its timestamp is always fresh relative to when this client actually
# receives it -- measured at ~1.1s on the live production gate, not days.
# Keeping the week-wide window bought nothing and cost real replay
# resistance. (The constant keeps its name because adapters import it by
# name.)
SOCKET_SIGNATURE_TOLERANCE_SECONDS = 300

DEFAULT_DEDUPE_MAX = 5000
DEFAULT_DRAIN_LIMIT = 100


def default_state_dir(agent_id: str) -> Path:
    """`~/.salt/agents/<agentId>` -- the default home for BOTH the cursor
    and dedupe files for one identity, unless the caller passes its own
    stores explicitly (see SocketClient/saltapp.cable.CableClient/
    Agent.run_socket_async). Created (and chmod'd 0700) if missing."""
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


# One row's verify+dedupe outcome -- "advance" means the cursor may move
# past it (whether dispatched, deduped-skip, or a DEFINITIVE rejection
# like a bad signature); "halt" means a failure that might be transient
# (no signing secret available, or the lookup itself raised) -- the caller
# must not advance the cursor past it.
_RowOutcome = str  # "advance" | "halt"


class SocketClient:
    """On-demand drain of `GET /api/v1/agent/updates` -- `drain_once()` is
    the only way this ever makes a request; nothing here loops or waits on
    its own. Transport concern only -- dedupe against the server-side
    event families, the mention rule and loop guards live in
    `saltapp.agent.Agent`.
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
        limit: int = DEFAULT_DRAIN_LIMIT,
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
        self.limit = limit
        self._logger = logger or logging.getLogger("saltapp.socket")
        # Set by drain_once() on every call: True when the drain actually
        # reached an empty page (genuinely nothing left), False when it
        # stopped early on a transient verification halt. An empty
        # `events` list is ambiguous by itself -- both outcomes can return
        # one -- so a caller that needs to tell "caught up" apart from
        # "try again" (see saltapp.cable.CableClient._backfill) reads this
        # right after a drain_once() call, before calling it again.
        self.exhausted = True

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
                    "[socket] fetching signing secret failed (transient) for update id=%s: %s",
                    update.get("id"), exc,
                )
                return "halt", None
            if not secret:
                self._logger.error(
                    "[socket] no signing secret available yet for update id=%s (transient)",
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

    async def drain_once(self, after: int | None = None) -> list[Event]:
        """Pages `GET /api/v1/agent/updates` (always `timeout=0` -- never
        holds a connection open waiting for something new) from `after`
        (or this instance's own `cursor_store` position when `after` is
        `None`) until a page comes back empty, then returns every
        verified, deduped `Event` collected along the way, in order. The
        cursor store is advanced/persisted as it goes -- a page that
        contains only a deduped repeat or a definitively-rejected row
        still moves the cursor past it, same as ever.

        Stops early (returning what it has so far) on a transient
        verification failure, WITHOUT advancing the cursor store past the
        row that failed -- the next call to `drain_once` picks it up
        again. A transport error talking to salt-api is NOT caught here;
        it propagates, so the caller decides whether and how to retry
        (this module has no retry loop of its own -- see
        `saltapp.cable.CableClient._backfill` for a caller that adds
        one).

        An empty return is ambiguous by itself (a genuinely empty first
        page and a halt on the first row both return `[]`) -- check
        `self.exhausted` right after the call: `True` means this drain
        actually reached an empty page (nothing left), `False` means it
        stopped early on a transient halt and there is more to fetch once
        that clears up."""
        cursor = self.cursor_store.get() if after is None else after
        events: list[Event] = []
        while True:
            response = await self.client.get_agent_updates(self.api_key, after=cursor, timeout=0, limit=self.limit)
            updates = response.get("updates") or []
            if not updates:
                self.exhausted = True
                return events

            advanced = cursor
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
                self.exhausted = False
                return events

            server_cursor = response.get("cursor")
            cursor = server_cursor if server_cursor is not None else advanced
            self.cursor_store.set(cursor)
