"""A computer that is asleep or offline: salt-api keeps the request `requested` with `delivered_at: null`
for up to 8 hours (UNDELIVERED_TTL) and sends `device_session_received` when the device has it.
The approval clock starts there; before it `open()` waits on `queued_timeout_s`. Session JSON shapes
follow DeviceSession#as_json_public (salt-api)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from saltapp.device_client import DeviceClient, DeviceProtocolError, QUEUED_TIMEOUT_S

from tests.test_device_client import DEVICE, SESSION, FakeTransport, ManualTimers, tick


def session_json(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "agent": None, "intent": None, "id": SESSION, "agent_id": "ag", "device_id": DEVICE, "mandate_id": "m",
        "chat_id": "chat-1", "status": "requested", "requested_at": "2026-11-16T10:00:00Z", "started_at": None,
        "ended_at": None, "end_reason": None, "last_beat_at": None, "delivered_at": None,
        "waits_until": "2026-11-16T18:00:00Z", "needs_person": True, "position": None, "pause_reason": None,
        "lease_expires_at": None, "last_seq": 0, "capabilities": {}, "action_counts": {},
        "chat": {"id": "chat-1", "readers": {"ids": ["d", "p1"], "open": False}},
        "device_online": False, "device_last_seen_at": None,
    }
    base.update(over)
    return base


def event(kind: str, **over: Any) -> dict[str, Any]:
    return {**session_json(**over), "type": f"device_session_{kind}", "event": kind, "session_id": SESSION}


async def test_the_default_wait_is_the_servers_eight_hours():
    assert QUEUED_TIMEOUT_S == 8 * 3600


async def test_undelivered_request_runs_the_long_wait_not_the_approval_clock(monkeypatch):
    timers = ManualTimers(monkeypatch)
    t = FakeTransport(open_result=session_json())
    c = DeviceClient(t, DEVICE, approval_timeout_s=150, queued_timeout_s=28800)
    states: list[Any] = []
    c.on_state(states.append)
    opened = asyncio.ensure_future(c.open())
    await tick()
    assert [x["delay"] for x in timers.live()] == [28800]
    assert states == [{"state": "requested", "delivered": False}]
    # The computer wakes and has the request: the approval clock replaces the long wait.
    c.handle_delivery("device_session", event("received", delivered_at="2026-11-16T10:05:00Z"))
    assert states[-1] == {"state": "delivered"}
    assert [x["delay"] for x in timers.live()] == [150]
    timers.fire_last()
    with pytest.raises(DeviceProtocolError, match="did not approve in time"):
        await opened


async def test_the_long_wait_ends_with_its_own_message(monkeypatch):
    timers = ManualTimers(monkeypatch)
    c = DeviceClient(FakeTransport(open_result=session_json()), DEVICE)
    opened = asyncio.ensure_future(c.open())
    await tick()
    timers.fire_last()
    with pytest.raises(DeviceProtocolError, match="did not pick up the request"):
        await opened


async def test_queued_behind_another_agent_waits_on_the_long_clock_and_queue_moves_do_not_reset_it(monkeypatch):
    timers = ManualTimers(monkeypatch)
    t = FakeTransport(open_result=session_json(status="queued", position=2, chat=None))
    c = DeviceClient(t, DEVICE, queued_timeout_s=99)
    opened = asyncio.ensure_future(c.open())
    await tick()
    assert [x["delay"] for x in timers.live()] == [99]
    c.handle_delivery("device_session", event("queue_moved", status="queued", position=1, chat=None))
    assert len(timers.live()) == 1
    # Promoted to a request the computer has not got yet: still the long wait.
    c.handle_delivery("device_session", event("promoted", status="requested", chat=None))
    assert [x["delay"] for x in timers.live()] == [99]
    c.handle_delivery("device_session", event("active", status="active", delivered_at="2026-11-16T10:05:00Z"))
    assert (await opened).status == "active"
    assert timers.live() == []


async def test_a_server_that_omits_delivered_at_keeps_the_approval_clock_at_once(monkeypatch):
    timers = ManualTimers(monkeypatch)
    raw = session_json()
    del raw["delivered_at"]
    c = DeviceClient(FakeTransport(open_result=raw), DEVICE, approval_timeout_s=150)
    states: list[Any] = []
    c.on_state(states.append)
    opened = asyncio.ensure_future(c.open())
    await tick()
    assert [x["delay"] for x in timers.live()] == [150]
    assert states == [{"state": "requested"}]
    timers.fire_last()
    with pytest.raises(DeviceProtocolError):
        await opened


async def test_approved_after_a_long_sleep_resolves_with_the_real_readers_shape():
    t = FakeTransport(open_result=session_json())
    c = DeviceClient(t, DEVICE)
    opened = asyncio.ensure_future(c.open())
    await tick()
    c.handle_delivery("device_session", event("active", status="active", delivered_at="2026-11-16T10:05:00Z"))
    m = await opened
    assert m.status == "active"
    c._require_active()  # readers {ids, open:false} are known
    assert c._readers is not None and c._readers.ids == ("d", "p1")


async def test_cancelling_open_stops_the_wait_and_withdraws_the_request(monkeypatch):
    timers = ManualTimers(monkeypatch)
    t = FakeTransport(open_result=session_json())
    c = DeviceClient(t, DEVICE)
    opened = asyncio.ensure_future(c.open())
    await tick()
    opened.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opened
    assert t.stopped == [SESSION]
    assert timers.live() == []
