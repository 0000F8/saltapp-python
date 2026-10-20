# Salt 0.244.0: get_readiness, get_plan_summary, pause_all, resume_all, accept_mandate with first_post_words.
# Mirrors salt-agent-sdk tests/plan-0244.test.js.
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from saltapp import SaltApiError
from saltapp.client import PLAN_EVENT_TYPES, AsyncSaltClient, SaltClient

HOST = "https://example.saltapp.test"


def sync_client(handler) -> SaltClient:
    return SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(handler)))


def async_client(handler) -> AsyncSaltClient:
    return AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def recorder(response):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(request) if callable(response) else response

    return calls, handler


def test_get_readiness_reads_the_caller_a_person_or_a_plans_grantor():
    body = {"person_id": "p1", "wallets": 1, "shop": False, "connections": [{"service": "notion", "name": "Notion"}], "followers": 3, "contacts": 2, "email_subscribers": 0}
    calls, handler = recorder(httpx.Response(200, json=body))
    c = sync_client(handler)
    assert c.get_readiness("k")["connections"] == [{"service": "notion", "name": "Notion"}]
    c.get_readiness("k", person_id="p1")
    c.get_readiness("k", plan_id="m1")
    assert [r.method for r in calls] == ["GET"] * 3
    assert calls[0].url.path == "/api/v1/me/readiness" and not calls[0].url.query
    assert calls[1].url.params["person_id"] == "p1"
    assert calls[2].url.params["plan_id"] == "m1"


def test_get_readiness_404_is_a_typed_error():
    _, handler = recorder(httpx.Response(404, json={"error": "Not found."}))
    with pytest.raises(SaltApiError) as exc:
        sync_client(handler).get_readiness("k", person_id="x")
    assert exc.value.status == 404


def test_get_plan_summary_gets_the_summary():
    body = {"mandate_id": "m1", "status": "active", "first_check_in_at": "2026-10-21T10:00:00Z", "targets": [{"metric": "followers", "actual": None, "measured_by": "agent"}]}
    calls, handler = recorder(httpx.Response(200, json=body))
    s = sync_client(handler).get_plan_summary("k", "m1")
    assert s["first_check_in_at"] == "2026-10-21T10:00:00Z"
    assert s["targets"][0]["measured_by"] == "agent"
    assert calls[0].method == "GET" and calls[0].url.path == "/api/v1/mandates/m1/summary"


def test_pause_all_and_resume_all_send_goal_id_only_when_given():
    def response(request):
        if request.url.path.endswith("pause_all"):
            return httpx.Response(200, json={"status": "paused", "paused": ["m1"], "already_paused": [], "count": 1})
        return httpx.Response(200, json={"status": "active", "resumed": ["m1"], "still_paused": []})

    calls, handler = recorder(response)
    c = sync_client(handler)
    assert c.pause_all("k")["count"] == 1
    c.pause_all("k", goal_id="m1")
    assert c.resume_all("k", goal_id="m1")["resumed"] == ["m1"]
    assert [r.method for r in calls] == ["POST"] * 3
    assert calls[0].url.path == "/api/v1/mandates/pause_all" and not calls[0].content
    assert json.loads(calls[1].content) == {"goal_id": "m1"}
    assert calls[2].url.path == "/api/v1/mandates/resume_all"
    assert json.loads(calls[2].content) == {"goal_id": "m1"}


def test_resume_all_by_a_grantee_surfaces_resume_grantor_only():
    _, handler = recorder(httpx.Response(403, json={"code": "resume_grantor_only", "error": "Tap Resume on the plan to start it again."}))
    with pytest.raises(SaltApiError) as exc:
        sync_client(handler).resume_all("k")
    assert exc.value.status == 403
    assert exc.value.code == "resume_grantor_only"
    assert "Tap Resume" in str(exc.value)


def test_accept_mandate_sends_version_and_first_post_words_and_none_by_default():
    calls, handler = recorder(httpx.Response(200, json={"id": "m1", "first_post_result": {"exercise_id": 7, "message_id": "x", "chat_id": "c"}}))
    c = sync_client(handler)
    c.accept_mandate("k", "m1")
    out = c.accept_mandate("k", "m1", version=2, first_post_words="Hello")
    assert not calls[0].content
    assert json.loads(calls[1].content) == {"version": 2, "first_post_words": "Hello"}
    assert calls[1].url.path == "/api/v1/mandates/m1/accept"
    assert out["first_post_result"]["chat_id"] == "c"


def test_words_required_is_a_typed_422():
    _, handler = recorder(httpx.Response(422, json={"code": "words_required", "error": "Open the card to see the words first."}))
    with pytest.raises(SaltApiError) as exc:
        sync_client(handler).accept_mandate("k", "m1")
    assert exc.value.status == 422 and exc.value.code == "words_required"


def test_async_client_mirrors_the_new_calls():
    calls, handler = recorder(lambda r: httpx.Response(200, json={"ok": True}))

    async def run():
        c = async_client(handler)
        await c.get_readiness("k", plan_id="m1")
        await c.get_plan_summary("k", "m1")
        await c.pause_all("k", goal_id="g")
        await c.resume_all("k")
        await c.accept_mandate("k", "m1", first_post_words="Hi")

    asyncio.run(run())
    assert [(r.method, r.url.path) for r in calls] == [
        ("GET", "/api/v1/me/readiness"),
        ("GET", "/api/v1/mandates/m1/summary"),
        ("POST", "/api/v1/mandates/pause_all"),
        ("POST", "/api/v1/mandates/resume_all"),
        ("POST", "/api/v1/mandates/m1/accept"),
    ]
    assert calls[0].url.params["plan_id"] == "m1"
    assert json.loads(calls[2].content) == {"goal_id": "g"}
    assert json.loads(calls[4].content) == {"first_post_words": "Hi"}


def test_plan_event_types():
    assert PLAN_EVENT_TYPES == ("plan_filed", "plan_allowed", "plan_acted", "plan_paused", "plan_resumed", "plan_stopped", "plan_tuned")
