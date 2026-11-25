# saltapp.device_client: the agent side of device control. A fake transport stands in for
# Salt; deliveries and lane results are fed in the way a dispatcher would. No network.
# Mirrors salt-agent-sdk's tests/device-client.test.js, device-task.test.js (the client
# cases), device-settings.test.js, device-submit.test.js, device-locking.test.js (the client
# cases), device-round2.test.js (L-N1, L-N2), device-security.test.js (M7) and
# show-me-agent.test.js (the client cases). Host-side Node tests have no Python twin: there is
# no Python device host. The `wired` cases run the client against a small in-test fake device.
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx
import pytest

from saltapp import crypto, device_client
from saltapp.client import AsyncSaltClient
from saltapp.device import (
    AGENT_OPS,
    CAPABILITY_FOR_OP,
    DEVICE_RESULT_OPS,
    DeviceHeader,
    DeviceProtocolError,
    decode_device_message,
    encode_device_message,
)
from saltapp.device_client import (
    DeviceClient,
    DevicePausedError,
    DeviceSessionLiveError,
    DeviceSessionMeta,
    HttpDeviceTransport,
    LaneReaders,
)

DEVICE = "d"
SESSION = "sess-1"
CHAT = {"id": "chat-1", "readers": ["-----PUBKEY-----"]}


async def tick(n: int = 6) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def meta(**over: Any) -> dict[str, Any]:
    base = {"id": SESSION, "device_id": DEVICE, "agent_id": "ag", "mandate_id": "m", "status": "active", "chat": CHAT}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def active_delivery(**over: Any) -> dict[str, Any]:
    return {"type": "device_session_active", "session": meta(**over)}


class FakeTransport:
    def __init__(self, open_result: Any = None, **handlers: Any) -> None:
        self.posted: list[tuple[str, LaneReaders]] = []
        self.stopped: list[str] = []
        self.opened: list[Optional[str]] = []
        self.open_result = open_result if open_result is not None else meta(status="requested", chat=None)
        for name, fn in handlers.items():
            setattr(self, name, fn)

    async def open_session(self, device_id: str, *, intent: Optional[str] = None):
        self.opened.append(intent)
        if isinstance(self.open_result, Exception):
            raise self.open_result
        return self.open_result

    async def stop_session(self, session_id: str) -> None:
        self.stopped.append(session_id)

    async def post_command(self, plaintext: str, readers: LaneReaders) -> None:
        self.posted.append((plaintext, readers))

    def sent(self, i: int):
        return decode_device_message(self.posted[i][0])


async def active_client(t: Optional[FakeTransport] = None, **opts: Any):
    t = t or FakeTransport()
    c = DeviceClient(t, DEVICE, **opts)
    opened = asyncio.ensure_future(c.open())
    await tick()
    c.handle_delivery("device_session", active_delivery())
    await opened
    return c, t


def result(sent_plaintext_or_msg: Any, op: str, body: Any = None, *, rid: Optional[str] = None, seq: Optional[int] = None) -> str:
    m = decode_device_message(sent_plaintext_or_msg) if isinstance(sent_plaintext_or_msg, str) else sent_plaintext_or_msg
    return encode_device_message(
        DeviceHeader(v=1, id=rid or "r" + m.header.id, seq=seq or m.header.seq, op=op, session=SESSION, re=m.header.id), body
    )


def reply(c: DeviceClient, t: FakeTransport, i: int, op: str, body: Any = None, sender: str = DEVICE) -> bool:
    return c.handle_lane_message(result(t.sent(i), op, body), sender)


ACK = {"ok": True}


class ManualTimers:
    """Replaces the client's one timer seam so tests drive time by hand."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.timers: list[dict[str, Any]] = []
        monkeypatch.setattr(device_client, "_call_later", self._call_later)

    def _call_later(self, loop: Any, delay: float, fn: Any):
        timer = {"delay": delay, "fn": fn, "cancelled": False}
        self.timers.append(timer)

        class Handle:
            def cancel(self_inner) -> None:
                timer["cancelled"] = True

        return Handle()

    def live(self) -> list[dict[str, Any]]:
        return [t for t in self.timers if not t["cancelled"]]

    def fire_last(self) -> None:
        t = self.live()[-1]
        t["cancelled"] = True
        t["fn"]()


# ------------------------------------------------------------------ open / basics


async def test_open_resolves_when_the_device_approves_active_delivery():
    t = FakeTransport()
    c = DeviceClient(t, DEVICE)
    opened = asyncio.ensure_future(c.open())
    await tick()
    c.handle_delivery("device_session", active_delivery())
    m = await opened
    assert m.status == "active"
    assert c.session().status == "active"


async def test_a_command_posts_a_device_message_and_resolves_on_the_matching_result():
    c, t = await active_client()
    p = asyncio.ensure_future(c.observe())
    await tick()
    assert len(t.posted) == 1
    sent = t.sent(0)
    assert sent.header.op == "observe"
    assert sent.header.seq == 1
    assert sent.body == {"target": "screen"}
    answer = result(sent, "observation", {"obs": "o1", "width": 100, "height": 50, "scale": 2, "format": "png", "image_b64": "AAAA"})
    assert c.handle_lane_message(answer, DEVICE) is True
    shot = await p
    assert shot["obs"] == "o1" and shot["width"] == 100


async def test_an_error_result_rejects_the_command_with_its_code():
    c, t = await active_client()
    p = asyncio.ensure_future(c.click(1, 1))
    await tick()
    reply(c, t, 0, "error", {"code": "out_of_scope", "message": "no device.act"})
    with pytest.raises(DeviceProtocolError) as e:
        await p
    assert e.value.code == "out_of_scope"


async def test_seq_increments_per_command_one_in_flight_at_a_time():
    c, t = await active_client()
    a = asyncio.ensure_future(c.type("a"))
    b = asyncio.ensure_future(c.type("b"))
    await tick()
    assert t.sent(0).header.seq == 1
    assert len(t.posted) == 1  # "b" waits for "a"
    reply(c, t, 0, "ack", ACK)
    await a
    await tick()
    assert t.sent(1).header.seq == 2
    reply(c, t, 1, "ack", ACK)
    await b


async def test_session_ended_rejects_pending_commands_and_fires_ended():
    c, t = await active_client()
    ended: list[str] = []
    c.on("ended", ended.append)
    p = asyncio.ensure_future(c.type("hello"))
    await tick()
    c.handle_delivery("device_session", {"type": "device_session_ended", "session": {"id": SESSION}, "end_reason": "stopped_by_person"})
    with pytest.raises(DeviceProtocolError):
        await p
    assert ended == ["stopped_by_person"]


async def test_stop_calls_the_transport_and_ends_the_session():
    c, t = await active_client()
    await c.stop()
    assert t.stopped == [SESSION]
    assert c.session().status == "ended"
    await c.stop()  # safe to call more than once
    assert t.stopped == [SESSION, SESSION]


async def test_a_result_for_someone_elses_session_is_ignored():
    c, t = await active_client()
    stray = encode_device_message(DeviceHeader(v=1, id="r", seq=1, op="ack", session="OTHER", re="x"), ACK)
    assert c.handle_lane_message(stray, DEVICE) is False


async def test_the_device_client_accepts_lane_messages_only_from_the_devices_own_account():
    c, t = await active_client()
    p = asyncio.ensure_future(c.observe())
    await tick()
    sent = t.sent(0)
    forged = result(sent, "observation", {"obs": "fake", "image_b64": "AA"})
    for who in ("person-1", "another-agent", "", None):
        assert c.handle_lane_message(forged, who) is False
    await tick()
    assert not p.done()
    real = result(sent, "observation", {"obs": "real", "image_b64": "AA"}, rid="r2", seq=4)
    assert c.handle_lane_message(real, DEVICE.upper()) is True  # ids compare as ids
    assert (await p)["obs"] == "real"


async def test_a_lane_message_that_is_not_a_device_line_is_not_consumed():
    c, t = await active_client()
    assert c.handle_lane_message("hello there", DEVICE) is False
    assert c.handle_lane_message("[[SALT-DEVICE v=1 broken", DEVICE) is False


# ------------------------------------------------------------------ the commands' wire bodies


async def run_command(coro_fn, op: str, body: Any, reply_op: str = "ack", reply_body: Any = ACK):
    c, t = await active_client()
    p = asyncio.ensure_future(coro_fn(c))
    await tick()
    sent = t.sent(0)
    assert sent.header.op == op
    assert sent.body == body
    reply(c, t, 0, reply_op, reply_body)
    return await p, sent


async def test_every_command_sends_its_op_and_body_and_resolves_on_the_right_result():
    EL = {"obs": "o1", "node": "n7"}
    _, s = await run_command(lambda c: c.click(10, 20, button="right", count=2, obs="o1", element=EL), "click", {"x": 10, "y": 20, "button": "right", "count": 2, "obs": "o1", "element": EL})
    assert s.header.exp is not None
    await run_command(lambda c: c.click(1, 2), "click", {"x": 1, "y": 2})
    await run_command(lambda c: c.type("hi"), "type", {"text": "hi"})
    await run_command(lambda c: c.key(["cmd", "s"]), "key", {"keys": ["cmd", "s"]})
    await run_command(lambda c: c.scroll(1, 2, 0, -5), "scroll", {"x": 1, "y": 2, "dx": 0, "dy": -5})
    await run_command(lambda c: c.focus_app("com.apple.Safari"), "focus_app", {"app": "com.apple.Safari"})
    await run_command(lambda c: c.write_file("/tmp/a", "AAAA"), "write_file", {"path": "/tmp/a", "content_b64": "AAAA"})
    apps, sent = await run_command(lambda c: c.list_apps(), "list_apps", None, "apps", {"apps": [{"id": "x", "name": "X", "frontmost": True}]})
    assert apps["apps"][0]["name"] == "X"
    f, _ = await run_command(lambda c: c.read_file("/tmp/a"), "read_file", {"path": "/tmp/a"}, "file", {"path": "/tmp/a", "size": 3, "content_b64": "AAAA"})
    assert f["size"] == 3
    await run_command(
        lambda c: c.observe("window", window="Mail", want="pixels", max_width=800, format="jpeg", display=1),
        "observe",
        {"target": "window", "window": "Mail", "want": "pixels", "max_width": 800, "format": "jpeg", "display": 1},
        "observation",
        {"obs": "o"},
    )


async def test_submit_posts_a_submit_command_carrying_the_element_and_resolves_on_ack():
    EL = {"obs": "o1", "node": "n3"}
    await run_command(lambda c: c.submit(EL), "submit", {"element": EL})


async def test_an_ask_result_raises_needs_approval_and_fires_ask():
    c, t = await active_client()
    asks: list[str] = []
    c.on("ask", asks.append)
    p = asyncio.ensure_future(c.type("x"))
    await tick()
    reply(c, t, 0, "ask", {"reason": "the person must approve at the computer"})
    with pytest.raises(DeviceProtocolError) as e:
        await p
    assert e.value.code == "needs_approval"
    assert asks == ["the person must approve at the computer"]


async def test_the_wrong_result_op_is_a_failure():
    c, t = await active_client()
    p = asyncio.ensure_future(c.type("x"))
    await tick()
    reply(c, t, 0, "observation", {"obs": "o"})
    with pytest.raises(DeviceProtocolError) as e:
        await p
    assert e.value.code == "failed" and "expected ack" in str(e.value)


def test_the_capability_table_and_op_lists_are_the_shared_ones():
    assert DeviceClient.capability_for_op is CAPABILITY_FOR_OP
    assert "settings" in AGENT_OPS and "settings" in DEVICE_RESULT_OPS
    assert CAPABILITY_FOR_OP["settings"] is None
    assert "apps_access" in AGENT_OPS and "apps_access" in DEVICE_RESULT_OPS
    assert CAPABILITY_FOR_OP["apps_access"] == "device.apps"


async def test_apps_access_sends_an_empty_read_and_resolves_with_the_list():
    body = {"as_of": "2026-11-17T00:00:00Z", "system": {"accessibility": {"state": "granted"}, "screen_recording": {"state": "off"}}, "apps": []}
    r, _ = await run_command(lambda c: c.apps_access(), "apps_access", None, "apps_access", body)
    assert r == body


# ------------------------------------------------------------------ settings


SETTINGS = {
    "settings": {"stay_awake": True, "commands_enabled": True},
    "state": {"on_ac": True, "battery_pct": 80, "lid_closed": False, "display": "on", "holding_awake": True},
    "changed": ["stay_awake"],
    "refused": {"color": "unknown_setting"},
}


async def test_settings_sends_an_empty_body_settings_command_and_resolves_with_the_result():
    r, _ = await run_command(lambda c: c.settings(), "settings", {}, "settings", SETTINGS)
    assert r == SETTINGS


async def test_set_settings_sends_set_and_returns_changed_and_refused():
    r, _ = await run_command(lambda c: c.set_settings({"stay_awake": True}), "settings", {"set": {"stay_awake": True}}, "settings", SETTINGS)
    assert r["changed"] == ["stay_awake"] and r["refused"]["color"] == "unknown_setting"


async def test_a_declined_set_comes_back_as_data_in_refused_not_a_raise():
    declined = {**SETTINGS, "changed": [], "refused": {"stay_awake": "declined"}}
    r, _ = await run_command(lambda c: c.set_settings({"stay_awake": False}), "settings", {"set": {"stay_awake": False}}, "settings", declined)
    assert r["refused"] == {"stay_awake": "declined"}


# ------------------------------------------------------------------ whole tasks


def ok(**over: Any) -> dict[str, Any]:
    base = {
        "task_id": "t_abc123", "status": "done", "summary": "Done.",
        "checked": {"ok": True, "what": "the page title says saltapp.ai"},
        "steps": [{"n": 1, "kind": "command", "line": "open https://saltapp.ai", "ok": True}],
        "commands": 1, "duration_ms": 1200,
    }
    base.update(over)
    return base


def progress(c: DeviceClient, re: str, seq: int, body: Any) -> bool:
    return c.handle_lane_message(
        encode_device_message(DeviceHeader(v=1, id=f"p{seq}", seq=seq, op="progress", session=SESSION, re=re), body), DEVICE
    )


async def test_task_sends_goal_done_when_max_steps_timeout_s_delivers_progress_in_order_and_resolves():
    c, t = await active_client()
    lines = []
    p = asyncio.ensure_future(c.task("Open saltapp.ai", done_when="title says Salt", max_steps=10, timeout_s=60, on_progress=lines.append))
    await tick()
    sent = t.sent(0)
    assert sent.header.op == "task"
    assert sent.body == {"goal": "Open saltapp.ai", "done_when": "title says Salt", "max_steps": 10, "timeout_s": 60}
    progress(c, sent.header.id, 2, {"step": 1, "of": 2, "line": "Opening Firefox"})
    progress(c, sent.header.id, 3, {"step": 2, "of": 2, "line": "Checking the title", "waiting": False})
    reply(c, t, 0, "task_result", ok())
    r = await p
    assert [x.to_dict() for x in lines] == [
        {"step": 1, "of": 2, "line": "Opening Firefox"},
        {"step": 2, "of": 2, "line": "Checking the title", "waiting": False},
    ]
    assert r.status == "done" and r.checked.ok is True and r.steps[0].kind == "command"


async def test_task_with_only_a_goal_sends_only_the_goal_and_bad_arguments_are_refused_before_sending():
    c, t = await active_client()
    p = asyncio.ensure_future(c.task("Just do it"))
    await tick()
    assert t.sent(0).body == {"goal": "Just do it"}
    reply(c, t, 0, "task_result", ok())
    await p
    before = len(t.posted)
    for bad in (dict(goal=""), dict(goal="g", max_steps=1.5), dict(goal="x" * 2001), dict(goal="g", done_when="d" * 501)):
        with pytest.raises(DeviceProtocolError) as e:
            await c.task(**bad)
        assert e.value.code == "bad_args"
    assert len(t.posted) == before


async def test_a_computer_without_a_task_handler_raises_unsupported_and_a_failure_raises_failed():
    c, t = await active_client()
    p = asyncio.ensure_future(c.task("g"))
    await tick()
    reply(c, t, 0, "error", {"code": "unsupported", "message": "no task"})
    with pytest.raises(DeviceProtocolError) as e:
        await p
    assert e.value.code == "unsupported"
    p = asyncio.ensure_future(c.task("g"))
    await tick()
    reply(c, t, 1, "error", {"code": "failed", "message": "model down"})
    with pytest.raises(DeviceProtocolError) as e:
        await p
    assert e.value.code == "failed" and "model down" in str(e.value)


async def test_the_result_is_allow_listed_again_on_this_side():
    c, t = await active_client()
    p = asyncio.ensure_future(c.task("g"))
    await tick()
    body = {**ok(summary="z" * 900), "image_b64": "AAAA", "steps": [{"n": 1, "kind": "command", "line": "a", "ok": True, "output": "x"}]}
    reply(c, t, 0, "task_result", body)
    r = await p
    assert len(r.summary) == 500
    assert sorted(r.to_dict()) == ["checked", "commands", "duration_ms", "status", "steps", "summary", "task_id"]
    assert sorted(r.steps[0].to_dict()) == ["kind", "line", "n", "ok"]


async def test_progress_out_of_order_is_dropped_extras_stripped_and_a_raising_callback_is_harmless():
    c, t = await active_client()
    got = []

    def cb(p):
        got.append(p)
        raise RuntimeError("callback bug")

    p = asyncio.ensure_future(c.task("g", on_progress=cb))
    await tick()
    re = t.sent(0).header.id
    assert progress(c, re, 2, {"step": 2, "line": "two", "secret": "x"}) is True
    assert progress(c, re, 1, {"step": 1, "line": "one"}) is True  # late: consumed, not delivered
    assert progress(c, re, 3, {"nope": True}) is True  # not a progress line: consumed, not delivered
    assert progress(c, re, 4, {"step": 3, "line": "three"}) is True
    assert [x.to_dict() for x in got] == [{"step": 2, "line": "two"}, {"step": 3, "line": "three"}]
    c.handle_lane_message(result(t.sent(0), "task_result", ok(), seq=5), DEVICE)
    assert (await p).status == "done"
    # after the result, a straggling progress line is not ours any more
    assert progress(c, re, 6, {"step": 4, "line": "late"}) is False


async def test_no_result_within_timeout_s_plus_30_sends_cancel_with_re_then_raises(monkeypatch):
    timers = ManualTimers(monkeypatch)
    c, t = await active_client()
    p = asyncio.ensure_future(c.task("g", timeout_s=20))
    await tick()
    assert timers.live()[-1]["delay"] == 50.0  # 20 + 30, not a second sooner
    timers.fire_last()
    with pytest.raises(DeviceProtocolError, match="timed out"):
        await p
    assert len(t.posted) == 2
    cancel = t.sent(1)
    assert cancel.header.op == "cancel"
    assert cancel.header.re == t.sent(0).header.id
    assert cancel.body == {"id": t.sent(0).header.id}


async def test_the_default_task_wait_is_180_plus_30(monkeypatch):
    timers = ManualTimers(monkeypatch)
    c, t = await active_client()
    p = asyncio.ensure_future(c.task("g"))
    await tick()
    assert timers.live()[-1]["delay"] == 210.0
    timers.fire_last()
    with pytest.raises(DeviceProtocolError, match="timed out"):
        await p
    assert t.sent(1).header.op == "cancel"


async def test_aborting_sends_cancel_with_re_and_the_devices_stopped_result_resolves_the_call():
    c, t = await active_client()
    abort = asyncio.Event()
    p = asyncio.ensure_future(c.task("g", abort=abort))
    await tick()
    abort.set()
    await tick()
    cancel = next(t.sent(i) for i in range(len(t.posted)) if t.sent(i).header.op == "cancel")
    assert cancel.header.re == t.sent(0).header.id
    c.handle_lane_message(result(t.sent(0), "task_result", ok(status="stopped", summary="Stopped after two steps.", checked=None)), DEVICE)
    r = await p
    assert r.status == "stopped" and r.summary == "Stopped after two steps."


async def test_an_abort_the_device_never_answers_raises_after_the_grace_and_a_set_event_sends_nothing(monkeypatch):
    c, t = await active_client()
    pre = asyncio.Event()
    pre.set()
    with pytest.raises(DeviceProtocolError, match="aborted"):
        await c.task("g", abort=pre)
    assert len(t.posted) == 0

    timers = ManualTimers(monkeypatch)
    abort = asyncio.Event()
    p = asyncio.ensure_future(c.task("g", abort=abort))
    await tick()
    abort.set()
    await tick()
    assert sum(1 for i in range(len(t.posted)) if t.sent(i).header.op == "cancel") == 1
    assert timers.live()[-1]["delay"] == 10.0  # the grace for the device's own `stopped`
    timers.fire_last()
    with pytest.raises(DeviceProtocolError, match="aborted"):
        await p


async def test_a_paused_session_refuses_a_task_up_front_with_device_paused_error():
    c, t = await active_client()
    c.handle_delivery("device_session", {"id": SESSION, "status": "paused", "pause_reason": "person", "type": "device_session_paused", "event": "paused", "session_id": SESSION})
    with pytest.raises(DevicePausedError):
        await c.task("g")
    assert len(t.posted) == 0


# ------------------------------------------------------------------ a fake device wired to the client


class FakeDevice:
    """Answers commands straight back through handle_lane_message, one at a time; records order."""

    def __init__(self, handlers: dict[str, Any]) -> None:
        self.handlers = handlers
        self.order: list[str] = []
        self.client: Optional[DeviceClient] = None

    async def on_command(self, plaintext: str, readers: LaneReaders) -> None:
        msg = decode_device_message(plaintext)
        if msg.header.op == "cancel":
            return
        asyncio.ensure_future(self._run(msg))

    async def _run(self, msg) -> None:
        handler = self.handlers.get(msg.header.op)
        if handler is None:
            op, body = "error", {"code": "unsupported", "message": f"no {msg.header.op}"}
        else:
            op, body = await handler(msg)
        self.client.handle_lane_message(result(msg, op, body), DEVICE)


async def wired(handlers: dict[str, Any]):
    fd = FakeDevice(handlers)
    t = FakeTransport(open_result=meta())

    async def post(plaintext: str, readers: LaneReaders) -> None:
        t.posted.append((plaintext, readers))
        await fd.on_command(plaintext, readers)

    t.post_command = post  # type: ignore[method-assign]
    c = DeviceClient(t, DEVICE)
    fd.client = c
    await c.open()
    return c, fd, t


async def test_round_trip_a_task_is_one_command_in_flight_and_a_click_queued_behind_it_waits():
    gate = asyncio.Event()
    holder: dict[str, Any] = {}

    async def task(msg):
        holder["fd"].order.append("task start")
        await gate.wait()
        holder["fd"].order.append("task end")
        return "task_result", ok()

    async def click(msg):
        holder["fd"].order.append("click")
        return "ack", ACK

    c, fd, t = await wired({"task": task, "click": click})
    holder["fd"] = fd
    task_f = asyncio.ensure_future(c.task("g"))
    click_f = asyncio.ensure_future(c.click(1, 1))
    await asyncio.sleep(0.02)
    assert fd.order == ["task start"]
    gate.set()
    await asyncio.gather(task_f, click_f)
    assert fd.order == ["task start", "task end", "click"]


async def test_concurrent_callers_are_serialised_each_command_has_an_id_seq_and_exp():
    c, t = await active_client()
    before = int(time.time() * 1000)
    ps = [asyncio.ensure_future(x) for x in (c.type("a"), c.key(["x"]), c.type("b"))]
    await tick()
    assert len(t.posted) == 1
    for i in range(3):
        reply(c, t, i, "ack", ACK)
        await tick()
    await asyncio.gather(*ps)
    assert [t.sent(i).header.seq for i in range(3)] == [1, 2, 3]
    assert len({t.sent(i).header.id for i in range(3)}) == 3
    for i in range(3):
        exp = t.sent(i).header.exp
        assert before + 29_000 <= exp <= int(time.time() * 1000) + 30_000


async def test_out_of_order_resyncs_once_and_resends_with_the_same_id():
    c, t = await active_client()
    p = asyncio.ensure_future(c.type("a"))
    await tick()
    reply(c, t, 0, "error", {"code": "out_of_order", "expected_seq": 5})
    await tick()
    assert len(t.posted) == 2
    assert t.sent(1).header.id == t.sent(0).header.id
    assert t.sent(1).header.seq == 5
    reply(c, t, 1, "ack", ACK)
    await p
    p2 = asyncio.ensure_future(c.type("b"))  # the next command continues from the resynced seq
    await tick()
    assert t.sent(2).header.seq == 6
    reply(c, t, 2, "ack", ACK)
    await p2


async def test_a_second_out_of_order_is_surfaced_not_retried_forever():
    c, t = await active_client()
    p = asyncio.ensure_future(c.type("a"))
    await tick()
    reply(c, t, 0, "error", {"code": "out_of_order", "expected_seq": 5})
    await tick()
    reply(c, t, 1, "error", {"code": "out_of_order", "expected_seq": 9})
    with pytest.raises(DeviceProtocolError) as e:
        await p
    assert e.value.code == "out_of_order"
    assert len(t.posted) == 2


async def test_expired_is_resent_once_with_the_same_id_and_a_fresh_exp():
    c, t = await active_client()
    p = asyncio.ensure_future(c.type("a"))
    await tick()
    reply(c, t, 0, "error", {"code": "expired", "expected_seq": 1})
    await tick()
    assert t.sent(1).header.id == t.sent(0).header.id
    assert t.sent(1).header.seq == 1
    reply(c, t, 1, "ack", ACK)
    await p


async def test_on_timeout_it_sends_cancel_with_the_commands_id():
    t = FakeTransport()
    c, t = await active_client(t, command_timeout_s=0.02)
    with pytest.raises(DeviceProtocolError, match="timed out"):
        await c.type("a")
    cancel = t.sent(len(t.posted) - 1)
    assert cancel.header.op == "cancel"
    assert cancel.body == {"id": t.sent(0).header.id}


async def test_a_host_paused_reply_raises_device_paused_error_and_does_not_burn_a_seq():
    c, t = await active_client()
    p = asyncio.ensure_future(c.type("a"))
    await tick()
    reply(c, t, 0, "error", {"code": "paused", "reason": "secure_field", "expected_seq": 1})
    with pytest.raises(DevicePausedError) as e:
        await p
    assert e.value.reason == "secure_field" and e.value.code == "paused"
    p2 = asyncio.ensure_future(c.type("b"))
    await tick()
    assert t.sent(1).header.seq == 1
    reply(c, t, 1, "ack", ACK)
    await p2


async def test_while_paused_by_delivery_commands_reject_immediately_unsent_and_resumed_clears():
    c, t = await active_client()
    states: list[dict] = []
    c.on_state(states.append)
    c.handle_delivery("device_session", {"event": "paused", "session_id": SESSION, "status": "paused", "pause_reason": "person_active"})
    with pytest.raises(DevicePausedError) as e:
        await c.click(1, 1)
    assert e.value.reason == "person_active"
    assert len(t.posted) == 0
    assert c.paused_reason() == "person_active"
    c.handle_delivery("device_session", {"event": "resumed", "session_id": SESSION, "status": "active"})
    p = asyncio.ensure_future(c.type("a"))
    await tick()
    assert len(t.posted) == 1
    reply(c, t, 0, "ack", ACK)
    await p
    assert states == [{"state": "paused", "reason": "person_active"}, {"state": "resumed"}]


# ------------------------------------------------------------------ open: queue, approval, reattach


async def test_queued_open_waits_on_deliveries_without_polling_and_resolves_on_promotion():
    fetches: list[str] = []

    async def fetch_session(sid):
        fetches.append(sid)
        return meta()

    t = FakeTransport(open_result=meta(status="queued", position=2, chat=None), fetch_session=fetch_session)
    c = DeviceClient(t, DEVICE, approval_timeout_s=0.02)
    states: list[dict] = []
    c.on_state(states.append)
    done = []
    opened = asyncio.ensure_future(c.open())
    opened.add_done_callback(lambda f: done.append(1))
    await tick()
    await asyncio.sleep(0.06)  # longer than the approval timeout: queued has no clock
    assert done == []
    c.handle_delivery("device_session", {"event": "queue_moved", "session_id": SESSION, "position": 1})
    c.handle_delivery("device_session", {"event": "promoted", "session_id": SESSION, "status": "active", "device_id": DEVICE})
    m = await opened
    assert m.status == "active"
    assert fetches == [SESSION]  # readers learned once, not polled
    assert [s["state"] for s in states] == ["queued", "queued", "active"]
    assert states[0]["position"] == 2 and states[1]["position"] == 1
    p = asyncio.ensure_future(c.type("a"))  # and commands now flow
    await tick()
    assert len(t.posted) == 1
    reply(c, t, 0, "ack", ACK)
    await p


async def test_promoted_to_requested_starts_the_approval_clock_then_active_resolves_open():
    t = FakeTransport(open_result=meta(status="queued", position=1, chat=None))
    c = DeviceClient(t, DEVICE)
    states: list[str] = []
    c.on_state(lambda s: states.append(s["state"]))
    opened = asyncio.ensure_future(c.open())
    await tick()
    c.handle_delivery("device_session", {"event": "promoted", "session_id": SESSION, "status": "requested"})
    c.handle_delivery("device_session", active_delivery())
    assert (await opened).status == "active"
    assert states == ["queued", "requested", "active"]


async def test_requested_that_the_device_never_approves_fails_after_the_approval_timeout():
    t = FakeTransport()  # answers "requested"
    c = DeviceClient(t, DEVICE, approval_timeout_s=0.02)
    with pytest.raises(DeviceProtocolError, match="did not approve in time"):
        await c.open()


async def test_queued_then_ended_rejects_open_with_the_reason():
    t = FakeTransport(open_result=meta(status="queued", position=1, chat=None))
    c = DeviceClient(t, DEVICE)
    opened = asyncio.ensure_future(c.open())
    await tick()
    c.handle_delivery("device_session", {"event": "ended", "session_id": SESSION, "end_reason": "queue_expired"})
    with pytest.raises(DeviceProtocolError, match="queue_expired"):
        await opened


async def test_open_wait_false_returns_the_queued_state_at_once_with_the_position():
    t = FakeTransport(open_result=meta(status="queued", position=3, chat=None))
    c = DeviceClient(t, DEVICE)
    states: list[dict] = []
    c.on_state(states.append)
    m = await c.open(wait=False)
    assert m.status == "queued" and m.position == 3
    assert states == [{"state": "queued", "position": 3}]
    # later promotion still arrives via on_state
    c.handle_delivery("device_session", {"event": "promoted", "session_id": SESSION, "status": "active", "session": meta()})
    assert c.session().status == "active"
    assert states[1]["state"] == "active"


async def test_open_passes_the_intent_through_and_an_open_session_is_returned_as_is():
    c, t = await active_client()
    assert t.opened == [None]
    t2 = FakeTransport()
    c2 = DeviceClient(t2, DEVICE)
    task = asyncio.ensure_future(c2.open(intent="Download the three invoices"))
    await tick()
    c2.handle_delivery("device_session", active_delivery())
    first = await task
    assert t2.opened == ["Download the three invoices"]
    assert await c2.open() is first  # already active: no second POST
    assert t2.opened == ["Download the three invoices"]


async def test_409_session_live_reattaches_and_continues_at_last_seq_plus_1():
    reattached: list[str] = []

    async def reattach_session(sid):
        reattached.append(sid)
        return {"session": meta(), "last_seq": 7}

    t = FakeTransport(open_result=DeviceSessionLiveError(SESSION), reattach_session=reattach_session)
    c = DeviceClient(t, DEVICE)
    m = await c.open()
    assert reattached == [SESSION]
    assert m.status == "active"
    p = asyncio.ensure_future(c.type("a"))
    await tick()
    assert t.sent(0).header.seq == 8
    reply(c, t, 0, "ack", ACK)
    await p


async def test_reattaching_a_paused_session_comes_back_paused():
    async def reattach_session(sid):
        return {"session": meta(status="paused", pause_reason="secure_field"), "last_seq": 2}

    t = FakeTransport(open_result=DeviceSessionLiveError(SESSION), reattach_session=reattach_session)
    c = DeviceClient(t, DEVICE)
    await c.open()
    with pytest.raises(DevicePausedError):
        await c.type("a")


async def test_deliveries_for_another_device_and_an_ended_for_another_session_are_ignored():
    t = FakeTransport(open_result=meta(id="s-new", status="requested", chat=None))
    c = DeviceClient(t, "dev-1")
    opened = asyncio.ensure_future(c.open())
    await tick()
    c.handle_delivery("device_session", {"type": "device_session_ended", "session_id": "s-old", "device_id": "dev-1", "end_reason": "agent_idle"})
    c.handle_delivery("device_session", {"type": "device_session_ended", "session_id": "s-other-device", "device_id": "dev-2", "end_reason": "x"})
    c.handle_delivery("device_session", {"type": "device_session_active", "session": meta(id="s-new", device_id="dev-1", chat={"id": "c", "readers": ["K"]})})
    m = await opened
    assert m.status == "active" and m.id == "s-new"
    # once known, other sessions are ignored too
    c.handle_delivery("device_session", {"type": "device_session_ended", "session_id": "s-old", "device_id": "dev-1", "end_reason": "agent_idle"})
    assert c.session().status == "active"


async def test_a_delivery_that_is_not_a_device_session_one_is_ignored():
    c, t = await active_client()
    c.handle_delivery("message", {"event": "ended", "session_id": SESSION})
    assert c.session().status == "active"


async def test_two_sequential_sessions_on_one_client_do_not_leak_the_first_into_the_second():
    n = {"i": 0}

    async def open_session(device_id, *, intent=None):
        n["i"] += 1
        i = n["i"]
        return meta(id=f"s{i}", chat={"id": f"chat-{i}", "readers": [f"KEY{i}"]})

    t = FakeTransport(open_session=open_session)
    c = DeviceClient(t, DEVICE, command_timeout_s=0.5)
    await c.open()
    assert c.session().id == "s1"
    await c.stop()
    second = await c.open()
    assert second.id == "s2"
    assert c.session().status == "active"
    p = asyncio.ensure_future(c.click(1, 1))
    await asyncio.sleep(0.01)
    text, readers = t.posted[-1]
    assert readers.chat_id == "chat-2" and readers.public_keys == ("KEY2",)
    h = decode_device_message(text).header
    assert h.session == "s2" and h.seq == 1
    p.cancel()


# ------------------------------------------------------------------ clock skew


async def test_a_device_clock_far_behind_ours_is_corrected_once_and_the_command_runs_with_the_same_id():
    skew = 120_000
    seen: list[Any] = []

    async def click(msg):
        device_now = int(time.time() * 1000) - skew
        if msg.header.exp is not None and msg.header.exp > device_now + 30_000 + 1000:
            return "error", {"code": "clock_skew", "now": device_now}
        seen.append(msg)
        return "ack", ACK

    c, fd, t = await wired({"click": click})
    await c.click(1, 1)
    assert len(seen) == 1
    assert len({decode_device_message(p).header.id for p, _ in t.posted}) == 1  # same id on the resend
    assert len(t.posted) == 2
    await c.click(2, 2)  # the offset sticks: the next command needs no retry
    assert len(seen) == 2 and len(t.posted) == 3


async def test_clock_skew_is_corrected_with_an_injected_clock_and_only_once():
    c, t = await active_client(clock_ms=lambda: 1_000_000)
    p = asyncio.ensure_future(c.type("a"))
    await tick()
    assert t.sent(0).header.exp == 1_000_000 + 30_000
    reply(c, t, 0, "error", {"code": "clock_skew", "now": 5_000_000})
    await tick()
    assert t.sent(1).header.exp == 5_000_000 + 30_000  # the device's clock, not ours
    reply(c, t, 1, "error", {"code": "clock_skew", "now": 5_000_000})
    with pytest.raises(DeviceProtocolError) as e:
        await p
    assert e.value.code == "clock_skew"
    assert len(t.posted) == 2


# ------------------------------------------------------------------ Show Me


STILL = {"help_id": "h", "obs": "O1", "width": 10, "height": 10, "scale": 1, "format": "jpeg", "image_b64": "AA", "taken_at": "t"}
POINTED = {"help_id": "h", "x": 0.5, "y": 0.5, "screen": {"x": 1, "y": 2}, "app": "Finder", "element": None, "refused": None, "words": ""}


def unsolicited(op: str, seq: int, body: Any, *, re: Optional[str] = None, session: str = SESSION) -> str:
    return encode_device_message(DeviceHeader(v=1, id=f"u{seq}", seq=seq, op=op, session=session, re=re), body)


async def test_a_still_and_a_pointed_result_surface_through_on_still_and_on_pointed():
    c, t = await active_client()
    stills, pointed = [], []
    off_still = c.on_still(stills.append)
    c.on_pointed(lambda p, pid: pointed.append((p, pid)))
    assert c.handle_lane_message(unsolicited("still", 1, STILL), DEVICE) is True
    assert len(stills) == 1 and stills[0].obs == "O1"
    assert c.handle_lane_message(unsolicited("pointed", 2, POINTED, re="POINT1"), DEVICE) is True
    assert pointed[0][1] == "POINT1" and pointed[0][0].app == "Finder"
    c.handle_lane_message(unsolicited("pointed", 3, {"nope": 1}, re="P2"), DEVICE)  # malformed: consumed, never delivered
    assert len(pointed) == 1
    assert c.handle_lane_message(unsolicited("still", 4, STILL, session="other"), DEVICE) is False
    off_still()
    c.handle_lane_message(unsolicited("still", 5, STILL), DEVICE)
    assert len(stills) == 1


async def test_a_raising_on_pointed_listener_never_breaks_the_stream():
    c, t = await active_client()

    def boom(p, pid):
        raise RuntimeError("boom")

    c.on_pointed(boom)
    got = []
    c.on_pointed(lambda p, pid: got.append(p))
    assert c.handle_lane_message(unsolicited("pointed", 1, POINTED, re="P"), DEVICE) is True
    assert len(got) == 1


async def test_stills_and_pointed_from_anyone_but_the_device_are_ignored():
    c, t = await active_client()
    stills, pointed = [], []
    c.on_still(stills.append)
    c.on_pointed(lambda p, pid: pointed.append(p))
    for who in ("person-1", "another-agent", "", None):
        assert c.handle_lane_message(unsolicited("still", 1, STILL), who) is False
        assert c.handle_lane_message(unsolicited("pointed", 2, POINTED, re="P"), who) is False
    assert not stills and not pointed
    assert c.handle_lane_message(unsolicited("pointed", 3, POINTED, re="P"), DEVICE.upper()) is True
    assert len(pointed) == 1


# ------------------------------------------------------------------ readers


async def test_readers_named_by_account_id_are_carried_to_the_transport_without_keys():
    t = FakeTransport(open_result=meta(chat={"id": "lane-1", "readers": {"ids": ["dev", "owner"], "open": False}}))
    c = DeviceClient(t, DEVICE)
    await c.open()
    p = asyncio.ensure_future(c.type("a"))
    await tick()
    _, readers = t.posted[0]
    assert readers == LaneReaders(chat_id="lane-1", public_keys=(), ids=("dev", "owner"))
    p.cancel()


async def test_active_without_readers_fetches_the_session_once_and_without_a_fetch_commands_say_so():
    t = FakeTransport(open_result=meta(chat=None))
    c = DeviceClient(t, DEVICE)
    await c.open()  # active, no readers, no fetch_session on the transport
    with pytest.raises(DeviceProtocolError, match="lane readers unknown"):
        await c.type("a")


# ------------------------------------------------------------------ the HTTP transport


HOST = "https://example.saltapp.test"
AGENT_KEY = "agent-key"
DEVICE_ID = "00000000-0000-0000-0000-0000000000d3"
OWNER_ID = "00000000-0000-0000-0000-0000000000b2"


def http_transport(handler):
    mock = httpx.MockTransport(handler)
    client = AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=mock))
    return HttpDeviceTransport(client, AGENT_KEY)


async def test_http_open_session_posts_the_intent_and_unwraps_a_202_queued_session():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("api-key"), request.content))
        return httpx.Response(202, json={"session": {"id": SESSION, "status": "queued", "position": 2, "device_id": DEVICE_ID}})

    m = await http_transport(handler).open_session(DEVICE_ID, intent="Reply to Maya in Mail")
    assert seen == [("POST", f"/api/v1/devices/{DEVICE_ID}/sessions", AGENT_KEY, b'{"intent":"Reply to Maya in Mail"}')]
    assert m.status == "queued" and m.position == 2

    seen.clear()
    await http_transport(lambda r: (seen.append(r.content), httpx.Response(201, json=meta()))[1]).open_session(DEVICE_ID)
    assert seen == [b""]  # no intent, no body


async def test_http_open_session_409_session_live_becomes_device_session_live_error_and_other_refusals_pass_through():
    live = http_transport(lambda r: httpx.Response(409, json={"code": "session_live", "session_id": SESSION, "error": "live"}))
    with pytest.raises(DeviceSessionLiveError) as e:
        await live.open_session(DEVICE_ID)
    assert e.value.session_id == SESSION
    long = http_transport(lambda r: httpx.Response(422, json={"error": "Intent is over 140 characters.", "code": "intent_too_long"}))
    with pytest.raises(Exception) as e2:
        await long.open_session(DEVICE_ID, intent="x" * 200)
    assert "Intent is over 140 characters." in str(e2.value) and e2.value.code == "intent_too_long"


async def test_http_reattach_fetch_and_stop_use_the_session_routes():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/reattach"):
            return httpx.Response(200, json={"session": meta(), "last_seq": 4})
        if request.url.path.endswith("/stop"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"session": meta(status="active")})

    tr = http_transport(handler)
    r = await tr.reattach_session(SESSION)
    assert r["last_seq"] == 4 and r["session"].id == SESSION
    assert (await tr.fetch_session(SESSION)).status == "active"
    await tr.stop_session(SESSION)
    assert calls == [
        ("POST", f"/api/v1/device_sessions/{SESSION}/reattach"),
        ("GET", f"/api/v1/device_sessions/{SESSION}"),
        ("POST", f"/api/v1/device_sessions/{SESSION}/stop"),
    ]
    ended = http_transport(lambda r: httpx.Response(409, json={"end_reason": "stopped_by_person"}))
    with pytest.raises(DeviceProtocolError, match="session ended: stopped_by_person"):
        await ended.reattach_session(SESSION)


async def test_http_post_command_encrypts_for_the_lane_readers_looked_up_by_id_and_posts_quietly(keypair_a, keypair_b):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == "/api/v1/chats/lane-1"
            return httpx.Response(200, json={"session": {"users": [
                {"id": DEVICE_ID, "public_key": keypair_a.public_key},
                {"id": OWNER_ID.upper(), "public_key": keypair_b.public_key},
                {"id": "someone-else", "public_key": "NOT-A-KEY"},
            ]}})
        posted.append(request.read())
        return httpx.Response(200, json={})

    tr = http_transport(handler)
    await tr.post_command("[[SALT-DEVICE v=1 id=x seq=1 op=observe session=s]]", LaneReaders("lane-1", ids=(DEVICE_ID, OWNER_ID)))
    import json

    body = json.loads(posted[0])
    assert body["chat_id"] == "lane-1" and body["quiet"] is True and set(body) == {"chat_id", "message", "quiet"}
    assert crypto.decrypt(body["message"], keypair_a.private_key, "passphrase-a").startswith("[[SALT-DEVICE")
    assert crypto.decrypt(body["message"], keypair_b.private_key, "passphrase-b").startswith("[[SALT-DEVICE")


async def test_http_post_command_with_armored_keys_does_not_look_anyone_up(keypair_a):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200, json={})

    await http_transport(handler).post_command("hello", LaneReaders("lane-1", public_keys=(keypair_a.public_key,)))
    assert seen == ["POST"]


async def test_http_post_command_refuses_to_send_when_a_reader_has_no_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "must not post"
        return httpx.Response(200, json={"session": {"users": [{"id": DEVICE_ID}]}})

    with pytest.raises(DeviceProtocolError, match="no public key"):
        await http_transport(handler).post_command("hello", LaneReaders("lane-1", ids=(DEVICE_ID,)))


def test_session_meta_round_trips_unknown_fields():
    raw = {"id": "s", "status": "ended", "end_reason": "lost_contact", "capabilities": {"device.task": {"mode": "auto"}}, "intent": None}
    m = DeviceSessionMeta.from_dict(raw)
    assert m.status == "ended" and m.extra["end_reason"] == "lost_contact"
    assert m.to_dict() == {"id": "s", "status": "ended", "end_reason": "lost_contact", "capabilities": {"device.task": {"mode": "auto"}}}


# ------------------------------------------------------------------ feeding the client from an Agent


def make_agent():
    from saltapp.agent import Agent

    client = AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    return Agent(host=HOST, api_key=AGENT_KEY, public_key="pub", private_key="priv", agent_id="ag", client=client)


def test_classify_routes_the_session_deliveries_to_one_type_and_leaves_ended_and_back_alone():
    from saltapp.webhook import classify

    for t in ("device_session_active", "device_session_promoted", "device_session_queue_moved", "device_session_paused", "device_session_resumed"):
        assert classify({"type": t}) == "device_session"
    assert classify({"type": "device_session_ended"}) == "device_session_ended"
    assert classify({"type": "device_back"}) == "device_back"


async def test_an_agent_feeds_deliveries_and_lane_messages_to_a_device_client_end_to_end():
    from saltapp.webhook import Event, classify

    agent = make_agent()
    t = FakeTransport()
    c = DeviceClient(t, DEVICE)
    ended_by_agent: list[str] = []

    @agent.on_device_delivery
    async def deliveries(kind, body):
        c.handle_delivery(kind, body)

    @agent.on_device_session_ended
    async def ended(ctx):
        ended_by_agent.append(ctx.end_reason)

    @agent.on_message
    async def lane(ctx):
        c.handle_lane_message(ctx.text, ctx.sender_id)

    async def feed(body):
        await agent.dispatch(Event(type=classify(body), body=body))

    opened = asyncio.ensure_future(c.open(intent="Check the invoice folder"))
    await tick()
    await feed({**meta(), "type": "device_session_active", "event": "active", "session_id": SESSION})
    assert (await opened).status == "active"

    p = asyncio.ensure_future(c.list_apps())
    await tick()
    answer = result(t.sent(0), "apps", {"apps": [{"id": "a", "name": "Mail", "frontmost": True}]})
    await feed({"message": {"chat_id": "chat-1", "user": {"id": DEVICE, "account_type": "Device"}, "message": answer, "encrypted": False}})
    assert (await p)["apps"][0]["name"] == "Mail"

    await feed({"type": "device_session_ended", "event": "ended", "session_id": SESSION, "id": SESSION, "device_id": DEVICE, "end_reason": "stopped_by_person"})
    assert c.session().status == "ended"
    assert ended_by_agent == ["stopped_by_person"]


async def test_apps_changed_surfaces_through_on_apps_changed():
    c, t = await active_client()
    got = []
    off = c.on_apps_changed(got.append)

    def boom(change):
        raise RuntimeError("boom")

    c.on_apps_changed(boom)
    body = {"at": 9, "changed": ["com.a"], "app": "Safari"}
    assert c.handle_lane_message(unsolicited("apps_changed", 1, body), DEVICE) is True
    assert [g.to_dict() for g in got] == [{"at": 9, "changed": ["com.a"]}]
    assert c.handle_lane_message(unsolicited("apps_changed", 2, {"at": 9, "changed": []}), DEVICE) is True  # consumed, not delivered
    assert c.handle_lane_message(unsolicited("apps_changed", 3, body, re="r"), DEVICE) is False  # a `re` is not unsolicited
    assert c.handle_lane_message(unsolicited("apps_changed", 4, body), "person-1") is False  # not the device
    assert c.handle_lane_message(unsolicited("apps_changed", 5, body, session="other"), DEVICE) is False
    assert len(got) == 1
    off()
    c.handle_lane_message(unsolicited("apps_changed", 6, body), DEVICE)
    assert len(got) == 1
