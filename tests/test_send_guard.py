"""Send guard (KEY_RESET_DESIGN.md section 7): fingerprints ride every encrypted send; one retry on stale."""
import json

import httpx
import pytest

from saltapp import crypto
from saltapp.client import AsyncSaltClient, SaltClient
from saltapp.errors import SaltApiError
from saltapp.identity import Identity

HOST = "https://salt.test"
IDENTITY = Identity(api_key="key", public_key="self-pk", private_key="priv", agent_id="self-id")
MEMBERS = [
    {"id": "self-id", "public_key": "self-pk", "public_fingerprint": "aa"},
    {"id": "u2", "public_key": "pk2", "public_fingerprint": "bb22"},
]
STALE = {"error": "stale", "code": "stale_recipient_keys", "stale": ["u2"],
         "members": [{"id": "u2", "public_key": "pk2-new", "public_fingerprint": "cc33"}]}


@pytest.fixture(autouse=True)
def fake_crypto(monkeypatch):
    monkeypatch.setattr(crypto, "encrypt_for", lambda text, keys: f"ENC[{','.join(keys)}]")
    monkeypatch.setattr(crypto, "fingerprint_of", lambda key: "dd44")


def _handler(posts, refusals):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            posts.append(json.loads(request.content))
            if len(posts) <= refusals:
                return httpx.Response(409, json=STALE)
            return httpx.Response(200, json={"id": "m1"})
        return httpx.Response(200, json={"session": {"users": MEMBERS}})
    return handler


def _sync(handler):
    return SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(handler)))


def _async(handler):
    return AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_send_message_sends_fingerprints():
    posts = []
    _sync(_handler(posts, 0)).send_message(IDENTITY, "c1", "hi")
    assert len(posts) == 1
    assert posts[0]["recipient_fingerprints"] == {"u2": "BB22"}
    assert posts[0]["sender_fingerprint"] == "DD44"


def test_send_message_retries_once_on_stale():
    posts = []
    _sync(_handler(posts, 1)).send_message(IDENTITY, "c1", "hi")
    assert len(posts) == 2
    assert posts[1]["message"] == "ENC[pk2-new]"
    assert posts[1]["recipient_fingerprints"] == {"u2": "CC33"}


def test_send_message_does_not_loop_on_second_stale():
    posts = []
    with pytest.raises(SaltApiError):
        _sync(_handler(posts, 5)).send_message(IDENTITY, "c1", "hi")
    assert len(posts) == 2


def test_other_409_is_not_retried():
    posts = []

    def handler(request):
        if request.url.path.endswith("/messages"):
            posts.append(1)
            return httpx.Response(409, json={"code": "pickup_required"})
        return httpx.Response(200, json={"session": {"users": MEMBERS}})

    with pytest.raises(SaltApiError):
        _sync(handler).send_message(IDENTITY, "c1", "hi")
    assert len(posts) == 1


def test_member_without_fingerprint_omits_guard():
    posts = []

    def handler(request):
        if request.url.path.endswith("/messages"):
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "m1"})
        return httpx.Response(200, json={"session": {"users": [{"id": "u2", "public_key": "pk2"}]}})

    _sync(handler).send_message(IDENTITY, "c1", "hi")
    assert "recipient_fingerprints" not in posts[0] and "sender_fingerprint" not in posts[0]


async def test_async_send_message_guard_and_retry():
    posts = []
    await _async(_handler(posts, 1)).send_message(IDENTITY, "c1", "hi")
    assert len(posts) == 2
    assert posts[0]["recipient_fingerprints"] == {"u2": "BB22"}
    assert posts[1]["recipient_fingerprints"] == {"u2": "CC33"}


async def test_async_does_not_loop():
    posts = []
    with pytest.raises(SaltApiError):
        await _async(_handler(posts, 5)).send_message(IDENTITY, "c1", "hi")
    assert len(posts) == 2


def test_classify_member_key_changed():
    from saltapp.webhook import classify
    assert classify({"type": "member_key_changed", "user_id": "u2"}) == "member_key_changed"
