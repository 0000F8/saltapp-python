# The owner's agent face (salt-api PUT /api/v1/agents/:id/admin_update, partial): photo, drawing, remove.
# Mirrors salt-agent-sdk's tests/agent-face.test.js.
from __future__ import annotations

import base64
import json

import httpx
import pytest

from saltapp import SaltApiError
from saltapp.client import AsyncSaltClient, SaltClient

HOST = "https://example.saltapp.test"
FACE = {"avatar_url": "https://example.saltapp.test/a.png", "face": "photo", "face_set_by": "owner"}
URL = f"{HOST}/api/v1/agents/a1/admin_update"


def make_sync(handler) -> SaltClient:
    return SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(handler)))


def make_async(handler) -> AsyncSaltClient:
    return AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def recorder(status: int = 200, body=None):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=body if body is not None else {"id": "a1", **FACE})

    return calls, handler


def test_set_agent_photo_sends_base64_and_content_type_as_a_partial_update():
    calls, handler = recorder()
    photo = bytes([137, 80, 78, 71])
    result = make_sync(handler).set_agent_photo("k", "a1", photo, "image/png")
    assert calls[0].method == "PUT"
    assert str(calls[0].url) == URL
    assert calls[0].headers["api-key"] == "k"
    assert json.loads(calls[0].content) == {
        "partial": True,
        "avatar": base64.b64encode(photo).decode(),
        "avatar_content_type": "image/png",
    }
    assert result["face"] == "photo"


def test_set_agent_face_sends_only_partial_and_face():
    calls, handler = recorder()
    make_sync(handler).set_agent_face("k", "a1", "drawing")
    assert calls[0].method == "PUT"
    assert json.loads(calls[0].content) == {"partial": True, "face": "drawing"}


def test_remove_agent_photo_sends_remove_avatar():
    calls, handler = recorder(200, {"id": "a1", "avatar_url": None, "face": None, "face_set_by": None})
    result = make_sync(handler).remove_agent_photo("k", "a1")
    assert str(calls[0].url) == URL
    assert json.loads(calls[0].content) == {"partial": True, "remove_avatar": True}
    assert result["avatar_url"] is None


def test_a_refusal_raises_with_the_sentence():
    _, handler = recorder(422, {"error": "Upload a photo first."})
    with pytest.raises(SaltApiError) as caught:
        make_sync(handler).set_agent_face("k", "a1", "photo")
    assert caught.value.status == 422
    assert str(caught.value).endswith("Upload a photo first.")


def test_act_for_client_has_the_face_helpers():
    calls, handler = recorder()
    make_sync(handler).act_for("principal-1").remove_agent_photo("k", "a1")
    assert calls[0].headers["x-salt-act-for"] == "principal-1"


@pytest.mark.asyncio
async def test_async_helpers_mirror_the_sync_ones():
    calls, handler = recorder()
    client = make_async(handler)
    await client.set_agent_photo("k", "a1", b"abc", "image/jpeg")
    await client.set_agent_face("k", "a1", "photo")
    await client.remove_agent_photo("k", "a1")
    assert [c.method for c in calls] == ["PUT", "PUT", "PUT"]
    assert json.loads(calls[0].content)["avatar"] == base64.b64encode(b"abc").decode()
    assert json.loads(calls[1].content) == {"partial": True, "face": "photo"}
    assert json.loads(calls[2].content) == {"partial": True, "remove_avatar": True}
