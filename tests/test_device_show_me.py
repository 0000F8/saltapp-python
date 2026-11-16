# saltapp.device: the Show Me body shapes the device client reads (op `still`, op `pointed`).
# Mirrors the normalizeStill / normalizePointed cases of salt-agent-sdk's protocol.ts. Pure: no I/O.
from __future__ import annotations

import math

from saltapp import device as dev
from saltapp.device import parse_pointed, parse_still

STILL = {"help_id": "h", "obs": "O1", "width": 10, "height": 8, "scale": 2, "format": "jpeg", "image_b64": "AA", "taken_at": "2026-11-16T09:00:00Z"}
POINTED = {
    "help_id": "h", "x": 0.25, "y": 0.75, "screen": {"x": 100, "y": 200}, "app": "Finder", "refused": None, "words": "this one",
    "element": {"role": "button", "subrole": "close", "name": "Close", "value": "v", "bounds": {"x": 1, "y": 2, "w": 3, "h": 4}},
}


def test_the_show_me_constants_and_ops_match_the_typescript_ones():
    assert dev.DEVICE_UNSOLICITED_OPS == ("still", "pointed")
    assert dev.STILL_MIN_INTERVAL_MS == 2_000 and dev.STILL_MAX_PER_MINUTE == 30
    assert dev.POINT_WORDS_MAX == 200 and dev.POINT_ID_MAX == 64 and dev.POINTED_NAME_MAX == 200
    assert dev.POINTED_REFUSALS == ("forbidden_app", "secure_field", "stale", "not_allowed")
    assert not set(dev.DEVICE_UNSOLICITED_OPS) & set(dev.DEVICE_RESULT_OPS)  # kept out of the fixture-pinned list


def test_parse_still_allow_lists_and_drops_extras():
    s = parse_still({**STILL, "extra": "x"})
    assert (s.help_id, s.obs, s.width, s.height, s.scale, s.format, s.image_b64, s.taken_at) == ("h", "O1", 10, 8, 2.0, "jpeg", "AA", "2026-11-16T09:00:00Z")
    assert not hasattr(s, "extra")
    assert parse_still({k: v for k, v in STILL.items() if k != "taken_at"}).taken_at == ""


def test_parse_still_refuses_anything_that_is_not_a_still():
    for bad in (
        None, "x", [], {},
        {**STILL, "help_id": ""}, {**STILL, "obs": 3}, {**STILL, "format": "gif"}, {**STILL, "image_b64": ""},
        {**STILL, "width": 0}, {**STILL, "height": 1.5}, {**STILL, "width": True}, {**STILL, "scale": 0},
        {**STILL, "scale": math.inf}, {**STILL, "scale": "2"},
    ):
        assert parse_still(bad) is None, bad


def test_parse_pointed_reads_the_element_and_clips_the_text():
    p = parse_pointed({**POINTED, "words": "w" * 300, "app": "a" * 300, "element": {**POINTED["element"], "name": "n" * 300}})
    assert (p.help_id, p.x, p.y, dict(p.screen), p.refused) == ("h", 0.25, 0.75, {"x": 100.0, "y": 200.0}, None)
    assert len(p.words) == 200 and len(p.app) == 200 and len(p.element.name) == 200
    assert (p.element.role, p.element.subrole, p.element.value) == ("button", "close", "v")
    assert dict(p.element.bounds) == {"x": 1, "y": 2, "w": 3, "h": 4}


def test_a_refused_pointed_answer_never_carries_an_element():
    p = parse_pointed({**POINTED, "refused": "secure_field"})
    assert p.refused == "secure_field" and p.element is None
    assert parse_pointed({**POINTED, "refused": "something_else"}).refused is None  # unknown reads as not refused


def test_pointed_without_an_element_or_with_odd_fields_gets_safe_defaults():
    p = parse_pointed({"help_id": "h", "x": 0, "y": 1, "screen": {"x": 0, "y": 0}})
    assert p.element is None and p.app is None and p.words == ""
    q = parse_pointed({**POINTED, "element": {"role": "group", "bounds": "nope"}})
    assert dict(q.element.bounds) == {"x": 0, "y": 0, "w": 0, "h": 0} and q.element.name is None and q.element.subrole is None


def test_parse_pointed_refuses_anything_that_is_not_a_pointed_body():
    for bad in (
        None, "x", [], {},
        {**POINTED, "help_id": ""}, {**POINTED, "x": 1.5}, {**POINTED, "y": -0.1}, {**POINTED, "x": "0.5"}, {**POINTED, "x": True},
        {**POINTED, "x": math.nan}, {**POINTED, "screen": None}, {**POINTED, "screen": {"x": 1}},
    ):
        assert parse_pointed(bad) is None, bad
