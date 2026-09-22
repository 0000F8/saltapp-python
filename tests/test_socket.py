from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest

from saltapp.client import AsyncSaltClient
from saltapp.errors import SaltApiError
from saltapp.socket import (
    SOCKET_SIGNATURE_TOLERANCE_SECONDS,
    FileCursorStore,
    FileDedupeStore,
    MemoryCursorStore,
    MemoryDedupeStore,
    SocketClient,
    default_state_dir,
)

SECRET = "socket-secret"
HOST = "https://example.saltapp.test"


def sign(secret: str, t: int, raw: bytes) -> str:
    digest = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


def envelope(update_id: int, body: dict, *, secret: str = SECRET, delivery_id: str | None = None, t: int | None = None) -> dict:
    raw = json.dumps(body).encode()
    ts = t if t is not None else int(time.time())
    return {
        "id": update_id,
        "delivery_id": delivery_id or f"delivery-{update_id}",
        "event": "message",
        "headers": {
            "X-Salt-Signature": sign(secret, ts, raw),
            "X-Salt-Agent-Id": "agent-1",
        },
        "body": json.dumps(body),
        "created_at": "2026-09-18T00:00:00Z",
    }


def make_async_client(handler) -> AsyncSaltClient:
    transport = httpx.MockTransport(handler)
    return AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=transport))


def make_socket(handler, **overrides) -> SocketClient:
    """A SocketClient wired for tests: always explicit memory stores (never
    the real home directory -- see saltapp.socket's default_state_dir)."""
    client = make_async_client(handler)
    kwargs = dict(
        agent_id="agent-1",
        webhook_secret=SECRET,
        cursor_store=MemoryCursorStore(),
        dedupe_store=MemoryDedupeStore(),
    )
    kwargs.update(overrides)
    return SocketClient(client, "api-key", **kwargs)


# drain_once makes no request on its own schedule -- every request in every
# test below happens because the test explicitly calls drain_once() once.
# There is no run()/poll loop anywhere in this module any more.


@pytest.mark.asyncio
async def test_drain_once_advances_cursor_and_returns_verified_events():
    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(200, json={"updates": [envelope(1, body)], "cursor": 1})
        return httpx.Response(200, json={"updates": [], "cursor": 1})  # the page that ends the drain

    socket = make_socket(handler)

    events = await socket.drain_once()
    assert len(events) == 1
    assert events[0].type == "message"
    assert events[0].delivery_id == "delivery-1"
    assert socket.cursor_store.get() == 1
    assert socket.exhausted is True  # the second page was genuinely empty
    assert "after=0" in calls[0]
    assert "timeout=0" in calls[0]  # always 0 -- never holds a connection open
    assert "after=1" in calls[1]
    await socket.client.aclose()


@pytest.mark.asyncio
async def test_drain_once_pages_until_a_page_comes_back_empty():
    """Unlike a single short-poll round trip, drain_once keeps asking as
    long as a page has rows in it, and only stops -- exhausted=True --
    once a page comes back with none."""
    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            assert "after=0" in calls[0]
            return httpx.Response(200, json={"updates": [envelope(1, body, delivery_id="d1")], "cursor": 1})
        if len(calls) == 2:
            assert "after=1" in calls[1]
            return httpx.Response(200, json={"updates": [envelope(2, body, delivery_id="d2")], "cursor": 2})
        assert "after=2" in calls[2]
        return httpx.Response(200, json={"updates": [], "cursor": 2})

    socket = make_socket(handler)
    events = await socket.drain_once()

    assert len(calls) == 3  # two pages with rows, one empty page that ends it
    assert {e.delivery_id for e in events} == {"d1", "d2"}
    assert socket.cursor_store.get() == 2
    assert socket.exhausted is True


@pytest.mark.asyncio
async def test_drain_once_explicit_after_overrides_the_stored_cursor():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"updates": [], "cursor": 99})

    socket = make_socket(handler, cursor_store=MemoryCursorStore(start=5))
    await socket.drain_once(after=99)
    assert "after=99" in captured["url"]  # not the stored 5


@pytest.mark.asyncio
async def test_drain_once_drops_bad_signature_but_still_advances_cursor():
    # A signature mismatch is a DEFINITIVE rejection (not "transient"), so
    # the cursor still moves past it -- retrying a forged/bad envelope
    # forever would just wedge the caller on it.
    body = {"message": {"chat_id": "c1"}}
    bad = envelope(1, body, secret="wrong-secret")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json={"updates": [bad], "cursor": 1})
        return httpx.Response(200, json={"updates": [], "cursor": 1})

    socket = make_socket(handler)

    events = await socket.drain_once()
    assert events == []  # dropped, not delivered
    assert socket.cursor_store.get() == 1  # but the cursor still moved past it
    assert socket.exhausted is True  # a genuinely empty second page followed it


@pytest.mark.asyncio
async def test_drain_once_drops_replayed_stale_envelope():
    # Far beyond the socket-mode tolerance -- still a DEFINITIVE rejection,
    # so the cursor still advances past it.
    body = {"message": {"chat_id": "c1"}}
    stale = envelope(1, body, t=int(time.time()) - (SOCKET_SIGNATURE_TOLERANCE_SECONDS + 10_000))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json={"updates": [stale], "cursor": 1})
        return httpx.Response(200, json={"updates": [], "cursor": 1})

    socket = make_socket(handler)

    events = await socket.drain_once()
    assert events == []
    assert socket.cursor_store.get() == 1


@pytest.mark.asyncio
async def test_drain_once_accepts_an_old_outbox_row_signed_at_serve_time():
    """A row can sit in the outbox for days, but salt-api re-signs it at
    SERVE time, so what arrives here is always freshly stamped -- age in the
    outbox is not age on the signature."""
    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}
    # id 1 is days old as a ROW; its signature was minted just now.
    served_now = envelope(1, body, t=int(time.time()))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json={"updates": [served_now], "cursor": 1})
        return httpx.Response(200, json={"updates": [], "cursor": 1})

    socket = make_socket(handler)

    events = await socket.drain_once()
    assert len(events) == 1
    assert socket.cursor_store.get() == 1


@pytest.mark.asyncio
async def test_drain_once_rejects_an_envelope_whose_signature_is_genuinely_stale():
    """The counterpart to serve-time signing: because every envelope is
    stamped as it is served, a timestamp that IS old means something is
    wrong (a replay), so the socket path uses the same ~300s window as the
    webhook path rather than the week-wide one it carried before."""
    assert SOCKET_SIGNATURE_TOLERANCE_SECONDS == 300

    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}
    stale = envelope(1, body, t=int(time.time()) - 3600)  # an hour old
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json={"updates": [stale], "cursor": 1})
        return httpx.Response(200, json={"updates": [], "cursor": 1})

    socket = make_socket(handler)

    events = await socket.drain_once()
    assert events == []
    # A definitive rejection still advances past the row -- retrying a
    # permanently bad signature forever would wedge the agent.
    assert socket.cursor_store.get() == 1


@pytest.mark.asyncio
async def test_drain_once_deduplicates_by_delivery_id():
    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}
    dupe = envelope(1, body, delivery_id="delivery-fixed")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        # Page 1 (first drain_once call): the row, then an empty page.
        # Pages 3+ (second drain_once call, same after=0): the SAME row
        # again (as if a restart replayed the backlog), then empty.
        if len(calls) in (1, 3):
            return httpx.Response(200, json={"updates": [dupe], "cursor": 1})
        return httpx.Response(200, json={"updates": [], "cursor": 1})

    dedupe = MemoryDedupeStore()
    socket = make_socket(handler, dedupe_store=dedupe, cursor_store=MemoryCursorStore(start=0))

    events = await socket.drain_once()
    assert len(events) == 1

    # A second, independent on-demand call with the SAME starting cursor
    # must not deliver it twice, even though it verifies fine again.
    events_again = await socket.drain_once(after=0)
    assert events_again == []


@pytest.mark.asyncio
async def test_drain_once_halts_on_transient_verification_failure_and_does_not_advance_past_it(monkeypatch):
    """A missing/unreachable signing secret must not advance the cursor --
    a real update behind it would be silently skipped forever otherwise --
    and must be distinguishable from genuinely having caught up."""
    body1 = {"message": {"chat_id": "c1", "message": "one", "user": {"id": "u1"}}}
    body2 = {"message": {"chat_id": "c1", "message": "two", "user": {"id": "u1"}}}
    e1 = envelope(1, body1, delivery_id="d1")
    e2 = envelope(2, body2, delivery_id="d2")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"updates": [e1, e2], "cursor": 2})

    # The secret provider fails (network error) for the SECOND row only --
    # simulated by making it fail after the first successful call.
    calls = {"n": 0}

    def flaky_secret() -> str:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("network blip fetching signing key")
        return SECRET

    socket = make_socket(handler, webhook_secret=None, webhook_secret_provider=flaky_secret)

    events = await socket.drain_once()
    # Only the first row was dispatched; the halt on the second row stops
    # processing right there -- no further page is even fetched.
    assert len(events) == 1
    assert events[0].delivery_id == "d1"
    # Cursor stops at the first row's id, NOT the server's cursor (2) and
    # NOT left at 0 -- it must not re-dispatch d1 nor skip d2 forever.
    assert socket.cursor_store.get() == 1
    # Distinguishable from a genuine empty-page catch-up.
    assert socket.exhausted is False


@pytest.mark.asyncio
async def test_drain_once_propagates_a_transport_error_rather_than_retrying():
    """drain_once has no retry loop of its own -- a transport failure
    propagates so the caller decides whether and how to retry (see
    saltapp.cable.CableClient._backfill for a caller that adds backoff)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    socket = make_socket(handler)
    with pytest.raises(SaltApiError):
        await socket.drain_once()


# --- store-level tests -------------------------------------------------------


def test_memory_dedupe_store_bounds_and_dedupes():
    store = MemoryDedupeStore(max_size=3)
    for i in range(5):
        store.add(f"d{i}")
    assert store.has("d0") is False  # evicted
    assert store.has("d1") is False  # evicted
    assert store.has("d2") is True
    assert store.has("d4") is True


def test_file_cursor_store_round_trips_atomically(tmp_path):
    store = FileCursorStore(tmp_path / "sub" / "cursor.json")
    assert store.get() == 0
    store.set(42)
    assert store.get() == 42
    # Directory was created and secured.
    directory = tmp_path / "sub"
    assert directory.is_dir()


def test_file_dedupe_store_round_trips_and_bounds(tmp_path):
    store = FileDedupeStore(tmp_path / "seen.json", max_size=2)
    store.add("a")
    store.add("b")
    store.add("c")
    assert store.has("a") is False  # evicted
    assert store.has("b") is True
    assert store.has("c") is True

    # A fresh store instance re-reads the same file.
    reloaded = FileDedupeStore(tmp_path / "seen.json", max_size=2)
    assert reloaded.has("b") is True
    assert reloaded.has("c") is True


def test_default_state_dir_is_per_agent_and_secured(tmp_path, monkeypatch):
    monkeypatch.setattr("saltapp.socket.Path.home", lambda: tmp_path)
    directory = default_state_dir("Agent-ABC")
    assert directory == tmp_path / ".salt" / "agents" / "agent-abc"
    assert directory.is_dir()


def test_socket_client_defaults_to_file_stores_under_fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr("saltapp.socket.Path.home", lambda: tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"updates": [], "cursor": 0})

    client = make_async_client(handler)
    socket = SocketClient(client, "api-key", agent_id="agent-1", webhook_secret=SECRET)

    assert isinstance(socket.cursor_store, FileCursorStore)
    assert isinstance(socket.dedupe_store, FileDedupeStore)
    assert socket.cursor_store.path == tmp_path / ".salt" / "agents" / "agent-1" / "cursor.json"
    assert socket.dedupe_store.path == tmp_path / ".salt" / "agents" / "agent-1" / "seen.json"
