"""Live and short-video methods against a mock server shaped like salt-api's media endpoints."""
from __future__ import annotations

import json

import httpx
import pytest

from saltapp.client import AsyncSaltClient, SaltClient
from saltapp.errors import SaltApiError

HOST = "https://example.saltapp.test"


def recorder(calls: list, respond):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith("application/json") else None
        calls.append({"method": request.method, "url": str(request.url), "key": request.headers.get("api-key"), "idem": request.headers.get("idempotency-key"), "body": body, "raw": request.content})
        return respond(request)

    return handler


def sync_client(calls, respond=lambda r: httpx.Response(200, json={})):
    return SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(recorder(calls, respond))))


def line(c):
    return f"{c['method']} {c['url'].replace(HOST, '')}"


def test_start_live_returns_publish_and_sends_idempotency_key():
    calls: list = []
    publish = {"ingest_endpoint": "x.live-video.net", "stream_key": "sk_secret", "rtmps_url": "rtmps://x.live-video.net:443/app/"}
    client = sync_client(calls, lambda r: httpx.Response(201, json={"live_session": {"id": "ls1"}, "publish": publish}))
    out = client.start_live("k", "c1", "rtmp", title="hi", record=True, idempotency_key="i1")
    assert out["publish"]["stream_key"] == "sk_secret"
    assert calls[0]["body"] == {"chat_id": "c1", "title": "hi", "source_type": "rtmp", "record": True}
    assert calls[0]["idem"] == "i1" and calls[0]["key"] == "k"


def test_start_live_raises_the_server_sentence():
    client = sync_client([], lambda r: httpx.Response(409, json={"error": "You are already live."}))
    with pytest.raises(SaltApiError) as e:
        client.start_live("k", "c1", "rtmp")
    assert e.value.status == 409 and str(e.value).endswith(": You are already live.")


def test_live_and_credential_paths():
    calls: list = []
    client = sync_client(calls, lambda r: httpx.Response(204) if r.method == "DELETE" else httpx.Response(200, json={}))
    client.get_live_session("k", "ls1")
    client.get_chat_live("k", "c1")
    client.end_live("k", "ls1")
    client.live_playback("k", "ls1")
    client.report_live("k", "ls1", "spam")
    client.list_stream_credentials("k")
    client.create_stream_credential("k", "c1", label="ffmpeg", persistent=True)
    client.update_stream_credential("k", "5", enabled=False)
    client.rotate_stream_credential("k", "5")
    client.delete_stream_credential("k", "5")
    assert [line(c) for c in calls] == [
        "GET /api/v1/live_sessions/ls1",
        "GET /api/v1/chats/c1/live",
        "POST /api/v1/live_sessions/ls1/end",
        "POST /api/v1/live_sessions/ls1/playback",
        "POST /api/v1/live_sessions/ls1/reports",
        "GET /api/v1/stream_credentials",
        "POST /api/v1/stream_credentials",
        "PATCH /api/v1/stream_credentials/5",
        "POST /api/v1/stream_credentials/5/rotate",
        "DELETE /api/v1/stream_credentials/5",
    ]
    assert calls[6]["body"] == {"chat_id": "c1", "label": "ffmpeg", "persistent": True}
    assert calls[7]["body"] == {"enabled": False}


def upload_server(fail_first_put=False):
    state = {"puts": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "s3.test":
            state["puts"] += 1
            if fail_first_put and state["puts"] == 1:
                return httpx.Response(500)
            return httpx.Response(200, headers={"ETag": f'"etag-{request.url.path[-1]}"'})
        if request.url.path == "/api/v1/media":
            return httpx.Response(201, json={"media": {"id": "m1", "state": "uploading"}, "upload": {"upload_id": "u1", "part_size": 4, "parts": [{"n": 1, "url": "https://s3.test/p1"}, {"n": 2, "url": "https://s3.test/p2"}]}})
        if request.url.path.endswith("/complete"):
            return httpx.Response(200, json={"media": {"id": "m1", "state": "processing"}})
        return httpx.Response(404, json={"error": "nope"})

    return respond


def test_upload_video_from_bytes_puts_each_part_then_completes():
    calls: list = []
    client = sync_client(calls, upload_server())
    media = client.upload_video("k", bytes([1, 2, 3, 4, 5, 6]), "video/mp4", filename="a.mp4")
    assert media["state"] == "processing"
    assert calls[0]["body"] == {"kind": "video", "byte_size": 6, "content_type": "video/mp4", "filename": "a.mp4"}
    assert calls[1]["raw"] == bytes([1, 2, 3, 4]) and calls[2]["raw"] == bytes([5, 6])
    assert calls[1]["key"] is None
    assert calls[3]["body"] == {"upload_id": "u1", "parts": [{"n": 1, "etag": '"etag-1"'}, {"n": 2, "etag": '"etag-2"'}]}


def test_upload_video_from_path_retries_a_failed_part(tmp_path, monkeypatch):
    monkeypatch.setattr("saltapp.client.time.sleep", lambda s: None)
    f = tmp_path / "clip.mp4"
    f.write_bytes(bytes([9, 8, 7, 6, 5]))
    calls: list = []
    client = sync_client(calls, upload_server(fail_first_put=True))
    assert client.upload_video("k", f, "video/mp4", retries=2)["id"] == "m1"
    assert len([c for c in calls if c["method"] == "PUT"]) == 3


def test_upload_video_gives_up_and_never_completes(monkeypatch):
    monkeypatch.setattr("saltapp.client.time.sleep", lambda s: None)
    calls: list = []

    def respond(request):
        if request.url.path == "/api/v1/media":
            return httpx.Response(201, json={"media": {"id": "m1"}, "upload": {"upload_id": "u1", "part_size": 4, "parts": [{"n": 1, "url": "https://s3.test/p1"}]}})
        return httpx.Response(500)

    with pytest.raises(Exception, match="upload -> 500"):
        sync_client(calls, respond).upload_video("k", b"x", "video/mp4", retries=2)
    assert not any(c["url"].endswith("/complete") for c in calls)


def test_media_and_video_paths():
    calls: list = []
    client = sync_client(calls, lambda r: httpx.Response(204) if r.method == "DELETE" else httpx.Response(200, json={}))
    client.get_media("k", "m1")
    client.media_playback("k", "m1")
    client.feed_playback("k")
    client.publish_video_post("k", "m1", caption="hi", chat_id="c1")
    client.get_video_post("k", "v1")
    client.my_video_posts("k", "draft")
    client.update_video_post("k", "v1", status="published", also_in_chat_id="c2")
    client.delete_video_post("k", "v1")
    client.report_video_post("k", "v1", "spam", "n")
    client.video_feed("k", scope="room", chat_id="c1", before_seq=40, limit=10)
    client.send_video_events("k", [{"post_id": "v1", "kind": "view", "at": "t"}])
    assert [line(c) for c in calls] == [
        "GET /api/v1/media/m1",
        "POST /api/v1/media/m1/playback",
        "POST /api/v1/media/playback",
        "POST /api/v1/video_posts",
        "GET /api/v1/video_posts/v1",
        "GET /api/v1/video_posts/mine?status=draft",
        "PATCH /api/v1/video_posts/v1",
        "DELETE /api/v1/video_posts/v1",
        "POST /api/v1/video_posts/v1/reports",
        "GET /api/v1/video/feed?scope=room&chat_id=c1&before_seq=40&limit=10",
        "POST /api/v1/video/events",
    ]
    assert calls[3]["body"] == {"media_id": "m1", "caption": "hi", "chat_id": "c1"}
    assert calls[6]["body"] == {"status": "published", "also_in_chat_id": "c2"}


@pytest.mark.asyncio
async def test_async_upload_video_and_feed():
    calls: list = []
    server = upload_server()

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/api/v1/video/feed":
            return httpx.Response(200, json={"items": [], "live": [], "next_before_seq": None})
        return server(request)

    client = AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    media = await client.upload_video("k", b"abcdef", "video/mp4")
    assert media["state"] == "processing"
    assert (await client.video_feed("k"))["items"] == []
    assert calls[-1] == f"{HOST}/api/v1/video/feed"
