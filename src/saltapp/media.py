"""Live and short-video types and request bodies (salt-api /api/v1/live_sessions,
/stream_credentials, /media, /video_posts, /video/feed). Shapes follow the media
build contract; the methods live on `SaltClient` / `AsyncSaltClient`."""
from __future__ import annotations

from typing import Any, Literal, TypedDict

SourceType = Literal["webrtc", "rtmp"]
VideoFeedScope = Literal["following", "everyone", "room"]


class MediaPerson(TypedDict):
    id: str
    username: str
    display_name: str
    avatar_url: str | None
    account_type: str


class LiveRecording(TypedDict):
    media_id: str
    video_post_id: str
    status: Literal["processing", "draft", "published", "discarded"]


class LiveSession(TypedDict):
    id: str
    chat_id: str
    state: Literal["connecting", "live", "ending", "ended", "failed"]
    title: str | None
    source_type: SourceType
    record: bool
    host: MediaPerson
    started_at: str | None
    ended_at: str | None
    ended_reason: str | None
    viewer_count: int
    peak_viewers: int
    recording: LiveRecording | None
    can_end: bool


class StreamCredential(TypedDict):
    id: str
    chat_id: str
    label: str | None
    protocol: Literal["rtmps"]
    key_last4: str
    persistent: bool
    enabled: bool
    last_used_at: str | None
    created_at: str


class PublishBlock(TypedDict):
    """Returned once and never again. `stream_key` is a secret: never log it."""

    ingest_endpoint: str
    stream_key: str
    rtmps_url: str


class MediaAsset(TypedDict):
    id: str
    kind: str
    state: Literal["uploading", "processing", "ready", "failed", "removed"]
    playback: dict[str, Any] | None
    duration_ms: int | None
    width: int | None
    height: int | None
    failed_reason: str | None
    transcript: dict[str, Any] | None
    created_at: str


class VideoPost(TypedDict):
    id: str
    status: Literal["draft", "published", "hidden", "removed"]
    caption: str | None
    media: MediaAsset
    creator: MediaPerson
    chat_id: str
    room: dict[str, Any] | None
    also_in_chat_id: str | None
    message_id: str | None
    published_at: str | None
    seq: int | None
    counts: dict[str, int]
    can_edit: bool
    live_session_id: str | None


def _compact(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if v is not None}


def live_start_body(chat_id: str, source_type: str, title: str | None, record: bool | None) -> dict[str, Any]:
    return _compact({"chat_id": chat_id, "title": title, "source_type": source_type, "record": record})


def credential_body(chat_id: str, label: str | None, persistent: bool | None) -> dict[str, Any]:
    return _compact({"chat_id": chat_id, "label": label, "persistent": persistent})


def media_create_body(byte_size: int, content_type: str, filename: str | None) -> dict[str, Any]:
    return _compact({"kind": "video", "byte_size": byte_size, "content_type": content_type, "filename": filename})


def video_publish_body(media_id: str, caption: str | None, chat_id: str | None) -> dict[str, Any]:
    return _compact({"media_id": media_id, "caption": caption, "chat_id": chat_id})


def video_update_body(caption: str | None, status: str | None, also_in_chat_id: str | None) -> dict[str, Any]:
    return _compact({"caption": caption, "status": status, "also_in_chat_id": also_in_chat_id})


def feed_query(scope: str | None, chat_id: str | None, before_seq: int | None, limit: int | None) -> str:
    from urllib.parse import urlencode

    pairs = [(k, v) for k, v in (("scope", scope), ("chat_id", chat_id), ("before_seq", before_seq), ("limit", limit)) if v is not None]
    return f"?{urlencode(pairs)}" if pairs else ""
