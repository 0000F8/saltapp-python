# Get an account's posts by email (salt-api contract EMAIL_SUBSCRIBE_0242_CONTRACT.md): subscribe (always-202
# "check_your_email", or 201 "active" for the caller's own verified email), the owner's subscriber count, and the
# caller's own subscriptions. Mirrors salt-agent-sdk's tests/post-subscriptions.test.js.
from __future__ import annotations

import json

import httpx
import pytest

from saltapp import SaltApiError
from saltapp.client import AsyncSaltClient, SaltClient

HOST = "https://example.saltapp.test"


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


def test_subscribe_signed_out_is_202_check_your_email_with_no_api_key_header():
    calls, handler = recorder(202, {"status": "check_your_email"})
    r = make_sync(handler).subscribe_to_posts_by_email("dana", email="a@b.co", cadence="weekly", time_zone="Europe/London")
    assert r == {"status": "check_your_email"}
    c = calls[0]
    assert c.method == "POST" and str(c.url) == f"{HOST}/api/v1/users/dana/post_subscriptions"
    assert "api-key" not in c.headers
    assert json.loads(c.content) == {"email": "a@b.co", "cadence": "weekly", "time_zone": "Europe/London"}


def test_subscribe_omits_time_zone_and_escapes_username():
    calls, handler = recorder(202, {"status": "check_your_email"})
    make_sync(handler).subscribe_to_posts_by_email("a/b", email="a@b.co", cadence="daily")
    assert str(calls[0].url) == f"{HOST}/api/v1/users/a%2Fb/post_subscriptions"
    assert json.loads(calls[0].content) == {"email": "a@b.co", "cadence": "daily"}


def test_subscribe_signed_in_own_verified_email_is_201_active():
    calls, handler = recorder(201, {"status": "active", "cadence": "instant"})
    r = make_sync(handler).subscribe_to_posts_by_email("dana", email="me@x.co", cadence="instant", api_key="agent-key")
    assert r == {"status": "active", "cadence": "instant"}
    assert calls[0].headers["api-key"] == "agent-key"


def test_subscribe_422_raises_with_the_plain_sentence():
    _, handler = recorder(422, {"error": "Choose as they happen, daily or weekly."})
    with pytest.raises(SaltApiError) as exc:
        make_sync(handler).subscribe_to_posts_by_email("dana", email="a@b.co", cadence="hourly")
    assert exc.value.status == 422
    assert exc.value.body["error"] == "Choose as they happen, daily or weekly."


def test_post_subscriber_count_reads_counts():
    body = {"count": 12, "by_cadence": {"instant": 2, "daily": 4, "weekly": 6}}
    calls, handler = recorder(200, body)
    assert make_sync(handler).post_subscriber_count("k") == body
    assert calls[0].method == "GET" and str(calls[0].url) == f"{HOST}/api/v1/me/post_subscribers"
    assert calls[0].headers["api-key"] == "k"


def test_my_post_subscriptions_lists():
    rows = [{"id": "s1", "cadence": "weekly", "status": "active", "account": {"username": "dana", "display_name": "Dana"}}]
    calls, handler = recorder(200, rows)
    assert make_sync(handler).my_post_subscriptions("k") == rows
    assert str(calls[0].url) == f"{HOST}/api/v1/me/post_subscriptions"


def test_cancel_post_subscription_deletes_and_tolerates_204():
    calls, handler = recorder(204)
    assert make_sync(handler).cancel_post_subscription("k", "s1") is None
    assert calls[0].method == "DELETE" and str(calls[0].url) == f"{HOST}/api/v1/me/post_subscriptions/s1"


@pytest.mark.asyncio
async def test_async_methods_match_sync():
    calls, handler = recorder(202, {"status": "check_your_email"})
    c = make_async(handler)
    assert await c.subscribe_to_posts_by_email("dana", email="a@b.co", cadence="weekly") == {"status": "check_your_email"}
    assert "api-key" not in calls[0].headers

    calls, handler = recorder(201, {"status": "active", "cadence": "daily"})
    r = await make_async(handler).subscribe_to_posts_by_email("dana", email="me@x.co", cadence="daily", api_key="k")
    assert r["status"] == "active" and calls[0].headers["api-key"] == "k"

    calls, handler = recorder(200, {"count": 0, "by_cadence": {"instant": 0, "daily": 0, "weekly": 0}})
    assert (await make_async(handler).post_subscriber_count("k"))["count"] == 0

    calls, handler = recorder(200, [])
    assert await make_async(handler).my_post_subscriptions("k") == []

    calls, handler = recorder(204)
    assert await make_async(handler).cancel_post_subscription("k", "s1") is None
    assert calls[0].method == "DELETE"
