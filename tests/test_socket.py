from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest

from saltapp.client import AsyncSaltClient
from saltapp.socket import MemoryCursorStore, SocketClient

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


@pytest.mark.asyncio
async def test_poll_once_advances_cursor_and_returns_verified_events():
    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert "after=0" in str(request.url)
        return httpx.Response(200, json={"updates": [envelope(1, body)], "cursor": 1})

    client = make_async_client(handler)
    socket = SocketClient(client, "api-key", webhook_secret=SECRET, cursor_store=MemoryCursorStore())

    events = await socket.poll_once()
    assert len(events) == 1
    assert events[0].type == "message"
    assert events[0].delivery_id == "delivery-1"
    assert socket.cursor_store.get() == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_poll_once_drops_bad_signature_but_still_advances_cursor():
    body = {"message": {"chat_id": "c1"}}
    bad = envelope(1, body, secret="wrong-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"updates": [bad], "cursor": 1})

    client = make_async_client(handler)
    socket = SocketClient(client, "api-key", webhook_secret=SECRET, cursor_store=MemoryCursorStore())

    events = await socket.poll_once()
    assert events == []  # dropped, not delivered
    assert socket.cursor_store.get() == 1  # but the cursor still moved past it


@pytest.mark.asyncio
async def test_poll_once_drops_replayed_stale_envelope():
    body = {"message": {"chat_id": "c1"}}
    stale = envelope(1, body, t=int(time.time()) - 10_000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"updates": [stale], "cursor": 1})

    client = make_async_client(handler)
    socket = SocketClient(client, "api-key", webhook_secret=SECRET, cursor_store=MemoryCursorStore())

    events = await socket.poll_once()
    assert events == []


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

    client = make_async_client(handler)
    socket = SocketClient(client, "api-key", webhook_secret=SECRET, cursor_store=MemoryCursorStore())

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

    client = make_async_client(handler)
    socket = SocketClient(client, "api-key", webhook_secret=SECRET, cursor_store=MemoryCursorStore())

    received = []
    stop = asyncio.Event()

    async def on_event(event):
        received.append(event)
        stop.set()

    await asyncio.wait_for(socket.run(on_event, stop=stop), timeout=5)
    assert len(received) == 1
    assert received[0].type == "message"
