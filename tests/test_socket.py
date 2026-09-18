from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest

from saltapp.client import AsyncSaltClient
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
        active_poll_delay=0.01,
        idle_poll_delay=0.02,
    )
    kwargs.update(overrides)
    return SocketClient(client, "api-key", **kwargs)


@pytest.mark.asyncio
async def test_poll_once_advances_cursor_and_returns_verified_events():
    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert "after=0" in str(request.url)
        return httpx.Response(200, json={"updates": [envelope(1, body)], "cursor": 1})

    socket = make_socket(handler)

    events = await socket.poll_once()
    assert len(events) == 1
    assert events[0].type == "message"
    assert events[0].delivery_id == "delivery-1"
    assert socket.cursor_store.get() == 1
    await socket.client.aclose()


@pytest.mark.asyncio
async def test_poll_once_drops_bad_signature_but_still_advances_cursor():
    # A signature mismatch is a DEFINITIVE rejection (not "transient"), so
    # the cursor still moves past it -- retrying a forged/bad envelope
    # forever would just wedge the client on it.
    body = {"message": {"chat_id": "c1"}}
    bad = envelope(1, body, secret="wrong-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"updates": [bad], "cursor": 1})

    socket = make_socket(handler)

    events = await socket.poll_once()
    assert events == []  # dropped, not delivered
    assert socket.cursor_store.get() == 1  # but the cursor still moved past it


@pytest.mark.asyncio
async def test_poll_once_drops_replayed_stale_envelope():
    # Beyond even the wide socket-mode tolerance (retention + 1h) -- still a
    # DEFINITIVE rejection, so the cursor still advances past it.
    body = {"message": {"chat_id": "c1"}}
    stale = envelope(1, body, t=int(time.time()) - (SOCKET_SIGNATURE_TOLERANCE_SECONDS + 10_000))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"updates": [stale], "cursor": 1})

    socket = make_socket(handler)

    events = await socket.poll_once()
    assert events == []
    assert socket.cursor_store.get() == 1


@pytest.mark.asyncio
async def test_poll_once_uses_wide_default_tolerance_for_an_old_but_within_retention_envelope():
    # Well beyond the webhook path's 300s default, but within retention +
    # 1h -- an outbox row can legitimately sit unpolled for days.
    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}
    old = envelope(1, body, t=int(time.time()) - (3 * 24 * 60 * 60))  # 3 days old

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"updates": [old], "cursor": 1})

    socket = make_socket(handler)

    events = await socket.poll_once()
    assert len(events) == 1
    assert socket.cursor_store.get() == 1


@pytest.mark.asyncio
async def test_poll_once_deduplicates_by_delivery_id():
    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}
    dupe = envelope(1, body, delivery_id="delivery-fixed")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"updates": [dupe], "cursor": 1})

    dedupe = MemoryDedupeStore()
    socket = make_socket(handler, dedupe_store=dedupe, cursor_store=MemoryCursorStore(start=0))

    events = await socket.poll_once()
    assert len(events) == 1

    # A second round with the SAME cursor position (as if the server
    # re-sent the same row, or a restart replayed the backlog) must not
    # dispatch it twice, even though it verifies fine again.
    socket.cursor_store.set(0)
    events_again = await socket.poll_once()
    assert events_again == []


@pytest.mark.asyncio
async def test_poll_once_halts_on_transient_verification_failure_and_does_not_advance_cursor(monkeypatch):
    """A missing/unreachable signing secret must not advance the cursor --
    a real update behind it would be silently skipped forever otherwise."""
    body1 = {"message": {"chat_id": "c1", "message": "one", "user": {"id": "u1"}}}
    body2 = {"message": {"chat_id": "c1", "message": "two", "user": {"id": "u1"}}}
    e1 = envelope(1, body1, delivery_id="d1")
    e2 = envelope(2, body2, delivery_id="d2")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"updates": [e1, e2], "cursor": 2})

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("saltapp.socket.asyncio.sleep", fake_sleep)

    # The secret provider fails (network error) for the SECOND row only --
    # simulated by making it fail after the first successful call.
    calls = {"n": 0}

    def flaky_secret() -> str:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("network blip fetching signing key")
        return SECRET

    socket = make_socket(handler, webhook_secret=None, webhook_secret_provider=flaky_secret)

    events = await socket.poll_once()
    # Only the first row was dispatched; the halt on the second row stops
    # processing right there.
    assert len(events) == 1
    assert events[0].delivery_id == "d1"
    # Cursor stops at the first row's id, NOT the server's cursor (2) and
    # NOT left at 0 -- it must not re-dispatch d1 nor skip d2 forever.
    assert socket.cursor_store.get() == 1
    # The transient halt slept out a backoff delay.
    assert len(sleeps) == 1


@pytest.mark.asyncio
async def test_poll_once_backs_off_on_transport_error_and_resets_on_success(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("saltapp.socket.asyncio.sleep", fake_sleep)

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"updates": [], "cursor": 0})

    socket = make_socket(handler)

    await socket.poll_once()  # fails -> backoff #1
    await socket.poll_once()  # fails -> backoff #2, larger
    await socket.poll_once()  # succeeds -> no sleep, backoff reset

    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0]

    # Backoff should have reset after the success: a subsequent failure
    # sleeps the INITIAL delay again, not a continuation of the old ramp.
    call_count["n"] = 0  # make the handler fail again
    await socket.poll_once()
    assert sleeps[2] == sleeps[0]


@pytest.mark.asyncio
async def test_run_dispatches_events_and_stops():
    import asyncio

    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"updates": [envelope(1, body)], "cursor": 1})
        return httpx.Response(200, json={"updates": [], "cursor": 1})

    socket = make_socket(handler)

    received = []
    stop = asyncio.Event()

    async def on_event(event):
        received.append(event)
        stop.set()

    await asyncio.wait_for(socket.run(on_event, stop=stop), timeout=5)
    assert len(received) == 1
    assert received[0].type == "message"


@pytest.mark.asyncio
async def test_run_polls_adaptively_slower_when_idle():
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"updates": [], "cursor": 0})

    socket = make_socket(handler, active_poll_delay=0.01, idle_poll_delay=0.03)

    stop = asyncio.Event()

    async def on_event(event):
        pass

    async def stopper():
        await asyncio.sleep(0.08)
        stop.set()

    await asyncio.gather(socket.run(on_event, stop=stop), stopper())
    # No activity at all -- ramps toward idle_poll_delay, never exceeding it.
    assert socket._had_activity is False


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
