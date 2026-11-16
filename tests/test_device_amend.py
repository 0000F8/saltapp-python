# Device op `amend` (agent side): protocol helpers, DeviceClient.amend_task, the task's on_start id,
# and the withdraw of a waiting request. Mirrors salt-agent-sdk's tests/device-amend.test.js (the
# client cases); there is no Python device host.
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from saltapp import device as dev
from saltapp.client import AsyncSaltClient
from saltapp.device import DeviceProtocolError, decode_device_message
from saltapp.device_client import DeviceClient, HttpDeviceTransport, LaneReaderError, LaneReaders

from tests.test_device_client import DEVICE, SESSION, FakeTransport, active_client, reply, tick, meta as session_meta


def test_amend_is_an_agent_op_under_device_task_with_no_count_class_and_a_1_s_interval():
    assert "amend" in dev.AGENT_OPS
    assert dev.CAPABILITY_FOR_OP["amend"] == "device.task"
    assert dev.PROGRESS_MIN_INTERVAL_MS == 1_000
    assert dev.AMEND_SAY_MAX == 500


def test_parse_amend_allow_lists_and_clips_and_refuses_bad_bodies():
    assert dev.parse_amend({"id": "T1", "say": "x" * 600, "extra": 1}) == dev.AmendArgs(say="x" * 500, id="T1")
    assert dev.parse_amend({"say": "hi"}) == dev.AmendArgs(say="hi")
    for bad in (None, "s", [], {}, {"say": 3}, {"say": "  "}, {"say": "a", "id": 5}, {"say": "a", "id": ""}, {"say": "a", "id": "i" * 65}):
        assert dev.parse_amend(bad) is None


def test_amend_command_has_re_exp_and_body_id_uses_the_newest_seq_and_refuses_over_500():
    m = decode_device_message(dev.amend_command("x" * 500, session="s", seq=0, task_id="T1", id="A1", now_ms=1_000))
    assert m.header.op == "amend" and m.header.re == "T1" and m.header.seq == 1
    assert m.header.exp == 1_000 + dev.COMMAND_TTL_MS
    assert m.body == {"id": "T1", "say": "x" * 500}
    m2 = decode_device_message(dev.amend_command("only task", session="s", seq=4))
    assert m2.header.re is None and m2.body == {"say": "only task"} and m2.header.seq == 4 and m2.header.exp is not None
    for bad in ("  ", "x" * 501):
        with pytest.raises(DeviceProtocolError) as e:
            dev.amend_command(bad, session="s", seq=1)
        assert e.value.code == "bad_args"
    assert dev.AMEND_MAX_PER_TASK == 20
    assert dev.parse_amend({"say": "\U0001F600" * 600}).say == "\U0001F600" * 500  # clipped by code points


async def test_amend_task_posts_out_of_band_and_refuses_an_empty_say():
    c, t = await active_client()
    await c.amend_task("T1", "also close the tab")
    await c.amend_task(None, "only task")
    a, b = t.sent(0), t.sent(1)
    assert a.header.op == "amend" and a.header.re == "T1" and a.body == {"id": "T1", "say": "also close the tab"}
    assert b.header.re is None and b.body == {"say": "only task"}
    assert a.header.exp is not None and abs(a.header.exp - (time.time() * 1000 + dev.COMMAND_TTL_MS)) < 2000
    with pytest.raises(DeviceProtocolError) as e:
        await c.amend_task("T1", " ")
    assert e.value.code == "bad_args"
    assert len(t.posted) == 2


async def test_amend_task_without_a_live_session_fails():
    c = DeviceClient(FakeTransport(), DEVICE)
    with pytest.raises(DeviceProtocolError) as e:
        await c.amend_task("T1", "x")
    assert e.value.code == "failed"


async def test_task_on_start_gives_the_id_the_amend_names_while_the_task_runs():
    c, t = await active_client()
    started = []
    run = asyncio.ensure_future(c.task("g", on_start=started.append))
    await tick()
    assert started == [t.sent(0).header.id]
    await c.amend_task(started[0], "change of plan")
    assert t.sent(1).header.op == "amend" and t.sent(1).body == {"id": started[0], "say": "change of plan"}
    reply(c, t, 0, "task_result", {"task_id": "t_1", "status": "done", "summary": "Done.", "checked": None, "steps": [], "commands": 0, "duration_ms": 1})
    assert (await run).status == "done"


async def test_cancelling_open_prefers_withdraw_session_when_the_transport_has_it():
    calls = []

    async def withdraw(session_id):
        calls.append(("withdraw", session_id))

    t = FakeTransport(open_result={"id": SESSION, "device_id": DEVICE, "agent_id": "ag", "mandate_id": "m", "status": "requested", "delivered_at": None}, withdraw_session=withdraw)
    c = DeviceClient(t, DEVICE)
    opened = asyncio.ensure_future(c.open())
    await tick()
    opened.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opened
    assert calls == [("withdraw", SESSION)] and t.stopped == []


async def test_http_withdraw_session_is_delete_on_the_session():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(204)

    client = AsyncSaltClient("https://example.saltapp.test", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await HttpDeviceTransport(client, "agent-key").withdraw_session(SESSION)
    assert seen == [("DELETE", f"/api/v1/device_sessions/{SESSION}")]


async def test_cancelling_the_asyncio_task_posts_cancel_and_clears_the_pending_entry():
    c, t = await active_client()
    job = asyncio.ensure_future(c.task("do the thing"))
    await tick()
    assert [t.sent(i).header.op for i in range(len(t.posted))] == ["task"]
    task_id = t.sent(0).header.id
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    await tick(10)
    ops = [t.sent(i).header.op for i in range(len(t.posted))]
    assert ops == ["task", "cancel"]
    assert t.sent(1).header.re == task_id and t.sent(1).body == {"id": task_id}
    assert c._pending == {}


async def test_wait_for_timing_out_a_task_stops_it_on_the_computer():
    c, t = await active_client()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(c.task("slow"), 0.05)
    await tick(10)
    assert [t.sent(i).header.op for i in range(len(t.posted))] == ["task", "cancel"]


async def test_cancelling_open_while_the_post_is_in_flight_withdraws_what_the_post_creates():
    gate = asyncio.Event()
    calls = []

    async def open_session(device_id, *, intent=None):
        await gate.wait()
        return {"id": SESSION, "device_id": DEVICE, "agent_id": "ag", "mandate_id": "m", "status": "requested", "delivered_at": None}

    async def withdraw(sid):
        calls.append(("withdraw", sid))

    t = FakeTransport(withdraw_session=withdraw)
    t.open_session = open_session
    c = DeviceClient(t, DEVICE)
    opened = asyncio.ensure_future(c.open())
    await tick()
    opened.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opened
    assert calls == []
    gate.set()
    await tick(10)
    assert calls == [("withdraw", SESSION)]


async def test_a_send_that_fails_on_a_stale_reader_rereads_the_session_and_retries_once_and_cancel_is_lenient():
    posts = []
    fetched = []

    async def post_command(plaintext, readers):
        posts.append((decode_device_message(plaintext).header.op, readers.ids, readers.lenient))
        if "ghost" in readers.ids and not readers.lenient:
            raise LaneReaderError("lane reader ghost has no public key; not sending")

    async def fetch_session(sid):
        fetched.append(sid)
        return session_meta(chat={"id": "chat-1", "readers": {"ids": ["d"], "open": False}})

    t = FakeTransport(open_result=session_meta(chat={"id": "chat-1", "readers": {"ids": ["d", "ghost"], "open": False}}), post_command=post_command, fetch_session=fetch_session)
    c = DeviceClient(t, DEVICE)
    await c.open()
    await c.amend_task("T1", "still goes")
    assert posts[0] == ("amend", ("d", "ghost"), True) and fetched == []
    job = asyncio.ensure_future(c.settings())
    await tick(10)
    job.cancel()
    assert fetched == [SESSION]
    assert [p[1] for p in posts[1:3]] == [("d", "ghost"), ("d",)]


async def test_http_lenient_skips_a_keyless_reader_but_never_sends_to_nobody(keypair_a):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"session": {"users": [{"id": "d", "public_key": keypair_a.public_key}]}})
        return httpx.Response(201, json={"id": "m1"})

    client = AsyncSaltClient("https://example.saltapp.test", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    tr = HttpDeviceTransport(client, "agent-key")
    with pytest.raises(LaneReaderError):
        await tr._keys_for(LaneReaders(chat_id="c", ids=("d", "ghost")))
    assert len(await tr._keys_for(LaneReaders(chat_id="c", ids=("d", "ghost"), lenient=True))) == 1
    with pytest.raises(LaneReaderError):
        await tr._keys_for(LaneReaders(chat_id="c", ids=("ghost",), lenient=True))
