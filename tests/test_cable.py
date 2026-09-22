# saltapp.cable.CableClient against an in-process fake Action Cable server
# -- no real network, no real `websockets` connection. FakeServer/
# FakeConnection together implement just enough of the connector protocol
# (`async with connector(uri, headers) as ws: await ws.send(...); await
# ws.recv()`) that CableClient can't tell the difference from a real
# websocket. Frame ordering, subscribe/replay/replay_done, dedupe,
# backfill-on-`more`, ack, reconnect-with-backoff, and honouring a 429's
# Retry-After on the handshake are all exercised without ever touching a
# socket.
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest

from saltapp.cable import CableHandshakeRejected, CableClient, _parse_retry_after, cable_url
from saltapp.client import AsyncSaltClient
from saltapp.socket import MemoryCursorStore, MemoryDedupeStore


def test_cable_url_rewrites_scheme():
    assert cable_url("https://saltapp.ai") == "wss://saltapp.ai/cable"
    assert cable_url("https://saltapp.ai/") == "wss://saltapp.ai/cable"
    assert cable_url("http://localhost:3000") == "ws://localhost:3000/cable"


def test_parse_retry_after_seconds_and_http_date_and_garbage():
    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("not a date") is None
    # An HTTP-date in the past still clamps to 0, never negative.
    assert _parse_retry_after("Mon, 01 Jan 2000 00:00:00 GMT") == 0.0

SECRET = "cable-secret"
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
        "created_at": "2026-09-22T00:00:00Z",
    }


class FakeConnection:
    """Stands in for one open websocket. `send` records outgoing frames;
    `recv` pulls from an inbound queue the test (or FakeServer) feeds --
    blocking, like the real thing, until something arrives or `close()`
    ends it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def recv(self) -> str:
        item = await self._inbox.get()
        if item is None:
            raise ConnectionClosedFake()
        return item

    async def push(self, frame: dict) -> None:
        await self._inbox.put(json.dumps(frame))

    async def close(self, *, error: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        await self._inbox.put(None)


class ConnectionClosedFake(Exception):
    pass


class FakeConnector:
    """Callable connector: `connector(uri, headers)` -> async context
    manager yielding a FakeConnection, exactly CableClient's `Connector`
    protocol. `on_connect(conn, uri, headers)` -- given per test -- is
    where the fake server actually drives the conversation (send welcome,
    reply to subscribe, etc), scheduled as its own task so `__aenter__`
    can return immediately, the same as a real connect() would."""

    def __init__(self, on_connect, *, reject: CableHandshakeRejected | None = None) -> None:
        self.on_connect = on_connect
        self.reject = reject
        self.connections: list[FakeConnection] = []
        self.attempts = 0

    def __call__(self, uri: str, headers: dict[str, str]):
        return self._ConnectCM(self, uri, headers)

    class _ConnectCM:
        def __init__(self, outer: "FakeConnector", uri: str, headers: dict[str, str]) -> None:
            self.outer = outer
            self.uri = uri
            self.headers = headers
            self.conn: FakeConnection | None = None
            self._task: asyncio.Task | None = None

        async def __aenter__(self) -> FakeConnection:
            self.outer.attempts += 1
            if self.outer.reject is not None:
                raise self.outer.reject
            self.conn = FakeConnection()
            self.outer.connections.append(self.conn)
            self._task = asyncio.create_task(self.outer.on_connect(self.conn, self.uri, self.headers))
            return self.conn

        async def __aexit__(self, *exc_info: object) -> bool:
            if self.conn is not None:
                await self.conn.close()
            if self._task is not None:
                self._task.cancel()
            return False


def make_client(handler) -> AsyncSaltClient:
    transport = httpx.MockTransport(handler)
    return AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=transport))


def make_cable(client_handler, connector, **overrides) -> CableClient:
    client = make_client(client_handler)
    kwargs = dict(
        agent_id="agent-1",
        webhook_secret=SECRET,
        cursor_store=MemoryCursorStore(),
        dedupe_store=MemoryDedupeStore(),
        connector=connector,
        min_backoff=0.01,
        max_backoff=0.02,
        ping_timeout=5.0,
    )
    kwargs.update(overrides)
    return CableClient(client, "api-key", **kwargs)


def no_http_calls_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected HTTP call while cable is idle: {request.method} {request.url}")


@pytest.mark.asyncio
async def test_subscribes_replays_and_dispatches_then_goes_idle_with_zero_http_calls():
    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}

    async def on_connect(conn, uri, headers):
        assert uri == "wss://example.saltapp.test/cable"
        assert headers == {"api-key": "api-key"}
        # Wait for the subscribe command before replaying anything.
        while not conn.sent:
            await asyncio.sleep(0.005)
        identifier = json.loads(conn.sent[0]["identifier"])
        assert identifier == {"channel": "AgentUpdatesChannel"}  # no local cursor yet -> omitted
        await conn.push({"type": "welcome"})
        await conn.push({"type": "confirm_subscription"})
        await conn.push({"message": envelope(1, body)})
        await conn.push({"message": {"type": "replay_done", "cursor": 1}})

    connector = FakeConnector(on_connect)
    cable = make_cable(no_http_calls_handler, connector)

    received = []

    async def on_event(event):
        received.append(event)

    stop = asyncio.Event()
    run_task = asyncio.create_task(cable.run(on_event, stop=stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(run_task, timeout=2)

    assert len(received) == 1
    assert received[0].type == "message"
    assert received[0].delivery_id == "delivery-1"
    assert cable.cursor_store.get() == 1


@pytest.mark.asyncio
async def test_dedupes_a_live_frame_repeated_after_reconnect():
    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}
    dedupe = MemoryDedupeStore()
    cursor = MemoryCursorStore()

    connect_count = {"n": 0}

    async def on_connect(conn, uri, headers):
        connect_count["n"] += 1
        while not conn.sent:
            await asyncio.sleep(0.005)
        await conn.push({"type": "confirm_subscription"})
        await conn.push({"message": envelope(1, body, delivery_id="fixed-id")})
        await conn.push({"message": {"type": "replay_done", "cursor": 1}})
        # Immediately close -- forces a reconnect, which replays the SAME
        # row again (as if the server were replaying from an un-advanced
        # cursor.json some caller reused).
        await conn.close()

    connector = FakeConnector(on_connect)
    cable = make_cable(no_http_calls_handler, connector, cursor_store=cursor, dedupe_store=dedupe)

    received = []

    async def on_event(event):
        received.append(event)

    stop = asyncio.Event()
    run_task = asyncio.create_task(cable.run(on_event, stop=stop))
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(run_task, timeout=2)

    assert connector.attempts >= 2
    assert len(received) == 1  # the second connection's replay was deduped


@pytest.mark.asyncio
async def test_bad_signature_is_dropped_but_still_advances_past():
    bad = envelope(1, {"message": {"chat_id": "c1"}}, secret="wrong-secret")

    async def on_connect(conn, uri, headers):
        while not conn.sent:
            await asyncio.sleep(0.005)
        await conn.push({"type": "confirm_subscription"})
        await conn.push({"message": bad})
        await conn.push({"message": {"type": "replay_done", "cursor": 1}})

    connector = FakeConnector(on_connect)
    cable = make_cable(no_http_calls_handler, connector)

    received = []
    stop = asyncio.Event()
    run_task = asyncio.create_task(cable.run(lambda e: received.append(e), stop=stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(run_task, timeout=2)

    assert received == []
    assert cable.cursor_store.get() == 1


@pytest.mark.asyncio
async def test_backfill_pages_until_empty_when_replay_done_says_more():
    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}
    backfill_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert "timeout=0" in str(request.url)
        backfill_calls.append(str(request.url))
        if len(backfill_calls) == 1:
            assert "after=1" in str(request.url)
            return httpx.Response(200, json={"updates": [envelope(2, body, delivery_id="d2")], "cursor": 2})
        if len(backfill_calls) == 2:
            assert "after=2" in str(request.url)
            return httpx.Response(200, json={"updates": [envelope(3, body, delivery_id="d3")], "cursor": 3})
        assert "after=3" in str(request.url)
        return httpx.Response(200, json={"updates": [], "cursor": 3})

    async def on_connect(conn, uri, headers):
        while not conn.sent:
            await asyncio.sleep(0.005)
        await conn.push({"type": "confirm_subscription"})
        await conn.push({"message": {"type": "replay_done", "cursor": 1, "more": True}})

    connector = FakeConnector(on_connect)
    cable = make_cable(handler, connector)

    received = []
    stop = asyncio.Event()
    run_task = asyncio.create_task(cable.run(lambda e: received.append(e), stop=stop))
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(run_task, timeout=2)

    assert len(backfill_calls) == 3  # two pages with rows, one empty page that ends it
    assert {e.delivery_id for e in received} == {"d2", "d3"}
    assert cable.cursor_store.get() == 3


@pytest.mark.asyncio
async def test_acks_after_processing_a_live_frame_once_caught_up():
    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}
    ack_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        ack_calls.append(str(request.url))
        assert "limit=1" in str(request.url)
        assert "timeout=0" in str(request.url)
        return httpx.Response(200, json={"updates": [], "cursor": 5})

    async def on_connect(conn, uri, headers):
        while not conn.sent:
            await asyncio.sleep(0.005)
        await conn.push({"type": "confirm_subscription"})
        await conn.push({"message": {"type": "replay_done", "cursor": 0}})  # caught up, nothing to replay
        await asyncio.sleep(0.02)
        await conn.push({"message": envelope(5, body, delivery_id="live-1")})  # a genuine live frame

    connector = FakeConnector(on_connect)
    cable = make_cable(handler, connector)

    received = []
    stop = asyncio.Event()
    run_task = asyncio.create_task(cable.run(lambda e: received.append(e), stop=stop))
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(run_task, timeout=2)

    assert len(received) == 1
    assert "after=5" in ack_calls[0]


@pytest.mark.asyncio
async def test_reconnects_with_backoff_after_an_abrupt_close():
    attempts = []

    async def on_connect(conn, uri, headers):
        attempts.append(time.monotonic())
        while not conn.sent:
            await asyncio.sleep(0.005)
        await conn.push({"type": "confirm_subscription"})
        await conn.push({"message": {"type": "replay_done", "cursor": 0}})
        await conn.close()  # drop immediately -> forces reconnect

    connector = FakeConnector(on_connect)
    cable = make_cable(no_http_calls_handler, connector, min_backoff=0.02, max_backoff=0.05)

    stop = asyncio.Event()
    run_task = asyncio.create_task(cable.run(lambda e: None, stop=stop))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(run_task, timeout=2)

    assert len(attempts) >= 3  # actually reconnected more than once
    # Each attempt is separated by roughly a backoff wait, not back-to-back.
    gaps = [b - a for a, b in zip(attempts, attempts[1:])]
    assert all(gap >= 0.005 for gap in gaps)


@pytest.mark.asyncio
async def test_honours_retry_after_on_a_429_handshake():
    rejected = CableHandshakeRejected(429, retry_after_seconds=0.05)
    connector = FakeConnector(on_connect=None, reject=rejected)
    cable = make_cable(no_http_calls_handler, connector, min_backoff=5.0, max_backoff=10.0)

    stop = asyncio.Event()
    started = time.monotonic()
    run_task = asyncio.create_task(cable.run(lambda e: None, stop=stop))
    # If Retry-After (0.05s) is honoured instead of the 5s backoff floor,
    # this settles quickly; otherwise the test would need to wait ~5s.
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(run_task, timeout=2)
    elapsed = time.monotonic() - started

    assert connector.attempts >= 2
    assert elapsed < 1.0  # far under the 5s ordinary backoff floor


@pytest.mark.asyncio
async def test_stop_returns_promptly_while_idle_and_subscribed():
    async def on_connect(conn, uri, headers):
        while not conn.sent:
            await asyncio.sleep(0.005)
        await conn.push({"type": "confirm_subscription"})
        await conn.push({"message": {"type": "replay_done", "cursor": 0}})
        # ... then nothing. The client is idle, connected, caught up.

    connector = FakeConnector(on_connect)
    cable = make_cable(no_http_calls_handler, connector, ping_timeout=30.0)

    stop = asyncio.Event()
    run_task = asyncio.create_task(cable.run(lambda e: None, stop=stop))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    stop.set()
    await asyncio.wait_for(run_task, timeout=2)
    assert time.monotonic() - started < 1.5  # stop() doesn't wait out the full ping window


@pytest.mark.asyncio
async def test_local_cursor_is_sent_as_after_on_a_resumed_subscribe():
    seen_identifiers = []

    async def on_connect(conn, uri, headers):
        while not conn.sent:
            await asyncio.sleep(0.005)
        seen_identifiers.append(json.loads(conn.sent[0]["identifier"]))
        await conn.push({"type": "confirm_subscription"})
        await conn.push({"message": {"type": "replay_done", "cursor": 7}})

    connector = FakeConnector(on_connect)
    cursor = MemoryCursorStore(start=7)
    cable = make_cable(no_http_calls_handler, connector, cursor_store=cursor)

    stop = asyncio.Event()
    run_task = asyncio.create_task(cable.run(lambda e: None, stop=stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(run_task, timeout=2)

    assert seen_identifiers == [{"channel": "AgentUpdatesChannel", "after": 7}]
