# Device op `amend` (agent side): protocol helpers, DeviceClient.amend_task, the task's on_start id,
# and the withdraw of a waiting request. Mirrors salt-agent-sdk's tests/device-amend.test.js (the
# client cases); there is no Python device host.
from __future__ import annotations

import asyncio

import httpx
import pytest

from saltapp import device as dev
from saltapp.client import AsyncSaltClient
from saltapp.device import DeviceProtocolError, decode_device_message
from saltapp.device_client import DeviceClient, HttpDeviceTransport

from tests.test_device_client import DEVICE, SESSION, FakeTransport, active_client, reply, tick


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


def test_amend_command_has_re_and_body_id_uses_the_newest_seq_and_clips():
    m = decode_device_message(dev.amend_command("x" * 501, session="s", seq=0, task_id="T1", id="A1"))
    assert m.header.op == "amend" and m.header.re == "T1" and m.header.seq == 1
    assert m.body == {"id": "T1", "say": "x" * 500}
    m2 = decode_device_message(dev.amend_command("only task", session="s", seq=4))
    assert m2.header.re is None and m2.body == {"say": "only task"} and m2.header.seq == 4
    with pytest.raises(DeviceProtocolError) as e:
        dev.amend_command("  ", session="s", seq=1)
    assert e.value.code == "bad_args"


async def test_amend_task_posts_out_of_band_and_refuses_an_empty_say():
    c, t = await active_client()
    await c.amend_task("T1", "also close the tab")
    await c.amend_task(None, "only task")
    a, b = t.sent(0), t.sent(1)
    assert a.header.op == "amend" and a.header.re == "T1" and a.body == {"id": "T1", "say": "also close the tab"}
    assert b.header.re is None and b.body == {"say": "only task"}
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
