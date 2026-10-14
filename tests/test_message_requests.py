# Message requests (salt-api contract MESSAGE_REQUESTS_CONTRACT.md): delivered_as passes through chat-create and
# message-send, the recipient's setting and the three request endpoints are wrapped on both clients, and the
# contacts-only refusal is a typed error. Mirrors salt-agent-sdk's tests/message-requests.test.js.
from __future__ import annotations

import json

import httpx
import pytest

from saltapp import RecipientContactsOnlyError, SaltApiError
from saltapp.client import AsyncSaltClient, SaltClient

HOST = "https://example.saltapp.test"
SENTENCE = "Ada only takes messages from their contacts."


def make_sync(handler) -> SaltClient:
    return SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(handler)))


def make_async(handler) -> AsyncSaltClient:
    return AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def recorder(status: int = 200, body=None):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status == 204:
            return httpx.Response(204)
        return httpx.Response(status, json=body if body is not None else {})

    return calls, handler


def test_create_or_get_chat_surfaces_delivered_as_request():
    calls, handler = recorder(201, {"id": "c1", "delivered_as": "request"})
    chat = make_sync(handler).create_or_get_chat("k", "u9")
    assert chat["delivered_as"] == "request"
    assert str(calls[0].url) == f"{HOST}/api/v1/chats"


def test_post_message_surfaces_delivered_as():
    _, handler = recorder(201, {"id": "m1", "delivered_as": "request"})
    assert make_sync(handler).post_message("k", "c1", "armor", "armor-sender")["delivered_as"] == "request"
    _, handler = recorder(201, {"id": "m2", "delivered_as": "chat"})
    assert make_sync(handler).post_message("k", "c1", "armor")["delivered_as"] == "chat"


def test_get_message_from_reads_the_account():
    calls, handler = recorder(200, {"id": "a1", "message_from": "everyone"})
    assert make_sync(handler).get_message_from("agent-key") == "everyone"
    assert calls[0].method == "GET"
    assert str(calls[0].url) == f"{HOST}/api/v1/account"
    assert calls[0].headers["api-key"] == "agent-key"


def test_set_message_from_sends_the_value():
    calls, handler = recorder(200, {"id": "a1", "message_from": "contacts"})
    result = make_sync(handler).set_message_from("agent-key", "contacts")
    assert calls[0].method == "POST"
    assert str(calls[0].url) == f"{HOST}/api/v1/account"
    assert json.loads(calls[0].content) == {"message_from": "contacts"}
    assert result["message_from"] == "contacts"


def test_set_message_from_invalid_value_raises_with_the_code():
    _, handler = recorder(422, {"error": "Pick everyone, known or contacts.", "code": "invalid_message_from"})
    with pytest.raises(SaltApiError) as caught:
        make_sync(handler).set_message_from("k", "nobody")
    assert not isinstance(caught.value, RecipientContactsOnlyError)
    assert caught.value.status == 422
    assert caught.value.code == "invalid_message_from"


def test_list_requests_asks_for_requests():
    rows = [{"session": {"id": "c5", "request_state": "pending"}}]
    calls, handler = recorder(200, rows)
    assert make_sync(handler).list_requests("k") == rows
    assert calls[0].method == "GET"
    assert str(calls[0].url) == f"{HOST}/api/v1/chats?requests=1"


def test_accept_request_posts_and_returns_the_chat():
    calls, handler = recorder(200, {"id": "c5"})
    assert make_sync(handler).accept_request("k", "c5")["id"] == "c5"
    assert calls[0].method == "POST"
    assert str(calls[0].url) == f"{HOST}/api/v1/chats/c5/accept_request"


def test_decline_request_posts_and_resolves_on_204():
    calls, handler = recorder(204)
    assert make_sync(handler).decline_request("k", "c5") is None
    assert calls[0].method == "POST"
    assert str(calls[0].url) == f"{HOST}/api/v1/chats/c5/decline_request"


def test_recipient_contacts_only_is_typed_with_code_and_sentence():
    _, handler = recorder(403, {"error": SENTENCE, "code": "recipient_contacts_only"})
    with pytest.raises(RecipientContactsOnlyError) as caught:
        make_sync(handler).create_or_get_chat("k", "u9")
    err = caught.value
    assert isinstance(err, SaltApiError)
    assert err.status == 403
    assert err.code == "recipient_contacts_only"
    assert err.sentence == SENTENCE
    assert str(err).endswith(SENTENCE)

    with pytest.raises(RecipientContactsOnlyError):
        make_sync(handler).post_message("k", "c1", "x")


def test_other_403s_stay_plain_salt_api_error():
    _, handler = recorder(403, {"error": "You can't message this person."})
    with pytest.raises(SaltApiError) as caught:
        make_sync(handler).create_or_get_chat("k", "u9")
    assert not isinstance(caught.value, RecipientContactsOnlyError)
    assert caught.value.code is None


def test_act_for_client_has_the_request_helpers():
    calls, handler = recorder(200, {"id": "c5"})
    make_sync(handler).act_for("principal-1").accept_request("k", "c5")
    assert calls[0].headers["x-salt-act-for"] == "principal-1"


# --- async mirror -------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_helpers_mirror_the_sync_ones():
    calls, handler = recorder(200, {"id": "c5", "message_from": "known"})
    client = make_async(handler)
    assert await client.get_message_from("k") == "known"
    await client.set_message_from("k", "known")
    await client.list_requests("k")
    await client.accept_request("k", "c5")
    assert [(c.method, c.url.path, c.url.query.decode()) for c in calls] == [
        ("GET", "/api/v1/account", ""),
        ("POST", "/api/v1/account", ""),
        ("GET", "/api/v1/chats", "requests=1"),
        ("POST", "/api/v1/chats/c5/accept_request", ""),
    ]
    assert json.loads(calls[1].content) == {"message_from": "known"}

    _, decline = recorder(204)
    assert await make_async(decline).decline_request("k", "c5") is None


@pytest.mark.asyncio
async def test_async_contacts_only_is_typed():
    _, handler = recorder(403, {"error": SENTENCE, "code": "recipient_contacts_only"})
    with pytest.raises(RecipientContactsOnlyError) as caught:
        await make_async(handler).create_or_get_chat("k", "u9")
    assert caught.value.sentence == SENTENCE
