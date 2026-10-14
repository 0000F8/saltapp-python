# "Someone added you": list incoming contacts, accept one (the ordinary add-contact call) or dismiss it, on the sync,
# async and act_for clients. Mirrors salt-agent-sdk's tests/incoming-contacts.test.js.
from __future__ import annotations

import json

import httpx
import pytest

from saltapp import SaltApiError
from saltapp.client import AsyncSaltClient, SaltClient

HOST = "https://example.saltapp.test"
ROW = {
    "user": {"id": "u7", "username": "ada", "display_name": "Ada", "avatar_url": None, "account_type": "User", "nickname": "A"},
    "added_at": "2026-10-13T10:00:00Z",
}


def make_sync(handler) -> SaltClient:
    return SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(handler)))


def make_async(handler) -> AsyncSaltClient:
    return AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def recorder(status: int = 200, body=None):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(204) if status == 204 else httpx.Response(status, json=body if body is not None else {})

    return calls, handler


def test_list_incoming_contacts():
    calls, handler = recorder(200, [ROW])
    assert make_sync(handler).list_incoming_contacts("k") == [ROW]
    assert calls[0].method == "GET"
    assert str(calls[0].url) == f"{HOST}/api/v1/contacts/incoming"
    assert calls[0].headers["api-key"] == "k"


def test_accept_contact_uses_the_add_contact_path():
    calls, handler = recorder(201, {"id": "ct1"})
    make_sync(handler).accept_contact("k", "u7")
    assert calls[0].method == "POST"
    assert str(calls[0].url) == f"{HOST}/api/v1/contacts"
    assert json.loads(calls[0].content) == {"contact_id": "u7"}


def test_dismiss_incoming_contact_resolves_on_204():
    calls, handler = recorder(204)
    assert make_sync(handler).dismiss_incoming_contact("k", "u7") is None
    assert calls[0].method == "POST"
    assert str(calls[0].url) == f"{HOST}/api/v1/contacts/incoming/u7/dismiss"


def test_a_refusal_raises_salt_api_error():
    _, handler = recorder(422, {"error": "You can't add this account."})
    with pytest.raises(SaltApiError) as caught:
        make_sync(handler).accept_contact("k", "u7")
    assert caught.value.status == 422


def test_act_for_client_has_the_helpers():
    calls, handler = recorder(200, [])
    make_sync(handler).act_for("principal-1").list_incoming_contacts("k")
    assert calls[0].headers["x-salt-act-for"] == "principal-1"


@pytest.mark.asyncio
async def test_async_helpers_mirror_the_sync_ones():
    calls, handler = recorder(200, [ROW])
    client = make_async(handler)
    assert await client.list_incoming_contacts("k") == [ROW]
    await client.accept_contact("k", "u7")
    assert [(c.method, c.url.path) for c in calls] == [("GET", "/api/v1/contacts/incoming"), ("POST", "/api/v1/contacts")]
    assert json.loads(calls[1].content) == {"contact_id": "u7"}

    _, dismiss = recorder(204)
    assert await make_async(dismiss).dismiss_incoming_contact("k", "u7") is None
