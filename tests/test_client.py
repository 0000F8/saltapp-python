from __future__ import annotations

import json

import httpx
import pytest

from saltapp.client import AsyncSaltClient, SaltClient
from saltapp.errors import SaltApiError
from saltapp.identity import Identity

HOST = "https://example.saltapp.test"


def make_sync_client(handler) -> SaltClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return SaltClient(HOST, http=http)


def make_async_client(handler) -> AsyncSaltClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return AsyncSaltClient(HOST, http=http)


def json_response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body)


def test_post_message_request_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return json_response(200, {"id": "m1"})

    client = make_sync_client(handler)
    result = client.post_message("key123", "chat1", "ciphertext", "sender-copy", mentions=["u1"], quiet=True)

    assert result == {"id": "m1"}
    assert captured["method"] == "POST"
    assert captured["url"] == f"{HOST}/api/v1/messages"
    assert captured["headers"]["api-key"] == "key123"
    assert captured["body"] == {
        "chat_id": "chat1",
        "message": "ciphertext",
        "sender_message": "sender-copy",
        "mentions": ["u1"],
        "quiet": True,
    }


def test_request_payment_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        return json_response(200, {"id": "tr1"})

    client = make_sync_client(handler)
    client.request_payment(
        "key", chat_id="c1", receiver_id="u2", wallet_id="w1", amount="1.50", message="lunch"
    )
    assert captured["url"] == f"{HOST}/api/v1/transfer_requests"
    assert captured["body"] == {
        "chat_id": "c1", "receiver_id": "u2", "wallet_id": "w1", "amount": "1.50", "message": "lunch",
    }
    assert "request_type" not in captured["body"]


def test_create_invoice_shape_and_idempotency_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return json_response(200, {"id": "inv1"})

    client = make_sync_client(handler)
    client.create_invoice(
        "key", chat_id="c1", receiver_id="u2", wallet_id="w1", amount="9.00",
        line_items=[{"name": "Coffee", "qty": 2, "unit_price": "4.50", "subtotal": "9.00"}],
        idempotency_key="idem-1",
    )
    assert captured["body"]["request_type"] == "invoice"
    assert captured["body"]["line_items"][0]["name"] == "Coffee"
    assert captured["headers"]["idempotency-key"] == "idem-1"


def test_post_card_and_update_card_shapes():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), json.loads(request.content)))
        return json_response(200, {"id": "card1"})

    client = make_sync_client(handler)
    client.post_card("key", "c1", [{"type": "divider"}], "hello")
    client.update_card("key", "card1", [{"type": "divider"}])

    assert calls[0] == ("POST", f"{HOST}/api/v1/cards", {"chat_id": "c1", "blocks": [{"type": "divider"}], "text": "hello"})
    assert calls[1] == ("PATCH", f"{HOST}/api/v1/cards/card1", {"blocks": [{"type": "divider"}]})


def test_error_response_raises_salt_api_error_with_server_sentence():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(422, {"error": "You can't request funds from yourself."})

    client = make_sync_client(handler)
    with pytest.raises(SaltApiError) as excinfo:
        client.request_payment("key", chat_id="c1", receiver_id="u1", wallet_id="w1", amount="1")
    assert "You can't request funds from yourself." in str(excinfo.value)
    assert excinfo.value.status == 422


def test_get_chat_members_reads_session_users():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, {"session": {"users": [{"id": "u1", "public_key": "pk1"}]}})

    client = make_sync_client(handler)
    members = client.get_chat_members("key", "c1")
    assert members == [{"id": "u1", "public_key": "pk1"}]


def test_send_message_encrypts_for_every_member_but_self(monkeypatch):
    from saltapp import crypto

    calls = {}

    def fake_encrypt_for(text, keys):
        calls.setdefault("encrypt_for", []).append((text, list(keys)))
        return f"CIPHERTEXT[{text}]"

    monkeypatch.setattr(crypto, "encrypt_for", fake_encrypt_for)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            calls["post_body"] = json.loads(request.content)
            return json_response(200, {"id": "m1"})
        return json_response(200, {
            "session": {"users": [
                {"id": "self-id", "public_key": "self-pk"},
                {"id": "u2", "public_key": "pk2"},
                {"id": "u3", "public_key": "pk3"},
            ]}
        })

    client = make_sync_client(handler)
    identity = Identity(api_key="key", public_key="self-pk", private_key="priv", agent_id="self-id")
    client.send_message(identity, "c1", "hello everyone")

    encrypt_calls = calls["encrypt_for"]
    # one call encrypting for every member but self, one call for self's own copy
    assert (encrypt_calls[0][0], sorted(encrypt_calls[0][1])) == ("hello everyone", ["pk2", "pk3"])
    assert encrypt_calls[1] == ("hello everyone", ["self-pk"])
    assert calls["post_body"]["message"] == "CIPHERTEXT[hello everyone]"
    assert calls["post_body"]["sender_message"] == "CIPHERTEXT[hello everyone]"


def test_send_message_raises_when_no_recipients():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, {"session": {"users": [{"id": "self-id", "public_key": "self-pk"}]}})

    client = make_sync_client(handler)
    identity = Identity(api_key="key", public_key="self-pk", private_key="priv", agent_id="self-id")
    with pytest.raises(SaltApiError):
        client.send_message(identity, "c1", "hello?")


@pytest.mark.asyncio
async def test_async_client_mirrors_sync_request_shapes():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return json_response(200, {"id": "m1"})

    client = make_async_client(handler)
    result = await client.post_message("key", "c1", "ct")
    assert result == {"id": "m1"}
    assert captured["url"] == f"{HOST}/api/v1/messages"
    assert captured["body"] == {"chat_id": "c1", "message": "ct"}
    await client.aclose()


@pytest.mark.asyncio
async def test_async_client_error_raises_salt_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(403, {"error": "You can't send a payment request to this user."})

    client = make_async_client(handler)
    with pytest.raises(SaltApiError):
        await client.request_payment("key", chat_id="c1", receiver_id="u1", wallet_id="w1", amount="1")
    await client.aclose()


def test_post_plain_message_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return json_response(200, {"id": "m1"})

    client = make_sync_client(handler)
    client.post_plain_message("key", "chat1", "hello room", mentions=["u1"], reply_to_message_id="m0")
    assert captured["body"] == {
        "chat_id": "chat1", "message": "hello room", "encrypted": False,
        "mentions": ["u1"], "reply_to_message_id": "m0",
    }


def test_post_plain_message_refused_on_encrypted_chat_surfaces_the_sentence():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(422, {"error": "This room is encrypted. Messages must be sent encrypted."})

    client = make_sync_client(handler)
    with pytest.raises(SaltApiError) as excinfo:
        client.post_plain_message("key", "chat1", "hello")
    assert "This room is encrypted. Messages must be sent encrypted." in str(excinfo.value)
    assert excinfo.value.status == 422


def test_get_chat_with_no_api_key_sends_no_api_key_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return json_response(200, {"session": {"id": "c1", "public": True, "encrypted": False, "member": False}})

    client = make_sync_client(handler)
    result = client.get_chat("", "c1")
    assert "api-key" not in captured["headers"]
    assert result["session"]["member"] is False


def test_get_chat_last_param():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return json_response(200, {"session": {}, "messages": []})

    client = make_sync_client(handler)
    client.get_chat("key", "c1", last=42)
    assert "last=42" in captured["url"]


def test_chat_subscription_get_set_clear():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), json.loads(request.content) if request.content else None))
        return json_response(200, {"chat_id": "c1", "mode": "keywords", "keywords": ["salt"]})

    client = make_sync_client(handler)
    client.get_chat_subscription("key", "c1")
    client.set_chat_subscription("key", "c1", "keywords", keywords=["salt", "agents"])
    client.clear_chat_subscription("key", "c1")

    assert calls[0] == ("GET", f"{HOST}/api/v1/chats/c1/subscription", None)
    assert calls[1] == ("PUT", f"{HOST}/api/v1/chats/c1/subscription", {"mode": "keywords", "keywords": ["salt", "agents"]})
    assert calls[2] == ("DELETE", f"{HOST}/api/v1/chats/c1/subscription", None)


@pytest.mark.asyncio
async def test_async_post_plain_message_and_anonymous_get_chat():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            captured["post_body"] = json.loads(request.content)
            captured["post_headers"] = dict(request.headers)
            return json_response(200, {"id": "m1"})
        captured["get_headers"] = dict(request.headers)
        return json_response(200, {"session": {"public": True, "encrypted": False, "member": False}})

    client = make_async_client(handler)
    await client.post_plain_message("agent-key", "c1", "hi everyone")
    await client.get_chat(None, "c1")
    await client.aclose()

    assert captured["post_body"] == {"chat_id": "c1", "message": "hi everyone", "encrypted": False}
    assert captured["post_headers"]["api-key"] == "agent-key"
    assert "api-key" not in captured["get_headers"]


def test_get_agent_updates_query_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return json_response(200, {"updates": [], "cursor": 0})

    client = make_sync_client(handler)
    client.get_agent_updates("key", after=5, timeout=1, limit=10)
    assert "after=5" in captured["url"]
    assert "timeout=1" in captured["url"]
    assert "limit=10" in captured["url"]
