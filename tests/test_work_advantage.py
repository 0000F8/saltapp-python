# The `advantage:` line of a work report: same shape salt-agent-sdk and salt-fe use.
import json

from saltapp.work import (
    MAX_ADVANTAGE_CHARS,
    add_advantage_line,
    format_advantage,
    parse_advantage,
    read_advantage_line,
)

REPORT = "[[SALT-WORK id=w_1 status=running kind=task]]\nGrowing the list\nDay 3"
EDGES = [
    {"label": "send_outreach · email · shops", "edge": 1.2, "n": 4},
    {"label": "post_to_profile · hacker_news · founders", "edge": 1.8, "n": 12},
]


def test_line_round_trips_sorted_with_phase_two_keys_empty():
    text = add_advantage_line(REPORT, {"edges": EDGES, "explore": 0.2})
    marker, title, line, detail = text.split("\n")
    assert marker.startswith("[[SALT-WORK ") and title == "Growing the list" and detail == "Day 3"
    sent = json.loads(line[len("advantage: "):])
    assert [e["edge"] for e in sent["edges"]] == [1.8, 1.2]
    assert [sent[k] for k in ("open", "settled", "testing", "kept", "dropped")] == [[], [], [], [], []]
    back = read_advantage_line(text)
    assert back["edges"] == sent["edges"] and back["explore"] == 0.2


def test_keeps_eight_edges_and_fits_the_cap():
    many = [{"label": "z" * 300, "edge": 1 + i / 100, "n": i} for i in range(12)]
    out = format_advantage({"edges": many})
    parsed = json.loads(out)
    assert len(parsed["edges"]) == 8 and parsed["edges"][0]["edge"] == 1.11
    assert len(parsed["edges"][0]["label"]) == 120
    assert len(out) <= MAX_ADVANTAGE_CHARS


def test_drops_lowest_n_first_when_over_the_cap(monkeypatch):
    import saltapp.work as work

    monkeypatch.setattr(work, "MAX_ADVANTAGE_CHARS", 330)
    edges = [{"label": "a" * 60, "edge": 2 - i / 10, "n": n} for i, n in enumerate([9, 1, 5, 7])]
    kept = json.loads(work.format_advantage({"edges": edges}))["edges"]
    assert 0 < len(kept) < 4 and 1 not in [e["n"] for e in kept]


def test_missing_or_bad_line_is_no_advantage():
    assert read_advantage_line(REPORT) is None
    assert read_advantage_line("[[SALT-WORK id=w_1 status=done]]\nx\nadvantage: {nope") is None
    assert parse_advantage("[1]") is None
    assert parse_advantage('{"edges":"x"}') is None
    assert parse_advantage("") is None
    assert format_advantage(None) is None
    assert add_advantage_line(REPORT, None) == REPORT
