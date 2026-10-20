# Let @salt run your plan (Salt 0.243.0): tune_plan, lock_plan, undo_tuning, report_count, request_ask as_="grantor",
# read_plan_report, and strategy_tuned's by / before / tuning_id. Mirrors salt-agent-sdk tests/plan-0243.test.js.
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from saltapp import SaltApiError, crypto
from saltapp.agent import StrategyTunedContext
from saltapp.client import AsyncSaltClient, SaltClient
from saltapp.identity import Identity
from saltapp.work import parse_plan_report

HOST = "https://example.saltapp.test"


def sync_client(handler) -> SaltClient:
    return SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(handler)))


def async_client(handler) -> AsyncSaltClient:
    return AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def recorder(response):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(request, len(calls) - 1) if callable(response) else response

    return calls, handler


def test_tune_plan_patches_snake_case_strategy_keys_with_the_version():
    calls, handler = recorder(httpx.Response(200, json={"id": "m1", "version": 4, "locks": []}))
    out = sync_client(handler).tune_plan("k", "m1", version=3, pace="careful", explore=0.3, goal_kind="growth")
    assert out["version"] == 4
    assert calls[0].method == "PATCH" and calls[0].url.path == "/api/v1/mandates/m1"
    assert json.loads(calls[0].content) == {"version": 3, "strategy": {"pace": "careful", "explore": 0.3, "goal_kind": "growth"}}


def test_a_locked_key_is_plan_locked_with_the_locked_keys():
    _, handler = recorder(httpx.Response(422, json={"error": "You asked to keep this plan as is.", "code": "plan_locked", "locked": ["pace"]}))
    with pytest.raises(SaltApiError) as info:
        sync_client(handler).tune_plan("k", "m1", version=3, pace="aggressive")
    assert info.value.status == 422 and info.value.code == "plan_locked"
    assert info.value.body["locked"] == ["pace"]
    assert "keep this plan as is" in str(info.value)


def test_lock_plan_sends_locks_keys_all_or_none():
    calls, handler = recorder(httpx.Response(200, json={"id": "m1", "version": 5, "locks": ["all"]}))
    client = sync_client(handler)
    assert client.lock_plan("k", "m1", version=4, locks=["all"])["locks"] == ["all"]
    client.lock_plan("k", "m1", version=5, locks=[])
    assert [json.loads(c.content) for c in calls] == [{"version": 4, "locks": ["all"]}, {"version": 5, "locks": []}]
    assert calls[0].method == "PATCH"


def test_lock_plan_by_a_non_grantor_surfaces_locks_grantor_only():
    _, handler = recorder(httpx.Response(403, json={"error": "Only they can do that.", "code": "locks_grantor_only"}))
    with pytest.raises(SaltApiError) as info:
        sync_client(handler).lock_plan("k", "m1", version=1, locks=["pace"])
    assert info.value.code == "locks_grantor_only" and info.value.status == 403


def test_undo_tuning_posts_to_the_undo_route_and_conflicts_are_typed():
    calls, handler = recorder(
        lambda req, i: httpx.Response(200, json={"id": "m1", "version": 6})
        if i == 0
        else httpx.Response(409, json={"error": "That was changed again since. Nothing was undone.", "code": "tuning_superseded"})
    )
    client = sync_client(handler)
    client.undo_tuning("k", "m1", "t 1")
    assert calls[0].method == "POST" and calls[0].url.raw_path.decode().endswith("/api/v1/mandates/m1/tunings/t%201/undo")
    with pytest.raises(SaltApiError) as info:
        client.undo_tuning("k", "m1", "t2")
    assert info.value.code == "tuning_superseded" and info.value.status == 409


def test_report_count_posts_metric_key_value_and_optional_note():
    stored = {"id": "rc1", "mandate_id": "m1", "metric_key": "shop_sales", "value": 7, "note": "Saturday market"}
    calls, handler = recorder(httpx.Response(201, json=stored))
    client = sync_client(handler)
    assert client.report_count("k", "m1", metric_key="shop_sales", value=7, note="Saturday market") == stored
    client.report_count("k", "m1", metric_key="shop_sales", value=0)
    assert json.loads(calls[0].content) == {"metric_key": "shop_sales", "value": 7, "note": "Saturday market"}
    assert json.loads(calls[1].content) == {"metric_key": "shop_sales", "value": 0}
    assert calls[0].url.path == "/api/v1/mandates/m1/reported_counts"


def test_report_count_refusal_keeps_its_code():
    _, handler = recorder(httpx.Response(403, json={"error": "Only Dana reports counts for their plan.", "code": "grantor_only"}))
    with pytest.raises(SaltApiError) as info:
        sync_client(handler).report_count("k", "m1", metric_key="x", value=1)
    assert info.value.code == "grantor_only"


def test_request_ask_as_grantor_sends_as_and_no_where_and_resolves_with_as():
    calls, handler = recorder(httpx.Response(202, json={"status": "asked", "exercise_id": "ex-9", "expires_at": "2026-10-21T00:00:00Z", "as": "grantor"}))
    client = sync_client(handler)
    out = client.request_ask("k", "m1", capability="profile.post", words="We open Saturday.", as_="grantor", idempotency_key="i1")
    assert out == {"asked": True, "exercise_id": "ex-9", "expires_at": "2026-10-21T00:00:00Z", "as": "grantor"}
    assert json.loads(calls[0].content) == {"capability": "profile.post", "as": "grantor", "words": "We open Saturday."}
    assert calls[0].headers["idempotency-key"] == "i1"
    client.request_ask("k", "m1", capability="room.create", words="Garden club", about="For the garden", as_="grantor")
    assert json.loads(calls[1].content) == {"capability": "room.create", "as": "grantor", "words": "Garden club", "about": "For the garden"}


def test_request_ask_as_grantor_refuses_before_the_network_and_a_plain_ask_still_needs_where():
    calls, handler = recorder(httpx.Response(202, json={"status": "asked", "exercise_id": "e"}))
    client = sync_client(handler)
    with pytest.raises(ValueError, match="profile.post and room.create only"):
        client.request_ask("k", "m1", capability="outreach.request", words="hi", as_="grantor", recipient_id="u2")
    with pytest.raises(ValueError, match="no replies, media"):
        client.request_ask("k", "m1", capability="profile.post", words="hi", as_="grantor", kind="reply")
    with pytest.raises(ValueError, match="no replies, media"):
        client.request_ask("k", "m1", capability="profile.post", words="hi", as_="grantor", media_digest="ab")
    with pytest.raises(ValueError, match="can only be"):
        client.request_ask("k", "m1", capability="profile.post", words="hi", as_="agent")
    with pytest.raises(ValueError, match="say where it goes"):
        client.request_ask("k", "m1", capability="profile.post", words="hi")
    assert calls == []
    client.request_ask("k", "m1", capability="profile.post", words="hi", where="Your profile")
    body = json.loads(calls[0].content)
    assert body["where"] == "Your profile" and "as" not in body


def test_server_refusal_of_an_as_grantor_ask_keeps_its_code():
    _, handler = recorder(httpx.Response(422, json={"error": "Replies need your own approval.", "code": "as_post_only"}))
    with pytest.raises(SaltApiError) as info:
        sync_client(handler).request_ask("k", "m1", capability="profile.post", words="hi", as_="grantor")
    assert info.value.code == "as_post_only"


def test_async_client_has_the_same_calls():
    calls, handler = recorder(lambda req, i: httpx.Response(202, json={"status": "asked", "exercise_id": "e", "as": "grantor"}) if req.url.path.endswith("/asks") else httpx.Response(200, json={"id": "m1", "version": 2, "locks": ["pace"]}))

    async def run():
        client = async_client(handler)
        assert (await client.request_ask("k", "m1", capability="room.create", words="Room", as_="grantor"))["as"] == "grantor"
        await client.tune_plan("k", "m1", version=1, pace="careful")
        await client.lock_plan("k", "m1", version=2, locks=["pace"])
        await client.undo_tuning("k", "m1", "t1")
        await client.report_count("k", "m1", metric_key="x", value=2)

    asyncio.run(run())
    assert [c.url.path for c in calls] == [
        "/api/v1/mandates/m1/asks", "/api/v1/mandates/m1", "/api/v1/mandates/m1",
        "/api/v1/mandates/m1/tunings/t1/undo", "/api/v1/mandates/m1/reported_counts",
    ]


# --- read_plan_report -------------------------------------------------------


def strategy(**over):
    return {"strategy_id": "s1", "mandate_id": "m1", "goal": "Sell 20 jars", "next_check_in": "2026-10-22T09:00:00Z",
            "progress": [{"metric": "shop_sales", "actual": 7, "target": 20}], **over}


def report(status, data, work_id="s1"):
    return f"[[SALT-WORK id={work_id} status=running kind=strategy]]\nRunning the plan\nstrategy: {json.dumps({'status': status, **data})}"


def identity(pair):
    return Identity(api_key="k", public_key=pair.public_key, private_key=pair.private_key, passphrase="passphrase-a")


def test_parse_plan_report_reads_a_strategy_report_and_nothing_else():
    r = parse_plan_report(report("attempting", strategy()))
    assert (r["id"], r["status"], r["title"], r["strategy"]["goal"]) == ("s1", "attempting", "Running the plan", "Sell 20 jars")
    assert parse_plan_report('[[SALT-WORK id=w1 status=done kind=task]]\nx\nstrategy: {"status":"succeeded"}') is None
    assert parse_plan_report("[[SALT-WORK id=s1 status=running kind=strategy]]\nx\nstrategy: {nope") is None
    assert parse_plan_report("[[SALT-WORK id=s1 status=running kind=strategy]]\nx") is None
    assert parse_plan_report("hello") is None


def lane_handler(messages, lane_status=200):
    def respond(request: httpx.Request, i: int) -> httpx.Response:
        if request.url.path.endswith("/sidechain"):
            if lane_status != 200:
                return httpx.Response(lane_status, json={"error": "This is already a private lane."})
            return httpx.Response(200, json={"session": {"id": "lane-1"}})
        return httpx.Response(200, json={"session": {"id": "lane-1"}, "messages": messages})

    return recorder(respond)


def test_read_plan_report_opens_the_lane_decrypts_with_the_agents_key_and_returns_the_newest(keypair_a, keypair_b):
    me = identity(keypair_a)
    seal = lambda text, *keys: crypto.encrypt_for(text, list(keys))  # noqa: E731
    messages = [
        {"id": "a", "message": seal(report("attempting", strategy(goal="old")), keypair_a.public_key, keypair_b.public_key), "created_at": "2026-10-19T10:00:00Z"},
        {"id": "b", "message": seal("just chatting", keypair_a.public_key), "created_at": "2026-10-19T11:00:00Z"},
        {"id": "c", "message": seal(report("attempting", strategy(goal="newest")), keypair_a.public_key), "created_at": "2026-10-20T10:00:00Z"},
        {"id": "d", "message": seal(report("attempting", strategy(goal="not mine")), keypair_b.public_key), "created_at": "2026-10-20T11:00:00Z"},
        {"id": "e", "event_type": "joined", "message": "x"},
    ]
    calls, handler = lane_handler(messages)
    got = sync_client(handler).read_plan_report(me, "chat-1", requester_id="u-person")
    assert calls[0].method == "POST" and calls[0].url.path == "/api/v1/chats/chat-1/sidechain"
    assert json.loads(calls[0].content) == {"with_id": "u-person"}
    assert calls[1].url.path == "/api/v1/chats/lane-1"
    assert got["strategy"]["goal"] == "newest" and got["message_id"] == "c" and got["created_at"] == "2026-10-20T10:00:00Z"
    assert got["strategy"]["progress"][0]["actual"] == 7


def test_read_plan_report_reads_chat_id_as_the_lane_without_a_requester_or_when_salt_says_it_is_one(keypair_a):
    me = identity(keypair_a)
    messages = [{"id": "a", "message": crypto.encrypt_for(report("blocked", strategy()), [keypair_a.public_key])}]
    calls, handler = lane_handler(messages)
    assert sync_client(handler).read_plan_report(me, "lane-9")["status"] == "blocked"
    assert [c.url.path for c in calls] == ["/api/v1/chats/lane-9"]
    calls, handler = lane_handler(messages, lane_status=422)
    assert sync_client(handler).read_plan_report(me, "lane-9", requester_id="u")["status"] == "blocked"
    assert calls[1].url.path == "/api/v1/chats/lane-9"


def test_read_plan_report_filters_by_strategy_id_reads_open_room_plaintext_and_returns_none(keypair_a):
    me = identity(keypair_a)
    pub = [keypair_a.public_key]
    messages = [
        {"id": "a", "message": crypto.encrypt_for(report("attempting", strategy(strategy_id="s1", goal="one")), pub)},
        {"id": "b", "message": crypto.encrypt_for(report("attempting", strategy(strategy_id="s2", goal="two"), "s2"), pub)},
    ]
    _, handler = lane_handler(messages)
    client = sync_client(handler)
    assert client.read_plan_report(me, "l", strategy_id="s1")["strategy"]["goal"] == "one"
    assert client.read_plan_report(me, "l", strategy_id="s3") is None
    _, open_handler = lane_handler([{"id": "p", "encrypted": False, "message": report("succeeded", strategy())}])
    assert sync_client(open_handler).read_plan_report(me, "l")["status"] == "succeeded"
    _, empty = lane_handler([])
    assert sync_client(empty).read_plan_report(me, "l") is None


def test_read_plan_report_raises_a_sidechain_failure_that_is_not_already_a_lane(keypair_a):
    _, handler = lane_handler([], lane_status=403)
    with pytest.raises(SaltApiError) as info:
        sync_client(handler).read_plan_report(identity(keypair_a), "c", requester_id="u")
    assert info.value.status == 403


def test_async_read_plan_report(keypair_a):
    me = identity(keypair_a)
    _, handler = lane_handler([{"id": "a", "message": crypto.encrypt_for(report("attempting", strategy()), [keypair_a.public_key])}])
    got = asyncio.run(async_client(handler).read_plan_report(me, "chat-1", requester_id="u"))
    assert got["strategy"]["goal"] == "Sell 20 jars"


# --- strategy_tuned carries who changed it ----------------------------------


def test_strategy_tuned_context_exposes_by_before_and_tuning_id_and_is_none_on_older_salt():
    base = {"id": "m1", "type": "strategy_tuned", "strategy_id": "s1", "changes": {"pace": "careful"}, "strategy": {"goal": "g"}, "version": 7}
    ctx = StrategyTunedContext(None, {**base, "by": "person", "before": {"pace": "balanced"}, "tuning_id": "t9"})
    assert (ctx.by, ctx.before, ctx.tuning_id) == ("person", {"pace": "balanced"}, "t9")
    old = StrategyTunedContext(None, base)
    assert (old.by, old.before, old.tuning_id) == (None, None, None)
