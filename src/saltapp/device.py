"""The ``[[SALT-DEVICE ...]]`` lane grammar, and the whole-task op (``device.task``).

A device command or result is the PLAINTEXT of an ordinary end-to-end encrypted
lane message. Salt relays the ciphertext and never parses it. This module only
turns a command into a line of text and back; it does no I/O, no crypto and no
transport. It mirrors salt-agent-sdk's ``src/device/protocol.ts`` byte for byte
(the grammar is owned by salt-api's ``docs/DEVICE_PROTOCOL.md`` and mirrored
here, never the other way round); ``tests/fixtures/device_envelope_vectors.json``
is committed identically in both repos and both test suites hold themselves to it.

THIS IS NOT A DEVICE CLIENT. A full Python device client (open a session, queue,
reattach, single-flight, send/await over the lane) is a stated gap: the TypeScript
SDK has ``createDeviceClient`` / ``createDeviceHost``, and @salt keeps its own
port in salt-personal-agent's ``device.py``. What a Python host needs to talk to a
computer, build a ``task`` command and read ``progress`` / ``task_result`` back, is
here; encrypt it for the lane readers with ``saltapp.crypto.encrypt_for``, post it
with ``client.post_message`` and feed what comes back through ``decode_device_message``.

Hand a whole task to a computer::

    text = task_command("Open saltapp.ai in Firefox", done_when="the page title says saltapp.ai",
                        session=session_id, seq=next_seq)
    # ... encrypt_for(readers, text), post_message(...) ...
    msg = decode_device_message(plaintext_from_lane)
    if msg and msg.header.op == "progress":    p = parse_progress(msg.body)
    if msg and msg.header.op == "task_result": r = parse_task_result(msg.body)

Everything that comes back is untrusted device text: ``parse_task_result``
allow-lists it field by field and applies the same caps the device applies, so
neither end trusts the other's limits. Caps count characters as people do
(Unicode code points).
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Union

from saltapp.errors import SaltAppError

#: Grammar version. A message with any other ``v`` is answered ``error {code: "unsupported"}``.
DEVICE_PROTOCOL_VERSION = 1

#: A single lane message carries at most this much PLAINTEXT.
MAX_DEVICE_PLAINTEXT_BYTES = 4 * 1024 * 1024

#: Per-session rate ceilings the device enforces (over either it answers ``busy``).
MAX_COMMANDS_PER_SECOND = 5
MAX_OBSERVE_PER_SECOND = 1

#: A command is valid this long after the client built it: ``exp = now + COMMAND_TTL_MS``
#: (epoch MILLISECONDS). The host refuses a command whose ``exp`` has passed with ``expired``.
COMMAND_TTL_MS = 30_000

#: The host remembers this many results per session by command ``id``.
RESULT_CACHE_SIZE = 64

#: Ops that change what is on the machine's screen/input state. The host serialises these
#: device-wide behind one mutex; a task holds it for its whole run.
MUTATING_OPS: tuple[str, ...] = ("click", "type", "key", "scroll", "submit", "focus_app", "task")

#: Commands an agent sends to a device.
AGENT_OPS: tuple[str, ...] = (
    "observe", "click", "type", "key", "scroll", "submit", "focus_app", "list_apps",
    "read_file", "write_file", "task", "stop", "cancel", "shell",
)

#: Results a device sends back. ``progress`` is NON-final (same ``re`` as its task);
#: ``task_result`` is the final answer to a ``task``.
DEVICE_RESULT_OPS: tuple[str, ...] = (
    "ack", "observation", "apps", "file", "ask", "error", "ended", "progress", "task_result",
)

DEVICE_ERROR_CODES: tuple[str, ...] = (
    "forbidden", "out_of_scope", "needs_approval", "unsupported", "out_of_order", "bad_args",
    "too_large", "busy", "expired", "clock_skew", "paused", "failed",
)

#: The ``device.*`` mandate capability each agent op requires (``None``: none needed).
CAPABILITY_FOR_OP: dict[str, Optional[str]] = {
    "observe": "device.observe",
    "click": "device.act",
    "type": "device.act",
    "key": "device.act",
    "scroll": "device.act",
    "submit": "device.act",
    "focus_app": "device.apps",
    "list_apps": "device.apps",
    "read_file": "device.files.read",
    "write_file": "device.files.write",
    "task": "device.task",
    "stop": None,
    "cancel": None,
    "shell": "device.shell",  # reserved: the server refuses it and a host answers `forbidden`
}

#: The per-class counts a device reports to the server (never content).
DEVICE_COUNT_CLASSES: tuple[str, ...] = (
    "observe", "click", "type", "key", "scroll", "file_read", "file_write", "app_focus",
    "task", "command", "look", "error",
)

# --- task limits (mirrored from protocol.ts) ---------------------------------

TASK_GOAL_MAX = 2000
TASK_DONE_WHEN_MAX = 500
TASK_MAX_STEPS_MIN = 1
TASK_MAX_STEPS_MAX = 60
TASK_MAX_STEPS_DEFAULT = 30
TASK_TIMEOUT_S_MIN = 10
TASK_TIMEOUT_S_MAX = 600
TASK_TIMEOUT_S_DEFAULT = 180
PROGRESS_LINE_MAX = 140
TASK_SUMMARY_MAX = 500
TASK_STEP_LINE_MAX = 140
TASK_RESULT_STEPS_MAX = 60
TASK_CHECKED_WHAT_MAX = 200
TASK_ID_MAX = 64
#: The device sends ``progress`` at most this often.
PROGRESS_MIN_INTERVAL_MS = 2_000
#: A task's command deadline on the device is ``timeout_s`` + this, not COMMAND_TTL_MS.
TASK_DEVICE_GRACE_MS = 15_000
#: The agent waits ``timeout_s`` + this for the result before it sends ``cancel``.
TASK_CLIENT_GRACE_MS = 30_000

TASK_STATUSES: tuple[str, ...] = ("done", "failed", "stopped", "denied", "timeout", "paused_out")
TASK_STEP_KINDS: tuple[str, ...] = ("command", "look", "observe", "act", "apps", "ask", "check")


class DeviceProtocolError(SaltAppError):
    """A device message that is malformed, or arguments the device would refuse.
    ``code`` is one of ``DEVICE_ERROR_CODES`` (``bad_args`` for anything the sender can fix)."""

    def __init__(self, code: str, message: Optional[str] = None) -> None:
        super().__init__(message or code)
        self.code = code


# --- the envelope ------------------------------------------------------------


@dataclass(frozen=True)
class DeviceHeader:
    v: int
    id: str
    seq: int
    op: str
    session: str
    #: The id of the command this answers (results only).
    re: Optional[str] = None
    #: Expiry, epoch milliseconds. Commands only; ``None`` = never expires.
    exp: Optional[int] = None


@dataclass(frozen=True)
class DeviceMessage:
    header: DeviceHeader
    #: The decoded JSON body, or ``None`` when the marker stood alone.
    body: Any = None


_MARKER_RE = re.compile(r"^\[\[SALT-DEVICE ((?:[a-z_]+=[^\s\]]+ ?)+)\]\]\s*([\s\S]*)\Z")
_INT_RE = re.compile(r"^-?[0-9]+\Z")


def _dumps(body: Any) -> str:
    # The same bytes JSON.stringify writes: no spaces, non-ASCII left as is.
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def encode_device_message(header: DeviceHeader, body: Any = None) -> str:
    """The plaintext of one lane message. A ``None`` body is the marker alone. The caller
    encrypts the returned string for the lane readers and posts it."""
    parts = [
        f"v={header.v}",
        f"id={header.id}",
        f"seq={header.seq}",
        f"op={header.op}",
        f"session={header.session}",
    ]
    if header.re is not None:
        parts.append(f"re={header.re}")
    if header.exp is not None:
        parts.append(f"exp={header.exp}")
    marker = f"[[SALT-DEVICE {' '.join(parts)}]]"
    if body is None:
        return marker
    return f"{marker}\n{_dumps(body)}"


def decode_device_message(plaintext: str) -> Optional[DeviceMessage]:
    """Parse the decrypted plaintext of a lane message. ``None`` when the text is not a device
    message at all (a plain chat line). Raises ``DeviceProtocolError("bad_args")`` only on a
    message that LOOKS like one but is structurally broken."""
    trimmed = plaintext.lstrip()
    if not trimmed.startswith("[[SALT-DEVICE"):
        return None
    m = _MARKER_RE.match(trimmed)
    if not m:
        raise DeviceProtocolError("bad_args", "malformed SALT-DEVICE marker")

    fields: dict[str, str] = {}
    for pair in m.group(1).strip().split():
        eq = pair.find("=")
        if eq == -1:
            raise DeviceProtocolError("bad_args", f"bad marker field: {pair}")
        fields[pair[:eq]] = pair[eq + 1:]

    for required in ("v", "id", "seq", "op", "session"):
        if required not in fields:
            raise DeviceProtocolError("bad_args", f"marker missing {required}")

    if not _INT_RE.match(fields["v"]) or not _INT_RE.match(fields["seq"]) or int(fields["seq"]) < 1:
        raise DeviceProtocolError("bad_args", "v and seq must be positive integers")

    exp: Optional[int] = None
    if "exp" in fields:
        try:
            exp_f = float(fields["exp"])
        except ValueError:
            exp_f = math.nan
        if not math.isfinite(exp_f):
            raise DeviceProtocolError("bad_args", "exp must be a number")
        exp = int(exp_f) if exp_f.is_integer() else exp_f  # type: ignore[assignment]

    header = DeviceHeader(
        v=int(fields["v"]),
        id=fields["id"],
        seq=int(fields["seq"]),
        op=fields["op"],
        session=fields["session"],
        re=fields.get("re"),
        exp=exp,
    )

    rest = m.group(2)
    body: Any = None
    if rest.strip():
        try:
            body = json.loads(rest, parse_constant=_refuse_constant)
        except ValueError:
            raise DeviceProtocolError("bad_args", "SALT-DEVICE body is not JSON") from None
    return DeviceMessage(header=header, body=body)


def _refuse_constant(name: str) -> Any:
    raise ValueError(name)  # NaN / Infinity are not JSON


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def device_message_id(now_ms: Optional[int] = None, rand: Optional[Callable[[], float]] = None) -> str:
    """A ULID-shaped id for a command (10 time characters, 16 random). Salt treats it as an
    opaque token; the only property relied on is uniqueness."""
    t = int(time.time() * 1000) if now_ms is None else int(now_ms)
    r = rand or random.random
    time_part = ""
    for _ in range(10):
        time_part = _CROCKFORD[t % 32] + time_part
        t //= 32
    rand_part = "".join(_CROCKFORD[int(r() * 32)] for _ in range(16))
    return time_part + rand_part


# --- helpers shared by the task shapes ----------------------------------------


def _clip(s: str, max_chars: int) -> str:
    return s[:max_chars]  # str slices count code points


def _whole(x: Any) -> Optional[int]:
    """A whole number as JavaScript's ``Number.isInteger`` sees it (1.0 counts, True does not)."""
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, float) and x.is_integer():
        return int(x)
    return None


# --- a whole task (op `task`, capability `device.task`) -----------------------


@dataclass(frozen=True)
class TaskArgs:
    """The arguments of a ``task`` command, checked and with defaults filled in."""

    goal: str
    done_when: Optional[str] = None
    max_steps: int = TASK_MAX_STEPS_DEFAULT
    timeout_s: int = TASK_TIMEOUT_S_DEFAULT

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"goal": self.goal}
        if self.done_when is not None:
            out["done_when"] = self.done_when
        out["max_steps"] = self.max_steps
        out["timeout_s"] = self.timeout_s
        return out


def parse_task_args(body: Any) -> TaskArgs:
    """The task arguments, checked and with defaults filled in; unknown keys are dropped.

    A goal that is missing, empty or over 2000 characters, or a ``done_when`` over 500, is
    ``bad_args`` (nothing is cut off silently). ``max_steps`` and ``timeout_s`` must be whole
    numbers and are clamped into their range (1..60, 10..600). Raises
    ``DeviceProtocolError("bad_args")``. Used before sending, and by a device before running."""
    if not isinstance(body, Mapping):
        raise DeviceProtocolError("bad_args", "task needs a goal")
    goal = body.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise DeviceProtocolError("bad_args", "goal must be a non-empty string")
    if len(goal) > TASK_GOAL_MAX:
        raise DeviceProtocolError("bad_args", f"goal is over {TASK_GOAL_MAX} characters")
    done_when: Optional[str] = None
    raw_done = body.get("done_when")
    if raw_done is not None:
        if not isinstance(raw_done, str):
            raise DeviceProtocolError("bad_args", "done_when must be a string")
        if len(raw_done) > TASK_DONE_WHEN_MAX:
            raise DeviceProtocolError("bad_args", f"done_when is over {TASK_DONE_WHEN_MAX} characters")
        done_when = raw_done or None
    max_steps = TASK_MAX_STEPS_DEFAULT
    if body.get("max_steps") is not None:
        n = _whole(body["max_steps"])
        if n is None:
            raise DeviceProtocolError("bad_args", "max_steps must be a whole number")
        max_steps = min(TASK_MAX_STEPS_MAX, max(TASK_MAX_STEPS_MIN, n))
    timeout_s = TASK_TIMEOUT_S_DEFAULT
    if body.get("timeout_s") is not None:
        n = _whole(body["timeout_s"])
        if n is None:
            raise DeviceProtocolError("bad_args", "timeout_s must be a whole number")
        timeout_s = min(TASK_TIMEOUT_S_MAX, max(TASK_TIMEOUT_S_MIN, n))
    return TaskArgs(goal=goal, done_when=done_when, max_steps=max_steps, timeout_s=timeout_s)


def task_command_body(
    goal: str,
    done_when: Optional[str] = None,
    max_steps: Optional[int] = None,
    timeout_s: Optional[int] = None,
) -> dict[str, Any]:
    """The wire body of a ``task`` command: only the fields that were given. Refuses
    (``bad_args``) what the device would refuse, before anything is sent."""
    raw: dict[str, Any] = {"goal": goal}
    if done_when is not None:
        raw["done_when"] = done_when
    if max_steps is not None:
        raw["max_steps"] = max_steps
    if timeout_s is not None:
        raw["timeout_s"] = timeout_s
    parsed = parse_task_args(raw)
    out: dict[str, Any] = {"goal": parsed.goal}
    if parsed.done_when is not None:
        out["done_when"] = parsed.done_when
    if max_steps is not None:
        out["max_steps"] = parsed.max_steps
    if timeout_s is not None:
        out["timeout_s"] = parsed.timeout_s
    return out


def task_command(
    goal: str,
    done_when: Optional[str] = None,
    max_steps: Optional[int] = None,
    timeout_s: Optional[int] = None,
    *,
    session: str,
    seq: int,
    id: Optional[str] = None,
    exp: Optional[int] = None,
    now_ms: Optional[int] = None,
) -> str:
    """The plaintext of a ``task`` command, ready to encrypt for the lane readers and post.

    ``seq`` is the next number in this session's strictly increasing sequence; ``id`` defaults
    to a fresh one (keep it: a resend reuses it, a ``cancel`` names it); ``exp`` defaults to
    now + ``COMMAND_TTL_MS`` (epoch milliseconds, the START window; the task's own time is
    ``timeout_s``). Wait ``timeout_s`` + ``TASK_CLIENT_GRACE_MS`` for the ``task_result``, then
    send ``cancel_command(id, ..., task=True)``."""
    body = task_command_body(goal, done_when, max_steps, timeout_s)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    header = DeviceHeader(
        v=DEVICE_PROTOCOL_VERSION,
        id=id or device_message_id(now),
        seq=seq,
        op="task",
        session=session,
        exp=exp if exp is not None else now + COMMAND_TTL_MS,
    )
    return encode_device_message(header, body)


def cancel_command(
    command_id: str,
    *,
    session: str,
    seq: int,
    task: bool = False,
    id: Optional[str] = None,
) -> str:
    """The plaintext of a ``cancel``: drops a command that has not started, or (``task=True``,
    which also names it in the header's ``re``) aborts a running task so it ends ``stopped``.
    Out of band: it consumes no sequence number, so pass the newest ``seq`` used so far
    (at least 1)."""
    header = DeviceHeader(
        v=DEVICE_PROTOCOL_VERSION,
        id=id or device_message_id(),
        seq=max(seq, 1),
        op="cancel",
        session=session,
        re=command_id if task else None,
    )
    return encode_device_message(header, {"id": command_id})


@dataclass(frozen=True)
class Progress:
    """One non-final line of a running task. ``waiting`` is true while a person is being asked."""

    step: int
    line: str
    of: Optional[int] = None
    waiting: Optional[bool] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"step": self.step}
        if self.of is not None:
            out["of"] = self.of
        out["line"] = self.line
        if self.waiting is not None:
            out["waiting"] = self.waiting
        return out


def _from_body(x: Union[str, Mapping[str, Any], None], op: str) -> Any:
    """A body given directly, or the plaintext of a lane message that must carry ``op``."""
    if isinstance(x, str):
        msg = decode_device_message(x)
        if msg is None or msg.header.op != op:
            raise DeviceProtocolError("failed", f"not a {op} message")
        return msg.body
    return x


def parse_progress(x: Union[str, Mapping[str, Any], None]) -> Optional[Progress]:
    """A ``progress`` body (or the plaintext of a ``progress`` lane message), allow-listed with
    the caps applied; ``None`` when it is not one. Only ``step``, ``of``, ``line`` and
    ``waiting`` get through."""
    try:
        raw = _from_body(x, "progress")
    except DeviceProtocolError:
        return None
    if not isinstance(raw, Mapping):
        return None
    step = _whole(raw.get("step"))
    line = raw.get("line")
    if step is None or step < 0 or not isinstance(line, str):
        return None
    of = _whole(raw.get("of"))
    waiting = raw.get("waiting")
    return Progress(
        step=step,
        line=_clip(line, PROGRESS_LINE_MAX),
        of=of if of is not None and of >= 1 else None,
        waiting=waiting if isinstance(waiting, bool) else None,
    )


@dataclass(frozen=True)
class TaskStep:
    n: int
    kind: str
    #: A command's text may appear here, truncated; its output never does.
    line: str
    ok: bool

    def to_dict(self) -> dict[str, Any]:
        return {"n": self.n, "kind": self.kind, "line": self.line, "ok": self.ok}


@dataclass(frozen=True)
class TaskChecked:
    ok: bool
    what: str

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "what": self.what}


@dataclass(frozen=True)
class TaskResult:
    """The final answer to a ``task``. Words and numbers only: command output and
    screenshots never leave the computer."""

    #: The device's local record id (``t_<hex>``).
    task_id: str
    #: One of ``TASK_STATUSES``.
    status: str
    #: One or two plain sentences, at most 500 characters.
    summary: str
    checked: Optional[TaskChecked]
    steps: tuple[TaskStep, ...] = field(default_factory=tuple)
    commands: int = 0
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary,
            "checked": self.checked.to_dict() if self.checked else None,
            "steps": [s.to_dict() for s in self.steps],
            "commands": self.commands,
            "duration_ms": self.duration_ms,
        }


def new_task_id(rand: Optional[Callable[[], float]] = None) -> str:
    """A fresh local task id, ``t_<12 hex>``."""
    r = rand or random.random
    return "t_" + "".join(format(int(r() * 16), "x") for _ in range(12))


def parse_task_result(
    x: Union[str, Mapping[str, Any], None],
    *,
    task_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> TaskResult:
    """A ``task_result`` body (or the plaintext of a ``task_result`` lane message), allow-listed
    field by field with type checks and caps. Extra fields never travel and a malformed one
    becomes a safe default rather than a raise: an unknown ``status`` reads ``failed``, ``steps``
    keeps the first 60 valid rows, strings are cut to their caps, numbers must be whole and
    non-negative. ``task_id`` / ``duration_ms`` fill what the body lacks. Raises
    ``DeviceProtocolError`` only when given the plaintext of some other message."""
    raw = _from_body(x, "task_result")
    r: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    tid = r.get("task_id")
    out_id = _clip(tid, TASK_ID_MAX) if isinstance(tid, str) and tid else (task_id or new_task_id())
    status = r.get("status")
    summary = r.get("summary")
    checked: Optional[TaskChecked] = None
    raw_checked = r.get("checked")
    if isinstance(raw_checked, Mapping) and isinstance(raw_checked.get("ok"), bool):
        what = raw_checked.get("what")
        checked = TaskChecked(
            ok=raw_checked["ok"],
            what=_clip(what, TASK_CHECKED_WHAT_MAX) if isinstance(what, str) else "",
        )
    steps: list[TaskStep] = []
    raw_steps = r.get("steps")
    if isinstance(raw_steps, list):
        for s in raw_steps:
            if len(steps) >= TASK_RESULT_STEPS_MAX:
                break
            if not isinstance(s, Mapping) or s.get("kind") not in TASK_STEP_KINDS or not isinstance(s.get("line"), str):
                continue
            n = _whole(s.get("n"))
            steps.append(
                TaskStep(
                    n=n if n is not None and n >= 0 else len(steps) + 1,
                    kind=s["kind"],
                    line=_clip(s["line"], TASK_STEP_LINE_MAX),
                    ok=s.get("ok") is True,
                )
            )
    commands = _whole(r.get("commands"))
    duration = _whole(r.get("duration_ms"))
    return TaskResult(
        task_id=out_id,
        status=status if status in TASK_STATUSES else "failed",
        summary=_clip(summary, TASK_SUMMARY_MAX) if isinstance(summary, str) else "",
        checked=checked,
        steps=tuple(steps),
        commands=commands if commands is not None and commands >= 0 else 0,
        duration_ms=duration if duration is not None and duration >= 0 else (duration_ms or 0),
    )
