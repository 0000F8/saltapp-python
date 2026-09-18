from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from saltapp.webhook import (
    AGENT_ID_HEADER,
    DELIVERY_ID_HEADER,
    SIGNATURE_HEADER,
    Event,
    WebhookVerificationError,
    classify,
    handle,
    parse_signature_header,
    verify_agent_id,
    verify_signature,
)

SECRET = "shh"


def sign(secret: str, t: int, raw: bytes) -> str:
    digest = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


def test_parse_signature_header():
    t, v1 = parse_signature_header("t=1700000000,v1=abcdef")
    assert t == 1700000000
    assert v1 == "abcdef"


def test_parse_signature_header_malformed():
    with pytest.raises(WebhookVerificationError):
        parse_signature_header("garbage")


def test_verify_signature_accepts_valid():
    body = b'{"hello":"world"}'
    t = int(time.time())
    header = sign(SECRET, t, body)
    verify_signature(body, header, SECRET)  # must not raise


def test_verify_signature_rejects_tampered_body():
    body = b'{"hello":"world"}'
    t = int(time.time())
    header = sign(SECRET, t, body)
    with pytest.raises(WebhookVerificationError):
        verify_signature(b'{"hello":"tampered"}', header, SECRET)


def test_verify_signature_rejects_stale_timestamp():
    body = b"{}"
    t = int(time.time()) - 10_000
    header = sign(SECRET, t, body)
    with pytest.raises(WebhookVerificationError):
        verify_signature(body, header, SECRET, tolerance_seconds=300)


def test_verify_signature_rejects_missing_secret():
    body = b"{}"
    header = sign(SECRET, int(time.time()), body)
    with pytest.raises(WebhookVerificationError):
        verify_signature(body, header, None)


def test_verify_signature_rejects_missing_header():
    with pytest.raises(WebhookVerificationError):
        verify_signature(b"{}", None, SECRET)


def test_verify_agent_id_case_insensitive():
    verify_agent_id("ABC-123", "abc-123")  # must not raise


def test_verify_agent_id_mismatch():
    with pytest.raises(WebhookVerificationError):
        verify_agent_id("abc-123", "def-456")


def test_classify_message():
    assert classify({"message": {"chat_id": "c1"}}) == "message"


def test_classify_card_interaction_explicit_type():
    assert classify({"type": "card_interaction", "card_id": "1", "action_id": "a"}) == "card_interaction"


def test_classify_card_interaction_shape_sniffed():
    assert classify({"card_id": "1", "action_id": "a", "chat_id": "c"}) == "card_interaction"


def test_classify_chat_opened():
    assert classify({"opened_by": {"id": "u1"}}) == "chat_opened"


def test_classify_handoff_confirmed():
    assert classify({"from_agent_id": "a1"}) == "handoff_confirmed"


def test_classify_handoff_received():
    assert classify({"to_agent_id": "a2"}) == "handoff_received"


def test_classify_invoice_paid():
    assert classify({"buyer": {"id": "u1"}, "line_items": []}) == "invoice_paid"


def test_classify_unknown():
    assert classify({"something": "else"}) == "unknown"


def test_handle_verifies_and_parses_event():
    body = {"message": {"chat_id": "c1", "message": "hi", "user": {"id": "u1"}}}
    raw = json.dumps(body).encode()
    t = int(time.time())
    headers = {
        SIGNATURE_HEADER: sign(SECRET, t, raw),
        AGENT_ID_HEADER: "agent-1",
        DELIVERY_ID_HEADER: "delivery-1",
    }
    event = handle(headers, raw, secret=SECRET)
    assert isinstance(event, Event)
    assert event.type == "message"
    assert event.agent_id == "agent-1"
    assert event.delivery_id == "delivery-1"
    assert event.body == body


def test_handle_case_insensitive_headers():
    body = {"message": {"chat_id": "c1"}}
    raw = json.dumps(body).encode()
    t = int(time.time())
    headers = {
        "x-salt-signature": sign(SECRET, t, raw),
        "x-salt-agent-id": "agent-1",
    }
    event = handle(headers, raw, secret=SECRET)
    assert event.agent_id == "agent-1"


def test_handle_rejects_bad_signature():
    body = {"message": {}}
    raw = json.dumps(body).encode()
    headers = {SIGNATURE_HEADER: "t=1,v1=deadbeef", AGENT_ID_HEADER: "agent-1"}
    with pytest.raises(WebhookVerificationError):
        handle(headers, raw, secret=SECRET)


def test_handle_verify_false_skips_check():
    body = {"message": {"chat_id": "c1"}}
    raw = json.dumps(body).encode()
    event = handle({}, raw, verify=False)
    assert event.type == "message"
