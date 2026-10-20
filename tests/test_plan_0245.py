# Salt 0.245.0: give_goal, get_goal_request, put_working, decide_working, GoalPlanError.
# Mirrors salt-agent-sdk tests/plan-0245.test.js.
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from saltapp import GoalPlanError, SaltApiError
from saltapp.client import AsyncSaltClient, SaltClient

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


def test_give_goal_posts_the_goal_optional_fields_and_the_idempotency_key():
    calls, handler = recorder(httpx.Response(201, json={"id": "g1", "status": "waiting", "next": "Salt is making a plan."}))
    c = sync_client(handler)
    r = c.give_goal("k", "Sell 10 prints", deadline="2026-12-31", chat_id="c1", idempotency_key="idem-1")
    assert r["status"] == "waiting"
    assert calls[0].method == "POST" and calls[0].url.path == "/api/v1/goals"
    assert json.loads(calls[0].content) == {"goal": "Sell 10 prints", "deadline": "2026-12-31", "chat_id": "c1"}
    assert calls[0].headers["Idempotency-Key"] == "idem-1"
    c.give_goal("k", "Just this")
    assert json.loads(calls[1].content) == {"goal": "Just this"}
    assert "Idempotency-Key" not in calls[1].headers


def test_give_goal_limits_are_typed_errors_with_retry_after():
    _, handler = recorder(httpx.Response(429, json={"error": "You have given 20 goals today. Try again later.", "code": "goals_per_day"}, headers={"Retry-After": "60"}))
    with pytest.raises(SaltApiError) as exc:
        sync_client(handler).give_goal("k", "x")
    assert exc.value.status == 429 and exc.value.code == "goals_per_day" and exc.value.retry_after == 60


def test_get_goal_request_reads_one_request():
    body = {"id": "g1", "status": "planned", "goal": "Sell", "deadline": None, "mandate_id": "m9", "created_at": "2026-10-20T10:00:00Z"}
    calls, handler = recorder(httpx.Response(200, json=body))
    assert sync_client(handler).get_goal_request("k", "g1")["mandate_id"] == "m9"
    assert calls[0].method == "GET" and calls[0].url.path == "/api/v1/goals/g1"


def test_get_goal_request_by_another_person_is_a_404():
    _, handler = recorder(httpx.Response(404, json={"error": "Not found."}))
    with pytest.raises(SaltApiError) as exc:
        sync_client(handler).get_goal_request("k", "g1")
    assert exc.value.status == 404


def test_put_working_sends_the_three_lists_filling_the_missing_ones():
    calls, handler = recorder(httpx.Response(200, json={"mandate_id": "m1", "working": {}, "working_at": "2026-10-20T10:00:00Z"}))
    testing = [{"id": "t1", "text": "Post at 9", "since": "2026-10-19", "trials": 3}]
    sync_client(handler).put_working("k", "m1", testing=testing)
    assert calls[0].method == "PUT" and calls[0].url.path == "/api/v1/mandates/m1/working"
    assert json.loads(calls[0].content) == {"testing": testing, "kept": [], "dropped": []}


def test_put_working_by_anyone_but_the_agent_is_a_403_with_the_code():
    _, handler = recorder(httpx.Response(403, json={"error": "Only the agent running this plan publishes what's working.", "code": "working_grantee_only"}))
    with pytest.raises(SaltApiError) as exc:
        sync_client(handler).put_working("k", "m1")
    assert exc.value.status == 403 and exc.value.code == "working_grantee_only"


def test_decide_working_sends_the_entry_id_in_the_body():
    calls, handler = recorder(httpx.Response(200, json={"mandate_id": "m1", "id": "k1", "decision": "keep", "working": {}}))
    assert sync_client(handler).decide_working("k", "m1", "k1", "keep")["decision"] == "keep"
    assert calls[0].method == "POST" and calls[0].url.path == "/api/v1/mandates/m1/working/decide"
    assert json.loads(calls[0].content) == {"id": "k1", "decision": "keep"}


def test_decide_working_on_a_missing_entry_is_422_entry_not_found():
    _, handler = recorder(httpx.Response(422, json={"error": "That entry isn't in what's working.", "code": "entry_not_found"}))
    with pytest.raises(SaltApiError) as exc:
        sync_client(handler).decide_working("k", "m1", "zz", "never")
    assert exc.value.status == 422 and exc.value.code == "entry_not_found"


@pytest.mark.parametrize(
    "code,error,nxt",
    [
        ("no_capability", "This plan can't post or message: you approved it yourself, so it holds no permissions.", "To have Salt run it, give Salt the goal: POST /api/v1/goals."),
        ("first_post_needs_post", "A first post needs permission to post on their profile in the plan.", None),
        ("source_unavailable", "They have no wallet to receive payments, so Salt can't count payments for this plan. Use a count they report.", None),
    ],
)
def test_plan_refusals_raise_goal_plan_error_with_sentence_and_next(code, error, nxt):
    body = {"error": error, "code": code, **({"next": nxt} if nxt else {})}
    _, handler = recorder(httpx.Response(422, json=body))
    with pytest.raises(GoalPlanError) as exc:
        sync_client(handler).get_plan_summary("k", "m1")
    assert isinstance(exc.value, SaltApiError)
    assert exc.value.code == code and exc.value.sentence == error and exc.value.next == nxt


def test_async_calls_match():
    calls, handler = recorder(httpx.Response(200, json={"id": "g1", "status": "waiting"}))

    async def run():
        c = async_client(handler)
        await c.give_goal("k", "x", idempotency_key="i")
        await c.get_goal_request("k", "g1")
        await c.put_working("k", "m1", kept=[{"id": "k1", "text": "t", "kept_at": "2026-10-20"}])
        await c.decide_working("k", "m1", "k1", "never")

    asyncio.run(run())
    assert [(r.method, r.url.path) for r in calls] == [
        ("POST", "/api/v1/goals"),
        ("GET", "/api/v1/goals/g1"),
        ("PUT", "/api/v1/mandates/m1/working"),
        ("POST", "/api/v1/mandates/m1/working/decide"),
    ]
    assert calls[0].headers["Idempotency-Key"] == "i"
