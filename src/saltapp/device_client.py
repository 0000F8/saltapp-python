"""The AGENT side of device control: ``DeviceClient``.

Python twin of salt-agent-sdk's ``src/device/client.ts`` (``createDeviceClient``) and
``http.ts`` (``httpDeviceAgentTransport``). It opens a control session on a computer the
agent holds a ``device.*`` grant for, sends commands (observe, click, type, ...) and awaits
the computer's results. It never contacts the computer directly and never learns more than
the lane carries: every command is the PLAINTEXT of a lane message that the transport
encrypts for the lane readers and posts, and every result is a lane message the host
application decrypts and feeds back in through ``handle_lane_message``.

Like the TypeScript client this module is transport-agnostic on purpose: the REST calls and
the encrypt-and-post are injected as a ``DeviceClientTransport`` (``HttpDeviceTransport``
wires them to ``AsyncSaltClient``; tests pass a fake). It captures no screen and synthesises
no input: those live on the computer. This side only asks.

Everything is asyncio: ``open()`` and every command are coroutines, and the two feeds
(``handle_delivery``, ``handle_lane_message``) are plain synchronous calls you make from the
event loop when a delivery or a decrypted lane message arrives. One command is in flight at a
time; concurrent callers queue in call order.

::

    transport = HttpDeviceTransport(client, api_key)         # client: AsyncSaltClient
    device = DeviceClient(transport, device_id)
    device.on_state(print)
    await device.open(intent="Download the three invoices")  # waits through queued / requested
    result = await device.task("Open saltapp.ai in Firefox", done_when="the page title says saltapp.ai")

Same codes, constants and timeouts as the TypeScript client (imported from ``saltapp.device``,
never re-invented). Results that come back are untrusted device text: ``task`` allow-lists its
result field by field; ``observe``, ``list_apps``, ``settings`` and ``read_file`` return the
device's JSON body as a plain dict, like the rest of this SDK's REST answers.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, Sequence, Union

from saltapp import crypto
from saltapp.device import (
    CAPABILITY_FOR_OP,
    COMMAND_TTL_MS,
    DEVICE_PROTOCOL_VERSION,
    TASK_CLIENT_GRACE_MS,
    TASK_TIMEOUT_S_DEFAULT,
    DeviceHeader,
    DeviceProtocolError,
    PointedBody,
    Progress,
    StillBody,
    TaskResult,
    decode_device_message,
    device_message_id,
    encode_device_message,
    parse_pointed,
    parse_progress,
    parse_still,
    parse_task_result,
    task_command_body,
)
from saltapp.errors import SaltApiError

#: After the agent aborts a task it asks the device to stop and waits this long (seconds) for the
#: device's own ``stopped`` result (which carries the steps so far) before giving up on it.
TASK_ABORT_GRACE_S = 10.0

#: How long a session may sit in ``requested`` (waiting for the device's local approval) before
#: ``open()`` gives up. The server ends an unanswered request after 2 minutes with
#: ``request_expired``; this is a touch longer so the server's own end reaches us first.
APPROVAL_TIMEOUT_S = 150.0
#: How long ``open()`` waits while the request is ``queued`` behind another agent or undelivered (the
#: computer asleep or offline): salt-api's ``UNDELIVERED_TTL``, 8 hours. The server expires it first.
QUEUED_TIMEOUT_S = 8 * 60 * 60.0

#: Per-command wait for a result, in seconds. On timeout the client sends ``cancel {id}``.
COMMAND_TIMEOUT_S = 30.0

SESSION_STATUSES: tuple[str, ...] = ("queued", "requested", "active", "paused", "ended")


# --- session metadata -----------------------------------------------------------------------


@dataclass(frozen=True)
class LaneReaders:
    """Who a lane message must be encrypted for. The server hands an agent
    ``chat: {id, readers}`` where ``readers`` is ``{"ids": [...], "open": False}`` (account ids,
    the caller excluded): then ``ids`` is set and the transport looks up each reader's key at send
    time. A list of armored public keys (the shape salt-agent-sdk's client reads) fills
    ``public_keys`` directly and the transport encrypts to exactly those."""

    chat_id: str
    public_keys: tuple[str, ...] = ()
    ids: tuple[str, ...] = ()


@dataclass
class DeviceSessionMeta:
    """A device session as the server describes it. ``extra`` keeps every other field the server
    sent (``end_reason``, ``capabilities``, ...) untouched."""

    id: str
    device_id: str = ""
    agent_id: str = ""
    mandate_id: str = ""
    status: str = "requested"
    #: ``queued`` only: 1-based place in the device's queue.
    position: Optional[int] = None
    #: ``paused`` only.
    pause_reason: Optional[str] = None
    #: ``{"id": lane chat id, "readers": {"ids": [...]} or [armored keys]}``.
    chat: Optional[dict[str, Any]] = None
    #: What the agent said it is doing when it opened (its own words, untrusted).
    intent: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    _FIELDS = ("id", "device_id", "agent_id", "mandate_id", "status", "position", "pause_reason", "chat", "intent")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DeviceSessionMeta":
        known = {k: raw[k] for k in cls._FIELDS if k in raw and raw[k] is not None}
        known.setdefault("id", "")
        extra = {k: v for k, v in raw.items() if k not in cls._FIELDS}
        return cls(extra=extra, **known)

    @classmethod
    def coerce(cls, raw: Union["DeviceSessionMeta", Mapping[str, Any]]) -> "DeviceSessionMeta":
        return raw if isinstance(raw, DeviceSessionMeta) else cls.from_dict(raw)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.extra)
        for k in self._FIELDS:
            v = getattr(self, k)
            if v is not None and v != "":  # a field the server never sent stays absent, so a merge never blanks a real one
                out[k] = v
        return out


#: What ``on_state`` emits: ``{"state": "queued", "position": n}``, ``{"state": "requested"}``,
#: ``{"state": "requested", "delivered": False}`` (the computer has not got it yet), ``{"state": "delivered"}``,
#: ``{"state": "active"}``, ``{"state": "paused", "reason": r}``, ``{"state": "resumed"}`` or
#: ``{"state": "ended", "reason": r}``.
DeviceState = dict[str, Any]


class DeviceSessionLiveError(Exception):
    """Raised by a transport's ``open_session`` on ``409 {code: "session_live", session_id}``: this
    agent already has a live session on the device. ``open()`` reattaches."""

    def __init__(self, session_id: str) -> None:
        super().__init__("session_live")
        self.session_id = session_id


class DevicePausedError(DeviceProtocolError):
    """A command was refused (or not sent) because the session is paused: the person is using the
    machine, or a secure field needs them. Wait for the ``resumed`` state (``on_state``) and try
    again. ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__("paused", f"device session paused: {reason}")
        self.reason = reason


class DeviceClientTransport(Protocol):
    """The injected transport. ``HttpDeviceTransport`` is the real one."""

    async def open_session(self, device_id: str, *, intent: Optional[str] = None) -> Union[DeviceSessionMeta, Mapping[str, Any]]:
        """``POST /api/v1/devices/:id/sessions`` -> the created session: status ``requested`` (the
        device must approve locally), ``active``, or ``queued`` (202, another agent controls the
        device). Raises ``DeviceSessionLiveError`` on a 409 ``session_live``."""

    async def stop_session(self, session_id: str) -> None:
        """``POST /api/v1/device_sessions/:sid/stop`` (subtract-only, idempotent)."""

    async def post_command(self, plaintext: str, readers: LaneReaders) -> None:
        """Encrypt ``plaintext`` for the current lane readers and POST it as one lane message. The
        transport owns the crypto and the readers; the client only decides the plaintext."""

    # Optional (looked up with getattr; a transport without them just cannot reattach or fetch):
    #   async def reattach_session(self, session_id) -> {"session": ..., "last_seq": int}
    #   async def fetch_session(self, session_id) -> session


Listener = Callable[..., Any]


@dataclass
class _Pending:
    future: "asyncio.Future[tuple[str, Any]]"
    handle: Any = None
    #: A task only: gets each ``progress`` line, in the order the device sent them.
    on_progress: Optional[Callable[[Progress], Any]] = None
    last_progress_seq: Optional[int] = None


class _CommandTimeout(Exception):
    pass


class _CommandAborted(Exception):
    pass


def _call_later(loop: asyncio.AbstractEventLoop, delay_s: float, fn: Callable[[], None]) -> Any:
    """One seam for every timer, so tests can drive time by hand."""
    return loop.call_later(delay_s, fn)


def _same_id(a: Any, b: Any) -> bool:
    return a is not None and b is not None and str(a).lower() == str(b).lower()


def _safely(fn: Callable[..., Any], *args: Any) -> None:
    """A listener or callback raising never breaks the stream. An async one is scheduled."""
    try:
        out = fn(*args)
        if asyncio.iscoroutine(out):
            task = asyncio.ensure_future(out)
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
    except Exception:  # noqa: BLE001
        pass


def _readers_from(meta: DeviceSessionMeta) -> Optional[LaneReaders]:
    chat = meta.chat
    if not isinstance(chat, Mapping) or not chat.get("id"):
        return None
    raw = chat.get("readers")
    if isinstance(raw, (list, tuple)):
        keys = tuple(k for k in raw if isinstance(k, str) and k)
        return LaneReaders(chat_id=chat["id"], public_keys=keys) if keys else None
    if isinstance(raw, Mapping):
        ids = tuple(str(i) for i in (raw.get("ids") or []))
        return LaneReaders(chat_id=chat["id"], ids=ids) if ids else None
    return None


class DeviceClient:
    """One agent's control of one computer. Construct with a transport and the device's account id;
    ``open()`` it, send commands, ``stop()`` it. See the module docstring."""

    def __init__(
        self,
        transport: DeviceClientTransport,
        device_id: str,
        *,
        approval_timeout_s: float = APPROVAL_TIMEOUT_S,
        queued_timeout_s: float = QUEUED_TIMEOUT_S,
        command_timeout_s: float = COMMAND_TIMEOUT_S,
        command_ttl_ms: int = COMMAND_TTL_MS,
        clock_ms: Optional[Callable[[], int]] = None,
    ) -> None:
        self._transport = transport
        self.device_id = device_id
        self._approval_timeout_s = approval_timeout_s
        self._queued_timeout_s = queued_timeout_s
        self._command_timeout_s = command_timeout_s
        self._command_ttl_ms = command_ttl_ms
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

        self._meta: Optional[DeviceSessionMeta] = None
        self._readers: Optional[LaneReaders] = None
        self._seq = 0
        self._paused_reason: Optional[str] = None
        self._pending: dict[str, _Pending] = {}
        self._listeners: dict[str, list[Listener]] = {"active": [], "ended": [], "ask": []}
        self._state_listeners: list[Callable[[DeviceState], Any]] = []
        self._still_listeners: list[Callable[[StillBody], Any]] = []
        self._pointed_listeners: list[Callable[[PointedBody, str], Any]] = []
        self._open_future: Optional["asyncio.Future[DeviceSessionMeta]"] = None
        self._open_timer: Any = None
        self._lock = asyncio.Lock()  # single flight: commands run one at a time, in call order
        self._early_ended: dict[str, str] = {}
        #: The device's clock minus ours, learned from ``clock_skew`` / ``expired`` replies.
        self._clock_offset_ms = 0
        self._background: set["asyncio.Task[Any]"] = set()

    #: ``op`` -> the ``device.*`` capability it needs, re-exported for callers that gate before sending.
    capability_for_op = CAPABILITY_FOR_OP

    # -- small internals ------------------------------------------------------------------------

    def _emit(self, event: str, *args: Any) -> None:
        for cb in list(self._listeners.get(event, [])):
            _safely(cb, *args)

    def _emit_state(self, state: DeviceState) -> None:
        for cb in list(self._state_listeners):
            _safely(cb, state)

    def _fail_all(self, err: Exception) -> None:
        for p in list(self._pending.values()):
            if p.handle is not None:
                p.handle.cancel()
            if not p.future.done():
                p.future.set_exception(err)
        self._pending.clear()

    def _clear_open_timer(self) -> None:
        if self._open_timer is not None:
            self._open_timer.cancel()
        self._open_timer = None

    def _take_open_future(self) -> Optional["asyncio.Future[DeviceSessionMeta]"]:
        fut = self._open_future
        self._open_future = None
        return fut

    def _start_approval_timer(self) -> None:
        """The approval clock runs only once the device has the request, never while it is
        ``queued`` or undelivered."""
        self._start_open_timer(self._approval_timeout_s, "device did not approve in time", keep_running=False)

    def _start_wait_timer(self, *, keep_running: bool = False) -> None:
        """While queued or undelivered ``open()`` waits on its own, much longer clock."""
        self._start_open_timer(self._queued_timeout_s, "computer did not pick up the request in time", keep_running=keep_running)

    def _start_open_timer(self, seconds: float, message: str, *, keep_running: bool) -> None:
        if self._open_future is None:
            return
        if self._open_timer is not None:
            if not keep_running:
                self._clear_open_timer()
            else:
                return

        def expire() -> None:
            self._open_timer = None
            fut = self._take_open_future()
            if fut is not None and not fut.done():
                fut.set_exception(DeviceProtocolError("failed", message))

        self._open_timer = _call_later(asyncio.get_running_loop(), seconds, expire)

    def _enter_requested(self) -> None:
        """A request the device holds: the approval clock if it is delivered (or an older server never
        says), otherwise the long wait. ``delivered_at: null`` is the server's word for "not yet"."""
        extra = self._meta.extra if self._meta is not None else {}
        if "delivered_at" in extra and extra["delivered_at"] is None:
            self._emit_state({"state": "requested", "delivered": False})
            self._start_wait_timer(keep_running=False)
            return
        self._emit_state({"state": "requested"})
        self._start_approval_timer()

    async def _wait_open(self, waiter: "asyncio.Future[DeviceSessionMeta]") -> DeviceSessionMeta:
        """Await the open future. Cancelling the caller stops the wait and withdraws a request that is
        still waiting (best effort), so it does not hold the computer for hours."""
        try:
            return await waiter
        except asyncio.CancelledError:
            sid = self._meta.id if self._meta is not None else None
            waiting = self._meta is not None and self._meta.status not in ("active", "paused")
            self._clear_open_timer()
            self._open_future = None
            if sid is not None and waiting:
                try:
                    await asyncio.shield(self._transport.stop_session(sid))
                except Exception:  # noqa: BLE001
                    pass
            raise

    def _settle_open(self, meta: DeviceSessionMeta) -> None:
        self._clear_open_timer()
        fut = self._take_open_future()
        if fut is not None and not fut.done():
            fut.set_result(meta)

    def _become_active(self, m: DeviceSessionMeta, announce: bool) -> None:
        """The session just became (or was found) active. Readers come from the payload; if absent
        they are fetched once."""
        meta = replace(m, status="active")
        self._meta = meta
        self._paused_reason = None
        self._readers = _readers_from(meta)

        def finish() -> None:
            self._settle_open(meta if self._meta is None else self._meta)
            if announce:
                self._emit("active", self._meta)
                self._emit_state({"state": "active"})

        fetch = getattr(self._transport, "fetch_session", None)
        if self._readers is not None or fetch is None:
            return finish()
        sid = meta.id

        async def fetch_then_finish() -> None:
            try:
                fetched = await fetch(sid)
                if self._meta is not None and _same_id(self._meta.id, sid) and fetched:
                    merged = {**self._meta.to_dict(), **DeviceSessionMeta.coerce(fetched).to_dict(), "status": self._meta.status}
                    self._meta = DeviceSessionMeta.from_dict(merged)
                    self._readers = _readers_from(self._meta)
            except Exception:  # noqa: BLE001
                pass
            finish()

        self._spawn(fetch_then_finish())

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _reset_session(self) -> None:
        self._meta = None
        self._readers = None
        self._paused_reason = None
        self._seq = 0
        self._clock_offset_ms = 0

    # -- open / reattach ------------------------------------------------------------------------

    async def open(self, *, wait: bool = True, intent: Optional[str] = None) -> DeviceSessionMeta:
        """Open a session. By default resolves when it is ``active``, waiting through ``queued``
        (another agent controls the device; a sleeping computer is queued too) and ``requested``
        (the device's local approval) on ``device_session`` deliveries, never polling. Raises if the
        device declines, the request expires, or the grant is gone. With ``wait=False`` it returns
        at once with the state the server answered (e.g. ``queued`` with ``position``); watch
        ``on_state`` for the rest. If this agent already has a live session on the device (409
        ``session_live``) it reattaches and continues at ``last_seq + 1``. ``intent`` is one line,
        at most 140 characters, in your own words: the person reads it in the Salt Device panel
        (never checked, never widens what you may do); longer is a 422 whose sentence is the error."""
        if self._meta is not None and self._meta.status in ("active", "paused"):
            return self._meta
        # A previous session's leftovers must never leak into this one.
        self._reset_session()
        # Register the waiter SYNCHRONOUSLY, before the POST yields: the device's
        # `device_session_active` delivery can race ahead of the POST response, and it must find a
        # resolver already in place.
        waiter: Optional["asyncio.Future[DeviceSessionMeta]"] = None
        if wait:
            waiter = asyncio.get_running_loop().create_future()
            waiter.add_done_callback(lambda f: f.cancelled() or f.exception())  # a rejection reaches the caller below
            self._open_future = waiter
        try:
            created = DeviceSessionMeta.coerce(await self._transport.open_session(self.device_id, intent=intent))
        except DeviceSessionLiveError as e:
            return await self._reattach(e.session_id, waiter)
        except BaseException:
            self._clear_open_timer()
            self._open_future = None
            raise
        self._seq = 0
        early = self._early_ended.get(str(created.id))
        self._early_ended.clear()
        if early is not None:
            self._meta = created
            self.handle_delivery("device_session_ended", {"session_id": created.id, "end_reason": early})
            raise DeviceProtocolError("failed", f"session ended: {early}")
        # If a delivery already moved us on while the POST was in flight, its fields (status, chat)
        # win; otherwise take the POST's view.
        self._meta = DeviceSessionMeta.from_dict({**created.to_dict(), **self._meta.to_dict()}) if self._meta else created
        status = self._meta.status
        if status == "active":
            self._become_active(self._meta, True)
            return await self._wait_open(waiter) if waiter else self._meta
        if status == "queued":
            self._emit_state({"state": "queued", "position": self._meta.position or 1})
            self._start_wait_timer()
        elif status == "requested":
            self._enter_requested()
        if waiter is None:
            return self._meta
        return await self._wait_open(waiter)

    async def _reattach(self, session_id: str, waiter: Optional["asyncio.Future[DeviceSessionMeta]"]) -> DeviceSessionMeta:
        try:
            reattach = getattr(self._transport, "reattach_session", None)
            if reattach is None:
                raise DeviceProtocolError("failed", "transport cannot reattach")
            r = await reattach(session_id)
            self._seq = int(r["last_seq"])  # continue at last_seq + 1
            session = DeviceSessionMeta.coerce(r["session"])
            if session.status == "paused":
                self._meta = replace(session)
                self._readers = _readers_from(self._meta)
                self._paused_reason = self._meta.pause_reason or "paused"
                self._settle_open(self._meta)
                self._emit_state({"state": "paused", "reason": self._paused_reason})
            else:
                self._become_active(session, True)
            return await self._wait_open(waiter) if waiter else self._meta  # type: ignore[return-value]
        except BaseException:
            self._clear_open_timer()
            self._open_future = None
            raise

    def _require_active(self) -> tuple[DeviceSessionMeta, LaneReaders]:
        if self._paused_reason is not None:
            raise DevicePausedError(self._paused_reason)
        if self._meta is None or self._meta.status != "active":
            raise DeviceProtocolError("failed", "no active device session")
        if self._readers is None:
            raise DeviceProtocolError("failed", "session active but lane readers unknown")
        return self._meta, self._readers

    # -- sending ---------------------------------------------------------------------------------

    async def _send_and_wait(
        self,
        cmd_id: str,
        plaintext: str,
        readers: LaneReaders,
        meta: DeviceSessionMeta,
        op: str,
        *,
        timeout_s: Optional[float] = None,
        on_progress: Optional[Callable[[Progress], Any]] = None,
        abort: Optional[asyncio.Event] = None,
    ) -> tuple[str, Any]:
        """Post one plaintext and wait for the result keyed to ``cmd_id``. On the client timeout,
        tell the device to drop it (``cancel {id}``) and fail. A task's cancel also names the task
        in ``re``, and an aborted task gets a short grace to report."""
        loop = asyncio.get_running_loop()
        is_task = op == "task"
        aborted = False
        future: "asyncio.Future[tuple[str, Any]]" = loop.create_future()
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        pending = _Pending(future=future, on_progress=on_progress)

        def time_out() -> None:
            self._pending.pop(cmd_id, None)
            if not future.done():
                future.set_exception(_CommandTimeout(f"{op} timed out"))

        pending.handle = _call_later(loop, timeout_s if timeout_s is not None else self._command_timeout_s, time_out)
        self._pending[cmd_id] = pending

        def cancel_text() -> str:
            return encode_device_message(
                DeviceHeader(
                    v=DEVICE_PROTOCOL_VERSION,
                    id=device_message_id(),
                    seq=max(self._seq, 1),
                    op="cancel",
                    session=meta.id,
                    re=cmd_id if is_task else None,
                ),
                {"id": cmd_id},
            )

        def give_up_aborted() -> None:
            self._pending.pop(cmd_id, None)
            if not future.done():
                future.set_exception(_CommandAborted("task aborted"))

        async def post_cancel() -> None:
            try:
                await self._transport.post_command(cancel_text(), readers)
            except Exception:  # noqa: BLE001
                pass

        def on_abort() -> None:
            nonlocal aborted
            p = self._pending.get(cmd_id)
            if p is None or aborted:
                return
            aborted = True
            # Ask the device to stop, then give it a moment to say how far it got.
            self._spawn(post_cancel())
            if p.handle is not None:
                p.handle.cancel()
            p.handle = _call_later(loop, TASK_ABORT_GRACE_S, give_up_aborted)

        watcher: Optional["asyncio.Task[None]"] = None
        if abort is not None:

            async def watch() -> None:
                await abort.wait()
                on_abort()

            watcher = asyncio.ensure_future(watch())
        try:
            try:
                await self._transport.post_command(plaintext, readers)
            except BaseException:
                p = self._pending.pop(cmd_id, None)
                if p is not None and p.handle is not None:
                    p.handle.cancel()
                raise
            try:
                return await future
            except _CommandTimeout as e:
                # Out-of-band and best effort: consumes no seq. A command that already started
                # keeps its stored result on the device for a retry by id.
                await post_cancel()
                raise DeviceProtocolError("failed", str(e)) from None
            except _CommandAborted as e:
                raise DeviceProtocolError("failed", str(e)) from None
        finally:
            if watcher is not None:
                watcher.cancel()

    async def _execute(
        self,
        op: str,
        args: Any = None,
        *,
        timeout_s: Optional[float] = None,
        on_progress: Optional[Callable[[Progress], Any]] = None,
        abort: Optional[asyncio.Event] = None,
    ) -> tuple[str, Any]:
        if abort is not None and abort.is_set():
            raise DeviceProtocolError("failed", "task aborted")
        meta, readers = self._require_active()
        cmd_id = device_message_id()  # reused by every resend of this command
        self._seq += 1
        resynced = refreshed = skewed = False
        while True:
            plaintext = encode_device_message(
                DeviceHeader(
                    v=DEVICE_PROTOCOL_VERSION,
                    id=cmd_id,
                    seq=self._seq,
                    op=op,
                    session=meta.id,
                    exp=self._clock_ms() + self._clock_offset_ms + self._command_ttl_ms,
                ),
                args,
            )
            res_op, body = await self._send_and_wait(
                cmd_id, plaintext, readers, meta, op, timeout_s=timeout_s, on_progress=on_progress, abort=abort
            )
            if res_op != "error":
                return res_op, body
            b: Mapping[str, Any] = body if isinstance(body, Mapping) else {}
            expected = b.get("expected_seq")
            expected = expected if isinstance(expected, int) and not isinstance(expected, bool) else None
            now = b.get("now")
            if isinstance(now, (int, float)) and not isinstance(now, bool) and now == now and abs(now) != float("inf"):
                self._clock_offset_ms = int(now) - self._clock_ms()
            code = b.get("code")
            if code == "clock_skew" and not skewed:
                skewed = True
                if expected is not None:
                    self._seq = expected
                continue  # same id and seq, exp corrected to the device's clock
            if code == "out_of_order" and expected is not None and not resynced:
                resynced = True
                self._seq = expected  # resend with the SAME id so it cannot run twice
                continue
            if code == "expired" and not refreshed:
                refreshed = True
                if expected is not None:
                    self._seq = expected
                continue  # same id and seq, fresh exp
            if code == "paused":
                if expected is not None:
                    self._seq = expected - 1
                # Not a state change (deliveries own that): just fail this call.
                raise DevicePausedError(b.get("reason") or "paused")
            if expected is not None:
                self._seq = expected - 1
            return res_op, body

    async def _request(self, op: str, args: Any = None, **kw: Any) -> tuple[str, Any]:
        if self._paused_reason is not None:
            raise DevicePausedError(self._paused_reason)
        async with self._lock:  # FIFO: commands run one at a time, in call order
            return await self._execute(op, args, **kw)

    async def _expect(self, op: str, want_op: str, args: Any = None, **kw: Any) -> Any:
        """Run one command and return the body of the wanted result. Raises ``DeviceProtocolError``
        on an ``error`` result (with the device's code) and on an ``ask`` result (``needs_approval``:
        the person has to act at the machine first)."""
        res_op, body = await self._request(op, args, **kw)
        if res_op == "error":
            b = body if isinstance(body, Mapping) else {}
            raise DeviceProtocolError(b.get("code") or "failed", b.get("message"))
        if res_op == "ask":
            b = body if isinstance(body, Mapping) else {}
            reason = b.get("reason") or "the device is waiting for the person"
            self._emit("ask", reason)
            raise DeviceProtocolError("needs_approval", reason)
        if res_op != want_op:
            raise DeviceProtocolError("failed", f"expected {want_op}, got {res_op}")
        return body

    # -- the commands ----------------------------------------------------------------------------

    async def observe(
        self,
        target: str = "screen",
        *,
        display: Optional[int] = None,
        window: Optional[str] = None,
        max_width: Optional[int] = None,
        format: Optional[str] = None,
        want: Optional[str] = None,
    ) -> dict[str, Any]:
        """Look at the screen or one window (op ``observe``, grant ``device.observe``). The computer
        prefers a structured ``ui`` snapshot (``want="ui"``, the default); ``want="pixels"`` or
        ``"both"`` asks for an image too. Returns the observation body: ``obs`` (the id a later
        click or type names), ``ui`` and/or ``image_b64`` with ``width``, ``height``, ``scale``,
        ``format``."""
        args: dict[str, Any] = {"target": target}
        for k, v in (("display", display), ("window", window), ("max_width", max_width), ("format", format), ("want", want)):
            if v is not None:
                args[k] = v
        return await self._expect("observe", "observation", args)

    async def click(
        self,
        x: float,
        y: float,
        *,
        button: Optional[str] = None,
        count: Optional[int] = None,
        obs: Optional[str] = None,
        element: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Click (op ``click``, grant ``device.act``). ``element={"obs": ..., "node": ...}`` names a
        node of an earlier observation: the computer then performs the semantic press (preferred;
        ``x``/``y`` stay the fallback for what the tree cannot name)."""
        args: dict[str, Any] = {"x": x, "y": y}
        for k, v in (("button", button), ("count", count), ("obs", obs), ("element", dict(element) if element else None)):
            if v is not None:
                args[k] = v
        await self._expect("click", "ack", args)

    async def type(self, text: str) -> None:
        """Type text at whatever has focus (op ``type``, grant ``device.act``)."""
        await self._expect("type", "ack", {"text": text})

    async def key(self, keys: Sequence[str]) -> None:
        """Press a key chord, e.g. ``["cmd", "s"]`` (op ``key``, grant ``device.act``)."""
        await self._expect("key", "ack", {"keys": list(keys)})

    async def scroll(self, x: float, y: float, dx: float, dy: float) -> None:
        """Scroll at a point (op ``scroll``, grant ``device.act``)."""
        await self._expect("scroll", "ack", {"x": x, "y": y, "dx": dx, "dy": dy})

    async def submit(self, element: Mapping[str, str]) -> None:
        """Confirm a named field or form through the accessibility layer, never a keystroke (op
        ``submit``, grant ``device.act``). ``element={"obs": ..., "node": ...}``."""
        await self._expect("submit", "ack", {"element": dict(element)})

    async def focus_app(self, app: str) -> None:
        """Bring an app to the front by name or bundle id (op ``focus_app``, grant ``device.apps``)."""
        await self._expect("focus_app", "ack", {"app": app})

    async def list_apps(self) -> dict[str, Any]:
        """The running apps (op ``list_apps``, grant ``device.apps``): ``{"apps": [{"id", "name",
        "frontmost"}]}``."""
        return await self._expect("list_apps", "apps")

    async def settings(self) -> dict[str, Any]:
        """Read the computer's settings and power/display state (op ``settings``). Needs a live
        session only: no grant, no ask, works with the screen off and with commands off. Returns
        ``{"settings", "state", "changed", "refused"}``."""
        return await self._expect("settings", "settings", {})

    async def set_settings(self, set: Mapping[str, Any]) -> dict[str, Any]:
        """Change a setting (op ``settings`` with ``{"set": ...}``). Any setting the device has can
        be set; the result's ``changed`` lists what applied and ``refused`` maps each key that did
        not to ``unknown_setting``, ``bad_value`` or ``declined`` (the person said no, or the ask
        timed out). When the grant does not act without asking, this raises the person's ask and
        resolves after the answer."""
        return await self._expect("settings", "settings", {"set": dict(set)})

    async def read_file(self, path: str) -> dict[str, Any]:
        """Read a file (op ``read_file``, grant ``device.files.read``): ``{"path", "size", "content_b64"}``."""
        return await self._expect("read_file", "file", {"path": path})

    async def write_file(self, path: str, content_b64: str) -> None:
        """Write a file (op ``write_file``, grant ``device.files.write``)."""
        await self._expect("write_file", "ack", {"path": path, "content_b64": content_b64})

    async def task(
        self,
        goal: str,
        *,
        done_when: Optional[str] = None,
        max_steps: Optional[int] = None,
        timeout_s: Optional[int] = None,
        on_progress: Optional[Callable[[Progress], Any]] = None,
        abort: Optional[asyncio.Event] = None,
    ) -> TaskResult:
        """Hand the computer ONE whole task (op ``task``, grant ``device.task``) and wait for its
        checked result. ``goal`` is what the person asked for, in plain words (1..2000 characters);
        ``done_when`` says how the device should check it worked (<= 500). The device does the work
        itself: command output and screenshots never come back, only a ``TaskResult`` of words and
        numbers. Raises ``DeviceProtocolError`` ``bad_args`` before sending when the arguments are
        out of range, ``unsupported`` when the computer cannot run tasks, ``out_of_scope`` without
        the grant or when the budget is spent. ``on_progress(Progress)`` gets each non-final line in
        order (at most one every 2 s, newest wins); a callback that raises is ignored. Waits
        ``timeout_s + 30`` s (default 180 + 30); on that timeout, or when ``abort`` (an
        ``asyncio.Event``) is set, it sends ``cancel`` with the task's id as ``re``. After an abort
        it waits up to 10 s for the device's ``stopped`` result and returns it, else raises."""
        # Refuses here what the device would refuse, before anything is sent.
        args = task_command_body(goal, done_when, max_steps, timeout_s)
        wait_s = args.get("timeout_s", TASK_TIMEOUT_S_DEFAULT) + TASK_CLIENT_GRACE_MS / 1000
        body = await self._expect("task", "task_result", args, timeout_s=wait_s, on_progress=on_progress, abort=abort)
        return parse_task_result(body)

    async def stop(self) -> None:
        """End the session (subtract-only). Safe to call more than once."""
        if self._meta is None or not self._meta.id:
            return
        try:
            await self._transport.stop_session(self._meta.id)
        finally:
            if self._meta is not None:
                self._meta.status = "ended"
            self._readers = None
            self._fail_all(DeviceProtocolError("failed", "session stopped"))

    # -- state -----------------------------------------------------------------------------------

    def session(self) -> Optional[DeviceSessionMeta]:
        """The live session metadata, or ``None`` before ``open()``. After the session ended it is
        still returned, with ``status == "ended"``."""
        return self._meta

    def paused_reason(self) -> Optional[str]:
        """The pause reason while paused, else ``None``."""
        return self._paused_reason

    # -- the two feeds ---------------------------------------------------------------------------

    def handle_delivery(self, kind: str, body: Any) -> None:
        """Feed a ``device_session`` socket delivery in (promoted / queue_moved / paused / resumed /
        ended, and the legacy active / ended types). Call it from the event loop when the
        delivery arrives. A delivery about another computer or another session is ignored."""
        if not isinstance(kind, str) or not kind.startswith("device_session"):
            return
        b: Mapping[str, Any] = body if isinstance(body, Mapping) else {}
        typ = b["type"] if isinstance(b.get("type"), str) else kind
        # New deliveries carry `event`; the legacy ones only a `device_session_<x>` type.
        if isinstance(b.get("event"), str):
            ev = b["event"]
        else:
            ev = typ[len("device_session"):].lstrip("_") if typ.startswith("device_session") else typ
        # The API's delivery IS the session JSON plus type/event/session_id; older shapes nest it under `session`.
        flat = isinstance(b.get("id"), str) and isinstance(b.get("status"), str)
        sess: Optional[dict[str, Any]] = None
        if isinstance(b.get("session"), Mapping):
            sess = dict(b["session"])
        elif flat:
            sess = {k: v for k, v in b.items() if k not in ("type", "event", "session_id")}
        sid = (sess or {}).get("id") or b.get("session_id")
        # A delivery about another computer is not ours (an agent can hold sessions on several).
        dev = (sess or {}).get("device_id") or b.get("device_id")
        if dev is not None and not _same_id(dev, self.device_id):
            return
        if self._meta is not None and sid is not None and not _same_id(sid, self._meta.id):
            return
        status = b.get("status") or (sess or {}).get("status")

        if ev in ("active", "promoted"):
            if self._meta is None and sess is None:
                return
            merged_raw = {**(self._meta.to_dict() if self._meta else {}), **(sess or {})}
            if sid is not None:
                merged_raw["id"] = sid
            merged = DeviceSessionMeta.from_dict(merged_raw)
            if ev == "active" or status == "active":
                self._become_active(merged, True)
            else:
                self._meta = replace(merged, status="requested")
                self._enter_requested()
        elif ev == "received":
            # The device has the request: the approval clock starts now, the long wait ends.
            if self._meta is None or self._meta.status in ("active", "paused"):
                return
            raw = {**self._meta.to_dict(), **(sess or {}), "status": "requested"}
            raw["delivered_at"] = b.get("delivered_at") or datetime.now(timezone.utc).isoformat()
            self._meta = DeviceSessionMeta.from_dict(raw)
            self._emit_state({"state": "delivered"})
            self._start_approval_timer()
        elif ev == "queue_moved":
            if self._meta is None:
                return
            try:
                position = int(b.get("position"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return
            self._meta = replace(self._meta, status="queued", position=position)
            self._emit_state({"state": "queued", "position": position})
            self._start_wait_timer(keep_running=True)
        elif ev == "paused":
            if self._meta is None:
                return
            reason = b.get("pause_reason") or b.get("reason") or "paused"
            self._paused_reason = reason
            self._meta = replace(self._meta, status="paused", pause_reason=reason)
            self._emit_state({"state": "paused", "reason": reason})
        elif ev == "resumed":
            if self._meta is None:
                return
            self._paused_reason = None
            self._meta = replace(self._meta, status="active", pause_reason=None)
            self._emit_state({"state": "resumed"})
        elif ev == "ended":
            # Before the session is known, an `ended` may be about an OLDER session of this agent:
            # hold it, apply it only if it turns out to be ours.
            if self._meta is None:
                if sid is not None:
                    self._early_ended[str(sid)] = b.get("end_reason") or "ended"
                return
            reason = b.get("end_reason") or "ended"
            self._meta.status = "ended"
            self._paused_reason = None
            self._readers = None
            self._clear_open_timer()
            err = DeviceProtocolError("failed", f"session ended: {reason}")
            fut = self._take_open_future()
            if fut is not None and not fut.done():
                fut.set_exception(err)
            self._fail_all(err)
            self._emit("ended", reason)
            self._emit_state({"state": "ended", "reason": reason})

    def handle_lane_message(self, plaintext: str, sender_id: Optional[str]) -> bool:
        """Feed a decrypted lane message in together with WHO sent it (the sender's account id).
        Only the device's own account counts: a person, another agent or anyone else who can post
        in the lane is ignored, so no one else can forge a result, a still or a ``pointed``. Returns
        True if it was a device result this client consumed."""
        if sender_id is None or not _same_id(sender_id, self.device_id):
            return False
        try:
            decoded = decode_device_message(plaintext)
        except DeviceProtocolError:
            return False  # a malformed device line from the device: not ours to answer
        if decoded is None:
            return False
        header, body = decoded.header, decoded.body
        if self._meta is None or not _same_id(header.session, self._meta.id):
            return False
        # Show Me: the two device-to-agent ops that are not answers to a command.
        if header.op == "still" and header.re is None:
            still = parse_still(body)
            if still is not None:
                for cb in list(self._still_listeners):
                    _safely(cb, still)
            return True
        if header.op == "pointed" and header.re is not None:
            pointed = parse_pointed(body)
            if pointed is not None:
                for cb in list(self._pointed_listeners):
                    _safely(cb, pointed, header.re)
            return True
        if header.re is None:
            return False  # a result always answers a command id
        p = self._pending.get(header.re)
        if p is None:
            return False
        if header.op == "progress":
            # Non-final: the command stays pending. Delivered in the order the device sent them; a
            # line that arrives after a newer one is dropped, and only the allow-listed fields get through.
            line = parse_progress(body if isinstance(body, Mapping) else None)
            if p.on_progress is not None and line is not None and (p.last_progress_seq is None or header.seq > p.last_progress_seq):
                p.last_progress_seq = header.seq
                _safely(p.on_progress, line)
            return True
        if p.handle is not None:
            p.handle.cancel()
        self._pending.pop(header.re, None)
        if not p.future.done():
            p.future.set_result((header.op, body))
        return True

    # -- listeners -------------------------------------------------------------------------------

    def on(self, event: str, cb: Listener) -> None:
        """``"active"`` (session metadata), ``"ended"`` (the end reason) or ``"ask"`` (the reason the
        person has to act at the machine). A callback that raises is ignored."""
        self._listeners.setdefault(event, []).append(cb)

    def on_still(self, cb: Callable[[StillBody], Any]) -> Callable[[], None]:
        """Show Me: a still the computer posted into the lane while a help request is open (op
        ``still``, unsolicited). The body is allow-listed (``parse_still``); a malformed one is
        dropped. Returns an unsubscribe."""
        self._still_listeners.append(cb)
        return lambda: self._still_listeners.remove(cb) if cb in self._still_listeners else None

    def on_pointed(self, cb: Callable[[PointedBody, str], Any]) -> Callable[[], None]:
        """Show Me: the computer's answer to a person's point (op ``pointed``). The second argument
        is the SALT-POINT id it answers (the header's ``re``). Returns an unsubscribe."""
        self._pointed_listeners.append(cb)
        return lambda: self._pointed_listeners.remove(cb) if cb in self._pointed_listeners else None

    def on_state(self, cb: Callable[[DeviceState], Any]) -> Callable[[], None]:
        """Session state changes: queued(position) / requested / active / paused(reason) / resumed /
        ended(reason), as dicts (see ``DeviceState``). Returns an unsubscribe."""
        self._state_listeners.append(cb)
        return lambda: self._state_listeners.remove(cb) if cb in self._state_listeners else None


# --- the real transport -----------------------------------------------------------------------


class HttpDeviceTransport:
    """``DeviceClientTransport`` wired to a real ``AsyncSaltClient`` (the Python twin of
    ``httpDeviceAgentTransport``). ``api_key`` is the ACTING AGENT's key.

    Routes: ``POST /api/v1/devices/:id/sessions`` (open; 409 ``session_live`` -> reattach),
    ``POST /api/v1/device_sessions/:sid/reattach``, ``GET /api/v1/device_sessions/:sid``,
    ``POST /api/v1/device_sessions/:sid/stop``, and ``POST /api/v1/messages`` with
    ``{chat_id, message: <ciphertext>, quiet: true}`` for each command. A refusal raises the
    client's ``SaltApiError`` (its message carries the server's own sentence, e.g. 422
    ``intent_too_long``).

    Encryption: a command is encrypted for every reader of the lane. When the session names its
    readers by account id (what salt-api sends), their keys are read fresh from the lane chat's
    member list at send time, never cached, and a reader with no key stops the send; when it carries
    armored keys, exactly those are used."""

    def __init__(self, client: Any, api_key: str) -> None:
        self._client = client
        self._api_key = api_key

    async def open_session(self, device_id: str, *, intent: Optional[str] = None) -> DeviceSessionMeta:
        try:
            data = await self._client._request(
                "POST", f"/api/v1/devices/{device_id}/sessions", self._api_key, {"intent": intent} if intent is not None else None
            )
        except SaltApiError as e:
            # 409 {code: "session_live", session_id}: this agent already controls the device.
            if e.status == 409 and e.code == "session_live" and isinstance(e.body, dict) and e.body.get("session_id") is not None:
                raise DeviceSessionLiveError(e.body["session_id"]) from None
            raise
        data = data if isinstance(data, Mapping) else {}
        # 201 -> the session; 202 -> {session: {status: "queued", position}}.
        raw = data.get("session") if isinstance(data.get("session"), Mapping) else data
        return DeviceSessionMeta.from_dict(raw)

    async def reattach_session(self, session_id: str) -> dict[str, Any]:
        try:
            data = await self._client._request("POST", f"/api/v1/device_sessions/{session_id}/reattach", self._api_key)
        except SaltApiError as e:
            if e.status == 409:
                body = e.body if isinstance(e.body, dict) else {}
                raise DeviceProtocolError("failed", f"session ended: {body.get('end_reason') or body.get('code') or 'ended'}") from None
            raise
        return {"session": DeviceSessionMeta.from_dict(data["session"]), "last_seq": data["last_seq"]}

    async def fetch_session(self, session_id: str) -> DeviceSessionMeta:
        data = await self._client._request("GET", f"/api/v1/device_sessions/{session_id}", self._api_key)
        data = data if isinstance(data, Mapping) else {}
        return DeviceSessionMeta.from_dict(data.get("session") if isinstance(data.get("session"), Mapping) else data)

    async def stop_session(self, session_id: str) -> None:
        await self._client._request("POST", f"/api/v1/device_sessions/{session_id}/stop", self._api_key)

    async def _keys_for(self, readers: LaneReaders) -> list[str]:
        if readers.public_keys:
            return list(readers.public_keys)
        members = await self._client.get_chat_members(self._api_key, readers.chat_id)
        by_id = {str(m.get("id", "")).lower(): m for m in members}
        keys: list[str] = []
        for rid in readers.ids:
            member = by_id.get(str(rid).lower())
            key = member.get("public_key") if member else None
            if not key:
                raise DeviceProtocolError("failed", f"lane reader {rid} has no public key; not sending")
            keys.append(key)
        return keys

    async def post_command(self, plaintext: str, readers: LaneReaders) -> None:
        ciphertext = crypto.encrypt_for(plaintext, await self._keys_for(readers))
        await self._client.post_message(self._api_key, readers.chat_id, ciphertext, quiet=True)
