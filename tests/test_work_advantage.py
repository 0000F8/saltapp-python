# The `advantage:` line of a work report: same shape salt-agent-sdk and salt-fe use.
import json

import pytest

from saltapp.work import (
    MAX_ADVANTAGE_CHARS,
    add_advantage_line,
    format_advantage,
    format_advantage_tap,
    parse_advantage_tap,
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


def test_phase_two_lists_round_trip_with_ids_and_drop_bad_rows():
    adv = {
        "edges": [{"label": "post_to_profile · hacker_news", "edge": 1.8, "n": 12, "arm_key": "ab12cd34ef56ab12"}],
        "open": [{"q": "Do evening posts beat morning posts?", "by": "2026-11-02"}],
        "settled": [{"q": "Does a short title help?", "verdict": "supported", "lift": 1.4}, {"q": "bad", "verdict": "maybe"}],
        "testing": [{"id": "l_test1", "text": "Lead with a question"}],
        "kept": [{"id": "l_kept1", "text": "Show the result first", "lift": 1.3}],
        "dropped": [{"text": "Post three times a day", "lift": 0.7, "why": "People stopped replying"}],
        "explore": 0.2,
    }
    back = read_advantage_line(add_advantage_line(REPORT, adv))
    assert back["edges"][0]["arm_key"] == "ab12cd34ef56ab12"
    assert back["open"] == [{"q": "Do evening posts beat morning posts?", "by": "2026-11-02"}]
    assert [x["verdict"] for x in back["settled"]] == ["supported"]
    assert back["testing"][0]["id"] == "l_test1" and back["kept"][0]["lift"] == 1.3
    assert back["dropped"][0]["why"] == "People stopped replying" and back["explore"] == 0.2
    assert parse_advantage('{"edges":[]}')["open"] == []


def test_over_the_cap_trims_oldest_rows():
    long = lambda i: "w" * 190 + str(i)  # noqa: E731
    adv = {
        "edges": [],
        "open": [{"q": long(i)} for i in range(10)],
        "testing": [{"text": long(i)} for i in range(10)],
        "kept": [{"text": long(i)} for i in range(10)],
    }
    out = format_advantage(adv)
    assert len(out) <= MAX_ADVANTAGE_CHARS
    parsed = json.loads(out)
    assert len(parsed["open"]) + len(parsed["testing"]) + len(parsed["kept"]) < 30
    assert not parsed["open"] or parsed["open"][-1]["q"].endswith("9")


def test_tap_markers_write_read_and_refuse_exactly():
    assert format_advantage_tap("never", "arm", "ab12") == "[[SALT-ADV-NEVER kind=arm key=ab12]]"
    assert format_advantage_tap("never", "lens", "l_1") == "[[SALT-ADV-NEVER kind=lens id=l_1]]"
    assert format_advantage_tap("keep", "lens", "l_1") == "[[SALT-ADV-KEEP kind=lens id=l_1]]"
    with pytest.raises(ValueError):
        format_advantage_tap("keep", "arm", "x")
    with pytest.raises(ValueError):
        format_advantage_tap("never", "lens", "bad id")
    assert parse_advantage_tap("[[SALT-ADV-KEEP kind=lens id=l_1]]") == {"action": "keep", "kind": "lens", "ref": "l_1"}
    assert parse_advantage_tap("[[SALT-ADV-NEVER kind=arm key=ab12]]") == {"action": "never", "kind": "arm", "ref": "ab12"}
    for bad in ("please [[SALT-ADV-KEEP kind=lens id=l_1]]", "[[SALT-ADV-KEEP kind=arm key=x]]", "[[SALT-ADV-NEVER kind=lens key=l_1]]", ""):
        assert parse_advantage_tap(bad) is None
