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
# dropped until it fits). The line sits after the report's title.
from __future__ import annotations

import json
import math
from typing import Any, Optional

MAX_ADVANTAGE_CHARS = 4000
MAX_ADVANTAGE_EDGES = 8
_MAX_LABEL = 120
_LIST_KEYS = ("open", "settled", "testing", "kept", "dropped")


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _edges(raw: Any) -> list[dict[str, Any]]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        label = " ".join(str(item.get("label") or "").split())[:_MAX_LABEL]
        edge, n = _number(item.get("edge")), _number(item.get("n"))
        if label and edge is not None and edge > 0 and n is not None and n >= 0:
            out.append({"label": label, "edge": edge, "n": int(n) if n == int(n) else n})
    return out


def format_advantage(advantage: Optional[dict]) -> Optional[str]:
    """The JSON for an `advantage:` line, or None when `advantage` has no usable shape."""
    if not isinstance(advantage, dict) or not isinstance(advantage.get("edges"), list):
        return None
    edges = sorted(_edges(advantage["edges"]), key=lambda e: -e["edge"])[:MAX_ADVANTAGE_EDGES]
    explore = _number(advantage.get("explore")) or 0

    def build() -> str:
        body = {"edges": edges, **{key: [] for key in _LIST_KEYS}, "explore": explore}
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))

    text = build()
    while len(text) > MAX_ADVANTAGE_CHARS and edges:
        edges.remove(min(edges, key=lambda e: e["n"]))
        text = build()
    return text if len(text) <= MAX_ADVANTAGE_CHARS else None


def parse_advantage(value: str) -> Optional[dict]:
    """An `advantage:` line's JSON as {"edges": [...], "explore": float|None, plus any phase 2 lists}, or None."""
    if not value or len(value) > MAX_ADVANTAGE_CHARS:
        return None
    try:
        raw = json.loads(value)
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("edges"), list):
        return None
    advantage: dict[str, Any] = {"edges": _edges(raw["edges"])[:MAX_ADVANTAGE_EDGES]}
    for key in _LIST_KEYS:
        if isinstance(raw.get(key), list):
            advantage[key] = raw[key]
    explore = _number(raw.get("explore"))
    if explore is not None:
        advantage["explore"] = explore
    return advantage


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
