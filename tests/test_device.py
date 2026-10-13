# saltapp.device: the [[SALT-DEVICE ...]] envelope and the whole-task shapes, held to
# tests/fixtures/device_envelope_vectors.json. The SAME file is committed, byte for byte, in
# salt-agent-sdk (tests/fixtures/device_envelope_vectors.json); scripts/generate_device_vectors.cjs
# wrote it from the real compiled TypeScript SDK, and salt-agent-sdk's tests/device-vectors.test.js
# runs it too. So: what TypeScript encoded, Python decodes to the same header and body; what
# Python encodes is the same bytes TypeScript wrote; every normalisation gives the same answer.
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from saltapp import device as dev
from saltapp.device import (
    DeviceHeader,
    DeviceProtocolError,
    decode_device_message,
    encode_device_message,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "device_envelope_vectors.json").read_text(encoding="utf-8"))


def header_from(d: dict) -> DeviceHeader:
    return DeviceHeader(**d)


def header_dict(h: DeviceHeader) -> dict:
    return {k: v for k, v in asdict(h).items() if v is not None}


def test_fixture_is_well_formed():
    assert len(FIXTURE["envelopes"]) >= 10
    assert len(FIXTURE["decode_only"]) >= 10
    assert len(FIXTURE["task_args"]) >= 15
    assert len(FIXTURE["progress"]) >= 10
    assert len(FIXTURE["task_result"]) >= 10


def test_constants_match_the_ones_the_fixture_was_written_with():
    for name, want in FIXTURE["constants"].items():
        got = getattr(dev, name)
        if isinstance(want, list):
            assert list(got) == want, name
        else:
            assert got == want, name


@pytest.mark.parametrize("v", FIXTURE["envelopes"], ids=lambda v: v["name"])
def test_encode_is_the_bytes_typescript_wrote(v):
    assert encode_device_message(header_from(v["header"]), v.get("body")) == v["text"]


@pytest.mark.parametrize("v", FIXTURE["envelopes"], ids=lambda v: v["name"])
def test_decode_gives_the_header_and_body_typescript_encoded(v):
    m = decode_device_message(v["text"])
    assert m is not None
    assert header_dict(m.header) == v["decoded_header"] == v["header"]
    assert m.body == v.get("decoded_body")


@pytest.mark.parametrize("v", FIXTURE["decode_only"], ids=lambda v: v["name"])
def test_decode_only_vectors(v):
    if "expect_error" in v:
        with pytest.raises(DeviceProtocolError) as e:
            decode_device_message(v["text"])
        assert e.value.code == v["expect_error"]
    elif v["expect"] is None:
        assert decode_device_message(v["text"]) is None
    else:
        m = decode_device_message(v["text"])
        assert header_dict(m.header) == v["expect"]["header"]
        assert m.body == v["expect"].get("body")


@pytest.mark.parametrize("v", FIXTURE["task_args"], ids=lambda v: v["name"])
def test_task_args_vectors(v):
    if "expect_error" in v:
        with pytest.raises(DeviceProtocolError) as e:
            dev.parse_task_args(v["input"])
        assert e.value.code == v["expect_error"]
    else:
        assert dev.parse_task_args(v["input"]).to_dict() == v["expect"]


@pytest.mark.parametrize("v", FIXTURE["progress"], ids=lambda v: v["name"])
def test_progress_vectors(v):
    got = dev.parse_progress(v["input"])
    assert (got.to_dict() if got else None) == v["expect"]


@pytest.mark.parametrize("v", FIXTURE["task_result"], ids=lambda v: v["name"])
def test_task_result_vectors(v):
    assert dev.parse_task_result(v["input"]).to_dict() == v["expect"]


# --- what the fixture cannot say -----------------------------------------------


def test_task_command_builds_the_wire_text_a_device_decodes():
    text = dev.task_command(
        "Open saltapp.ai in Firefox",
        done_when="the page title says saltapp.ai",
        max_steps=20,
        timeout_s=120,
        session="s1",
        seq=2,
        id="C1",
        now_ms=1_760_000_000_000,
    )
    assert text == (
        "[[SALT-DEVICE v=1 id=C1 seq=2 op=task session=s1 exp=1760000030000]]\n"
        '{"goal":"Open saltapp.ai in Firefox","done_when":"the page title says saltapp.ai","max_steps":20,"timeout_s":120}'
    )
    m = decode_device_message(text)
    assert m.header.op == "task" and m.header.exp == 1_760_000_030_000
    assert dev.parse_task_args(m.body).goal == "Open saltapp.ai in Firefox"


def test_task_command_with_only_a_goal_sends_only_the_goal_and_makes_an_id():
    text = dev.task_command("Empty Downloads", session="s1", seq=1, now_ms=1_000)
    m = decode_device_message(text)
    assert m.body == {"goal": "Empty Downloads"}
    assert len(m.header.id) == 26 and m.header.exp == 1_000 + dev.COMMAND_TTL_MS


def test_task_command_refuses_what_the_device_would_refuse_before_anything_is_built():
    for kwargs in (
        {"goal": ""},
        {"goal": "x" * 2001},
        {"goal": "g", "done_when": "x" * 501},
        {"goal": "g", "max_steps": 1.5},
        {"goal": "g", "timeout_s": "30"},
    ):
        with pytest.raises(DeviceProtocolError) as e:
            dev.task_command(session="s", seq=1, **kwargs)
        assert e.value.code == "bad_args"


def test_task_command_clamps_numbers_it_was_given():
    body = decode_device_message(dev.task_command("g", max_steps=999, timeout_s=2, session="s", seq=1)).body
    assert body == {"goal": "g", "max_steps": 60, "timeout_s": 10}


def test_cancel_command_names_a_task_in_re_and_in_the_body():
    t = decode_device_message(dev.cancel_command("T1", session="s", seq=3, task=True, id="X1"))
    assert t.header.op == "cancel" and t.header.re == "T1" and t.body == {"id": "T1"} and t.header.seq == 3
    plain = decode_device_message(dev.cancel_command("C9", session="s", seq=0, id="X2"))
    assert plain.header.re is None and plain.body == {"id": "C9"} and plain.header.seq == 1


def test_parse_accepts_the_lane_plaintext_as_well_as_a_body():
    prog = encode_device_message(
        DeviceHeader(v=1, id="P1", seq=5, op="progress", session="s", re="T1"), {"step": 1, "line": "Opening", "extra": 1}
    )
    assert dev.parse_progress(prog) == dev.Progress(step=1, line="Opening")
    result = encode_device_message(
        DeviceHeader(v=1, id="R1", seq=6, op="task_result", session="s", re="T1"),
        {"task_id": "t_1", "status": "done", "summary": "Done.", "checked": None, "steps": [], "commands": 0, "duration_ms": 5},
    )
    assert dev.parse_task_result(result).status == "done"
    # a message that is some other op is not a progress line, and is not a result
    assert dev.parse_progress(result) is None
    with pytest.raises(DeviceProtocolError):
        dev.parse_task_result(prog)
    with pytest.raises(DeviceProtocolError):
        dev.parse_task_result("just a chat line")


def test_a_missing_task_id_gets_a_fresh_t_hex_id_and_the_fallbacks_fill_in():
    r = dev.parse_task_result({"status": "done", "summary": "s"}, duration_ms=1234)
    assert r.task_id.startswith("t_") and len(r.task_id) == 14
    assert r.duration_ms == 1234
    assert dev.parse_task_result({}, task_id="t_given").task_id == "t_given"
    assert dev.parse_task_result(None).status == "failed"


def test_whole_numbers_follow_javascript_so_2_0_is_whole_and_booleans_are_not_numbers():
    r = dev.parse_task_result({"task_id": "t_1", "status": "done", "commands": 2.0, "duration_ms": True})
    assert r.commands == 2 and r.duration_ms == 0
    assert dev.parse_progress({"step": 3.0, "line": "x"}).step == 3
    assert dev.parse_progress({"step": True, "line": "x"}) is None
    assert dev.parse_task_args({"goal": "g", "max_steps": 10.0}).max_steps == 10
    with pytest.raises(DeviceProtocolError):
        dev.parse_task_args({"goal": "g", "max_steps": True})


def test_results_are_immutable_values_whose_to_dict_is_the_wire_shape():
    r = dev.parse_task_result({"task_id": "t_1", "status": "done", "summary": "s", "checked": {"ok": True, "what": "w"}, "steps": [{"n": 1, "kind": "check", "line": "l", "ok": True}]})
    assert r.to_dict()["checked"] == {"ok": True, "what": "w"}
    assert isinstance(r.steps, tuple)
    with pytest.raises(Exception):
        r.status = "failed"  # frozen


def test_nan_and_infinity_are_not_json_bodies():
    for body in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(DeviceProtocolError):
            decode_device_message(f"[[SALT-DEVICE v=1 id=a seq=1 op=observe session=s]]\n{{\"x\":{body}}}")
    with pytest.raises(ValueError):
        encode_device_message(DeviceHeader(v=1, id="a", seq=1, op="observe", session="s"), {"x": float("nan")})


def test_device_message_id_is_26_crockford_characters_and_time_ordered():
    a = dev.device_message_id(1_000, rand=lambda: 0.0)
    b = dev.device_message_id(2_000, rand=lambda: 0.0)
    assert len(a) == 26 and a < b
    assert set(a) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    assert dev.device_message_id(1_760_000_000_000, rand=lambda: 0.5) == dev.device_message_id(1_760_000_000_000, rand=lambda: 0.5)
