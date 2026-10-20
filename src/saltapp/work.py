# The `advantage:` line of a Salt work report (DYNAMIC_ADVANTAGE phase 1).
#
# This SDK has no work reporter (see AGENTS.md); a caller that builds a
# `[[SALT-WORK ...]]` report itself can write and read this one line with the
# same shape salt-agent-sdk's `formatAdvantage`/`parseAdvantage` and salt-fe's
# `parseAdvantage` use:
#
#   advantage: {"edges":[{"label","edge","n"}],"open":[],"settled":[],
#               "testing":[],"kept":[],"dropped":[],"explore":0.2}
#
# At most 8 edges, highest edge first, 4000 characters (the lowest-n edges are
# dropped until it fits). The line sits after the report's title. Phase 2 adds
# the lists below and the owner's tap markers.
from __future__ import annotations

import json
import math
import re
from typing import Any, Optional

MAX_ADVANTAGE_CHARS = 4000
MAX_ADVANTAGE_EDGES = 8
_MAX_LABEL = 120
_LIST_MAX = 10
_LIST_KEYS = ("open", "settled", "testing", "kept", "dropped")
_VERDICTS = ("supported", "refuted", "inconclusive")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Phase 2 lists (ADVANTAGE_P2_CONTRACT): open [{q, by}], settled [{q, verdict,
# lift}], testing and kept [{id, text, lift}], dropped [{text, lift, why}]; an
# edge may carry its `arm_key` and a lens its `id`, so the owner's taps
# (format_advantage_tap below) can name them.


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _id(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _ID_RE.match(value) else None


def _positive(value: Any) -> Optional[float]:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _rows(raw: Any) -> list:
    return [x for x in (raw[:_LIST_MAX] if isinstance(raw, list) else []) if isinstance(x, dict)]


def _edges(raw: Any) -> list[dict[str, Any]]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        label = _line(item.get("label"), _MAX_LABEL)
        edge, n = _number(item.get("edge")), _number(item.get("n"))
        if label and edge is not None and edge > 0 and n is not None and n >= 0:
            row: dict[str, Any] = {"label": label, "edge": edge, "n": int(n) if n == int(n) else n}
            key = _id(item.get("arm_key"))
            if key:
                row["arm_key"] = key
            out.append(row)
    return out


def _lens(x: dict) -> dict[str, Any]:
    row: dict[str, Any] = {"text": _line(x.get("text") or x.get("label"), 160)}
    if _id(x.get("id")):
        row["id"] = x["id"]
    if _positive(x.get("lift")) is not None:
        row["lift"] = _positive(x.get("lift"))
    return row


def _normalise(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict) or not isinstance(raw.get("edges"), list):
        return None
    advantage: dict[str, Any] = {"edges": _edges(raw["edges"])}
    advantage["open"] = [
        {k: v for k, v in {"q": _line(x.get("q"), 200), "by": _line(x.get("by"), 40) or None}.items() if v is not None}
        for x in _rows(raw.get("open"))
        if _line(x.get("q"), 200)
    ]
    advantage["settled"] = [
        {k: v for k, v in {"q": _line(x.get("q"), 200), "verdict": x.get("verdict"), "lift": _positive(x.get("lift"))}.items() if v is not None}
        for x in _rows(raw.get("settled"))
        if _line(x.get("q"), 200) and x.get("verdict") in _VERDICTS
    ]
    advantage["testing"] = [r for r in map(_lens, _rows(raw.get("testing"))) if r["text"]]
    advantage["kept"] = [r for r in map(_lens, _rows(raw.get("kept"))) if r["text"]]
    advantage["dropped"] = [
        {k: v for k, v in {"text": _line(x.get("text") or x.get("label"), 160), "lift": _positive(x.get("lift")), "why": _line(x.get("why"), 200) or None}.items() if v is not None}
        for x in _rows(raw.get("dropped"))
        if _line(x.get("text") or x.get("label"), 160)
    ]
    explore = _number(raw.get("explore"))
    if explore is not None:
        advantage["explore"] = explore
    return advantage


def format_advantage(advantage: Optional[dict]) -> Optional[str]:
    """The JSON for an `advantage:` line, or None when `advantage` has no usable shape.

    Edges sorted highest first, at most 8, every list present; over 4000
    characters the lowest-n edge goes first, then the oldest row of the longest
    other list."""
    body = _normalise(advantage)
    if body is None:
        return None
    body["edges"] = sorted(body["edges"], key=lambda e: -e["edge"])[:MAX_ADVANTAGE_EDGES]
    body["explore"] = body.get("explore") or 0

    def build() -> str:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))

    text = build()
    while len(text) > MAX_ADVANTAGE_CHARS:
        if body["edges"]:
            body["edges"].remove(min(body["edges"], key=lambda e: e["n"]))
        else:
            longest = max(_LIST_KEYS, key=lambda k: len(body[k]))
            if not body[longest]:
                break
            body[longest].pop(0)
        text = build()
    return text if len(text) <= MAX_ADVANTAGE_CHARS else None


def parse_advantage(value: str) -> Optional[dict]:
    """An `advantage:` line's JSON as a dict (edges plus the phase 2 lists, explore when sent), or None."""
    if not value or len(value) > MAX_ADVANTAGE_CHARS:
        return None
    try:
        raw = json.loads(value)
    except ValueError:
        return None
    body = _normalise(raw)
    if body is not None:
        body["edges"] = body["edges"][:MAX_ADVANTAGE_EDGES]
    return body


# The owner's taps on What's working (flow C): a whole message from the person,
# one exact token, into the lane the plan's reports come from. Act only when the
# sender is the owner or the person the plan serves; never show the marker.
#   [[SALT-ADV-NEVER kind=arm key=<arm_key>]]   [[SALT-ADV-NEVER kind=lens id=<lens_id>]]
#   [[SALT-ADV-KEEP kind=lens id=<lens_id>]]
_TAP_RE = re.compile(r"^\[\[SALT-ADV-(NEVER|KEEP) kind=(arm key|lens id)=([A-Za-z0-9_-]{1,64})\]\]$")


def format_advantage_tap(action: str, kind: str, ref: str) -> str:
    """The wire message for a tap. Raises ValueError on a combination the protocol does not have."""
    if not isinstance(ref, str) or not _ID_RE.match(ref):
        raise ValueError("A tap needs a key or id of letters, digits, _ or -.")
    if action not in ("never", "keep") or kind not in ("arm", "lens"):
        raise ValueError("action must be never or keep; kind must be arm or lens.")
    if action == "keep" and kind != "lens":
        raise ValueError("Keep going names a lens.")
    return f"[[SALT-ADV-{action.upper()} kind={kind} {'key' if kind == 'arm' else 'id'}={ref}]]"


def parse_advantage_tap(text: str) -> Optional[dict]:
    """{"action", "kind", "ref"} for a message that is exactly one tap marker, else None."""
    match = _TAP_RE.match((text or "").strip())
    if not match:
        return None
    action, kind = match.group(1).lower(), match.group(2).split(" ")[0]
    if action == "keep" and kind != "lens":
        return None
    return {"action": action, "kind": kind, "ref": match.group(3)}


def add_advantage_line(report: str, advantage: Optional[dict]) -> str:
    """`report` (a work report's plaintext) with the `advantage:` line after its title, or unchanged."""
    line = format_advantage(advantage)
    if line is None:
        return report
    lines = report.split("\n")
    # Line 1 is the [[SALT-WORK ...]] marker and line 2 the title.
    return "\n".join(lines[:2] + ["advantage: " + line] + lines[2:])


def read_advantage_line(report: str) -> Optional[dict]:
    """The advantage in a work report's plaintext, or None when it has no good `advantage:` line."""
    for line in (report or "").split("\n")[1:]:
        stripped = line.strip()
        if stripped.startswith("advantage:"):
            return parse_advantage(stripped[len("advantage:"):].strip())
    return None


# --- Reading the plan's report back (Salt 0.243.0) --------------------------
# A plan's report is a work report with `kind=strategy` and a `strategy: {json}`
# line, posted into the private lane the agent shares with the person. Salt only
# holds ciphertext, so `SaltClient.read_plan_report` fetches the lane's messages
# and decrypts them here with the agent's own key. The JSON is returned as the
# writer sent it (salt-fe `parseStrategy` is the reference reader for its fields);
# only the marker is checked. Mirrors salt-agent-sdk `parsePlanReport`.

STRATEGY_REPORT_STATUSES = ("attempting", "blocked", "succeeded", "abandoned", "superseded")
_STRATEGY_JSON_MAX = 20000
_MARKER_RE = re.compile(r"^\[\[SALT-WORK ([^\]\n]*)\]\]\n?")
_PGP_RE = re.compile(r"^-----BEGIN PGP MESSAGE-----")


def parse_plan_report(plaintext: str) -> Optional[dict]:
    """The plan report in a plaintext, `{"id", "status", "title", "strategy"[, "detail"]}`, or None when it is not a
    `kind=strategy` work report with a readable `strategy:` line."""
    match = _MARKER_RE.match(plaintext or "")
    if not match:
        return None
    attrs: dict[str, str] = {}
    for pair in match.group(1).split():
        key, eq, value = pair.partition("=")
        if eq and key:
            attrs[key] = value
    if attrs.get("kind") != "strategy" or not _ID_RE.match(attrs.get("id", "")):
        return None
    title, *rest = plaintext[match.end():].split("\n")
    strategy: Optional[dict] = None
    kept: list[str] = []
    for line in rest:
        stripped = line.strip()
        if not stripped.startswith("strategy:"):
            kept.append(line)
            continue
        raw = stripped[len("strategy:"):].strip()
        if not raw or len(raw) > _STRATEGY_JSON_MAX:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            strategy = parsed
    if strategy is None:
        return None
    status = strategy.get("status") if strategy.get("status") in STRATEGY_REPORT_STATUSES else attrs.get("status")
    if status not in STRATEGY_REPORT_STATUSES:
        return None
    report: dict[str, Any] = {"id": attrs["id"], "status": status, "title": title.strip(), "strategy": strategy}
    detail = "\n".join(kept).strip()
    if detail:
        report["detail"] = detail
    return report


def _when(value: Any) -> Optional[float]:
    from datetime import datetime

    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def latest_plan_report(messages: list, identity: Any, strategy_id: Optional[str] = None) -> Optional[dict]:
    """The newest plan report among a lane's messages (oldest first), each decrypted with `identity` (an `Identity`);
    a message that key cannot open is skipped. `strategy_id` keeps only that plan's reports. Adds `message_id` and
    `created_at` from the message."""
    from saltapp import crypto

    latest: Optional[dict] = None
    for m in messages or []:
        if not isinstance(m, dict) or m.get("event_type") or not isinstance(m.get("message"), str):
            continue
        if m.get("encrypted") is False:
            plaintext = m["message"]
        else:
            if not _PGP_RE.match(m["message"].strip()):
                continue
            try:
                plaintext = crypto.decrypt(m["message"], identity.private_key, identity.passphrase)
            except Exception:  # noqa: BLE001 -- predates this key, or not for it: skip, never fatal
                continue
        report = parse_plan_report(plaintext)
        if report is None:
            continue
        if strategy_id and (report["strategy"].get("strategy_id") or report["id"]) != strategy_id:
            continue
        at = _when(m.get("created_at"))
        latest_at = _when(latest.get("created_at")) if latest else None
        # Chat order is oldest first; a later message wins unless both carry times and it is older.
        if latest is not None and at is not None and latest_at is not None and at < latest_at:
            continue
        latest = {**report, **({"message_id": m["id"]} if m.get("id") is not None else {}), **({"created_at": m["created_at"]} if m.get("created_at") else {})}
    return latest
