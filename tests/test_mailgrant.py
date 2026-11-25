# Salt 0.264.0 mailbox grant, agent side: client.mailbox (sync + async), MailGrantError, open_mail_row, the two deliveries.
# Mirrors salt-agent-sdk tests for 0.42.0. No network: httpx.MockTransport.
from __future__ import annotations

import asyncio
import json
import pathlib

import httpx
import pytest

import saltapp
from saltapp import (
    MailApprovalCancelled,
    MailApprovalTimeout,
    MailGrantError,
    MailRowError,
    SaltApiError,
    as_mail_grant_error,
    crypto,
    is_mail_asked,
    open_mail_row,
)
from saltapp.agent import Agent
from saltapp.client import AsyncSaltClient, SaltClient
from saltapp.mailgrant import (
    MAIL_ALIAS_PLAN_REF_MAX,
    MAIL_ALIAS_PURPOSE_MAX,
    MAIL_REFUSAL_CODES,
    parse_alias_wake,
    parse_email_received,
)
from saltapp.webhook import Event, classify

HOST = "https://example.saltapp.test"
ROWS = json.loads((pathlib.Path(__file__).parent / "fixtures" / "mail_in_rows.json").read_text())


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


def test_reads_go_to_agents_me_with_the_holder_in_the_query():
    calls, handler = recorder(httpx.Response(200, json={"ok": True}))
    m = sync_client(handler).mailbox
    m.get("k", "h1", peek=True)
    m.get_grant("k", "h1")
    m.list_messages("k", "h1", before="b9", approval_id="a1")
    m.open_message("k", "msg1", "h1", approval_id="a1")
    m.list_aliases("k", "h1")
    seen = [(c.method, c.url.path, dict(c.url.params)) for c in calls]
    assert seen == [
        ("GET", "/api/v1/agents/me/mailbox", {"holder_id": "h1", "peek": "true"}),
        ("GET", "/api/v1/agents/me/mailbox_grant", {"holder_id": "h1"}),
        ("GET", "/api/v1/agents/me/mailbox/messages", {"holder_id": "h1", "before": "b9", "approval_id": "a1"}),
        ("GET", "/api/v1/agents/me/mailbox/messages/msg1", {"holder_id": "h1", "approval_id": "a1"}),
        ("GET", "/api/v1/agents/me/mailbox/tags", {"holder_id": "h1"}),
    ]
    assert all(c.headers["api-key"] == "k" for c in calls)


def test_list_senders_pages_with_limit_and_before():
    calls, handler = recorder(httpx.Response(200, json={"senders": [], "next_before": None}))
    m = sync_client(handler).mailbox
    m.list_senders("k", "h1")
    m.list_senders("k", "h1", limit=100, before="s7")
    assert dict(calls[0].url.params) == {"holder_id": "h1"}
    assert dict(calls[1].url.params) == {"holder_id": "h1", "limit": "100", "before": "s7"}


def test_allow_block_revoke_use_the_right_verbs_and_paths():
    calls, handler = recorder(httpx.Response(200, json={}))
    m = sync_client(handler).mailbox
    m.allow_sender("k", "s1", "h1")
    m.block_sender("k", "s2", "h1")
    m.revoke_alias("k", "t1", "h1")
    assert [(c.method, c.url.path) for c in calls] == [
        ("POST", "/api/v1/agents/me/mailbox/senders/s1/allow"),
        ("POST", "/api/v1/agents/me/mailbox/senders/s2/block"),
        ("DELETE", "/api/v1/agents/me/mailbox/tags/t1"),
    ]


def test_make_alias_body_and_idempotency_key():
    calls, handler = recorder(httpx.Response(201, json={"id": "t1"}))
    m = sync_client(handler).mailbox
    m.make_alias("k", "h1", tag="bsky", purpose="Bluesky sign-up", plan_ref="acct-7", idempotency_key="idem-1")
    m.make_alias("k", "h1")
    assert calls[0].method == "POST" and calls[0].url.path == "/api/v1/agents/me/mailbox/tags"
    assert json.loads(calls[0].content) == {"tag": "bsky", "purpose": "Bluesky sign-up", "plan_ref": "acct-7"}
    assert calls[0].headers["Idempotency-Key"] == "idem-1"
    assert json.loads(calls[1].content) == {}
    assert len(calls[1].headers["Idempotency-Key"]) >= 16


def test_make_alias_length_checks_throw_before_any_request():
    calls, handler = recorder(httpx.Response(201, json={}))
    m = sync_client(handler).mailbox
    with pytest.raises(MailGrantError) as exc:
        m.make_alias("k", "h1", purpose="x" * (MAIL_ALIAS_PURPOSE_MAX + 1))
    assert exc.value.kind == "invalid_purpose" and exc.value.code == "invalid_purpose" and exc.value.status == 422
    with pytest.raises(MailGrantError) as exc2:
        m.make_alias("k", "h1", plan_ref="x" * (MAIL_ALIAS_PLAN_REF_MAX + 1))
    assert exc2.value.kind == "invalid_plan_ref"
    assert calls == []
    m.make_alias("k", "h1", purpose="x" * MAIL_ALIAS_PURPOSE_MAX, plan_ref="y" * MAIL_ALIAS_PLAN_REF_MAX)
    assert len(calls) == 1


def asked_body(approval_id="ap1"):
    return {"approval_id": approval_id, "status": "waiting", "message": "Asked.", "poll": f"/api/v1/approvals/{approval_id}"}


def test_a_202_is_a_mail_asked_not_the_exercise_shape():
    _, handler = recorder(httpx.Response(202, json=asked_body()))
    r = sync_client(handler).mailbox.open_message("k", "m1", "h1")
    assert r == {"asked": True, "approval_id": "ap1", "status": "waiting", "poll": "/api/v1/approvals/ap1", "message": "Asked."}
    assert is_mail_asked(r) is True
    assert is_mail_asked({"asked": True, "exercise_id": "x"}) is False
    assert is_mail_asked({"messages": []}) is False


def refusal(code, status=403, **extra):
    return httpx.Response(status, json={"error": f"sentence for {code}", "code": code, **extra})


@pytest.mark.parametrize("code", MAIL_REFUSAL_CODES)
def test_every_refusal_code_becomes_a_typed_error_with_a_next_sentence(code):
    _, handler = recorder(refusal(code, 429 if code == "ask_limit" else 403, **({"capability": "mail.read", "feature": "Read mail"} if code == "mail_grant_required" else {})))
    with pytest.raises(MailGrantError) as exc:
        sync_client(handler).mailbox.get_grant("k", "h1")
    e = exc.value
    assert isinstance(e, SaltApiError) and e.kind == code and e.sentence == f"sentence for {code}"
    assert isinstance(e.next, str) and e.next.endswith((".", ")")) and "\n" not in e.next


def test_mail_grant_required_carries_the_ready_ask_and_the_next_step_names_the_feature():
    ask = {"method": "POST", "path": "/api/v1/mandates", "body": {"capabilities": [{"capability": "mail.read"}]}}
    grant = {"capability": "mail.read", "selector": "*", "mode": "ask"}
    _, handler = recorder(refusal("mail_grant_required", capability="mail.read", feature="Read mail", grant=grant, ask=ask))
    with pytest.raises(MailGrantError) as exc:
        sync_client(handler).mailbox.list_messages("k", "h1")
    e = exc.value
    assert (e.capability, e.feature, e.grant, e.ask) == ("mail.read", "Read mail", grant, ask)
    assert "Read mail" in e.next and "request_grant" in e.next


def test_ask_only_on_mail_grant_required_and_ask_limit_has_retry_after_seconds():
    _, handler = recorder(httpx.Response(429, json={"error": "Too many asks.", "code": "ask_limit", "ask": {"path": "/x", "body": {}}}, headers={"Retry-After": "120"}))
    with pytest.raises(MailGrantError) as exc:
        sync_client(handler).mailbox.allow_sender("k", "s1", "h1")
    assert exc.value.retry_after_seconds == 120 and exc.value.ask is None and exc.value.grant is None
    _, handler2 = recorder(refusal("declined"))
    with pytest.raises(MailGrantError) as exc2:
        sync_client(handler2).mailbox.allow_sender("k", "s1", "h1")
    assert exc2.value.retry_after_seconds is None and "hour" in exc2.value.next


def test_other_errors_stay_plain_and_as_mail_grant_error_ignores_them():
    _, handler = recorder(httpx.Response(404, json={"error": "Not found."}))
    with pytest.raises(SaltApiError) as exc:
        sync_client(handler).mailbox.get_approval("k", "nope")
    assert not isinstance(exc.value, MailGrantError) and exc.value.status == 404
    assert as_mail_grant_error(exc.value) is None
    assert as_mail_grant_error(ValueError("x")) is None
    # The generic mapping elsewhere is unchanged: a `declined` code outside the mailbox namespace is a plain SaltApiError.
    _, handler2 = recorder(refusal("declined"))
    with pytest.raises(SaltApiError) as exc2:
        sync_client(handler2).get_goal_request("k", "g1")
    assert not isinstance(exc2.value, MailGrantError)


def test_get_approval_reads_the_approval():
    body = {"approval_id": "ap1", "status": "approved", "capability": "mail.read", "performed": True, "spendable": True, "reason": None}
    calls, handler = recorder(httpx.Response(200, json=body))
    assert sync_client(handler).mailbox.get_approval("k", "ap1")["spendable"] is True
    assert calls[0].method == "GET" and calls[0].url.path == "/api/v1/approvals/ap1"


class FakeTime:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    async def asleep(self, s):
        self.sleep(s)


def approval_sequence(*statuses, **extra):
    it = iter(statuses)
    last = statuses[-1]

    def respond(request):
        return httpx.Response(200, json={"approval_id": "ap1", "status": next(it, last), **extra})

    return respond


def test_wait_for_approval_polls_with_growing_interval_until_decided():
    ft = FakeTime()
    calls, handler = recorder(approval_sequence("waiting", "waiting", "waiting", "waiting", "approved", performed=True))
    r = sync_client(handler).mailbox.wait_for_approval("k", {"asked": True, "approval_id": "ap1", "status": "waiting"}, sleep=ft.sleep, clock=ft.clock)
    assert r["status"] == "approved" and r["performed"] is True
    assert len(calls) == 5
    assert ft.sleeps[0] == 5.0 and ft.sleeps == sorted(ft.sleeps) and max(ft.sleeps) <= 30.0


def test_wait_for_approval_accepts_a_plain_id_and_returns_declined():
    _, handler = recorder(approval_sequence("declined", reason="declined", performed=False))
    r = sync_client(handler).mailbox.wait_for_approval("k", "ap1", sleep=lambda s: None)
    assert r["status"] == "declined" and r["performed"] is False and r["reason"] == "declined"


def test_wait_for_approval_times_out_with_the_last_approval():
    ft = FakeTime()
    _, handler = recorder(approval_sequence("waiting"))
    with pytest.raises(MailApprovalTimeout) as exc:
        sync_client(handler).mailbox.wait_for_approval("k", "ap1", timeout_s=60, sleep=ft.sleep, clock=ft.clock)
    assert exc.value.approval["status"] == "waiting"
    assert ft.t >= 60


def test_wait_for_approval_stops_when_cancelled():
    calls, handler = recorder(approval_sequence("waiting"))
    flag = {"stop": False}

    def sleep(_s):
        flag["stop"] = True

    with pytest.raises(MailApprovalCancelled):
        sync_client(handler).mailbox.wait_for_approval("k", "ap1", cancel=lambda: flag["stop"], sleep=sleep)
    assert len(calls) == 1


def test_async_namespace_matches_the_sync_one():
    async def run():
        calls, handler = recorder(lambda r: httpx.Response(202, json=asked_body()) if r.url.path.endswith("/allow") else httpx.Response(200, json={"approval_id": "ap1", "status": "approved", "performed": True}))
        m = async_client(handler).mailbox
        assert is_mail_asked(await m.allow_sender("k", "s1", "h1"))
        ft = FakeTime()
        r = await m.wait_for_approval("k", "ap1", sleep=ft.asleep, clock=ft.clock)
        assert r["status"] == "approved"
        with pytest.raises(MailGrantError) as exc:
            await m.make_alias("k", "h1", purpose="x" * 61)
        assert exc.value.kind == "invalid_purpose"
        _, h2 = recorder(refusal("owner_decides"))
        with pytest.raises(MailGrantError) as exc2:
            await async_client(h2).mailbox.get_grant("k", "h1")
        assert exc2.value.kind == "owner_decides"
        assert [c.url.path for c in calls][:1] == ["/api/v1/agents/me/mailbox/senders/s1/allow"]

    asyncio.run(run())


def test_async_wait_for_approval_times_out():
    async def run():
        ft = FakeTime()
        _, handler = recorder(approval_sequence("waiting"))
        with pytest.raises(MailApprovalTimeout):
            await async_client(handler).mailbox.wait_for_approval("k", "ap1", timeout_s=20, sleep=ft.asleep, clock=ft.clock)

    asyncio.run(run())


def test_acting_clients_also_get_the_namespaces():
    _, handler = recorder(httpx.Response(200, json={}))
    assert sync_client(handler).act_for("p1").mailbox.get_grant("k", "h1") == {}
    assert hasattr(async_client(handler).act_for("p1"), "folders")


# -- open_mail_row ---------------------------------------------------------------------------------------------------


def seal(row, keypair):
    return crypto.encrypt_for(json.dumps(row), [keypair.public_key])


def test_open_mail_row_decrypts_validates_and_returns_the_text_as_data(keypair_a):
    mail = open_mail_row(seal(ROWS["hostile"], keypair_a), keypair_a.private_key, "passphrase-a")
    assert mail["kind"] == "mail_in" and mail["v"] == 1 and mail["content_trust"] == "untrusted_external"
    assert mail["from"] == {"address": "stranger@example.test", "name": "Stranger"}
    assert mail["text"] == ROWS["hostile"]["text"]  # verbatim data, never acted on
    assert mail["auth"] == {"dmarc": "pass", "dkim": "pass", "spf": "pass"}
    assert "extra_field_not_allowed" not in mail
    assert open_mail_row is saltapp.mailgrant.open_mail_row


@pytest.mark.parametrize("name", ["wrong_kind", "wrong_version"])
def test_open_mail_row_throws_for_anything_else(keypair_a, name):
    with pytest.raises(MailRowError):
        open_mail_row(seal(ROWS[name], keypair_a), keypair_a.private_key, "passphrase-a")


def test_open_mail_row_throws_for_non_json_and_wrong_key(keypair_a, keypair_b):
    with pytest.raises(MailRowError):
        open_mail_row(crypto.encrypt_for("not json", [keypair_a.public_key]), keypair_a.private_key, "passphrase-a")
    with pytest.raises(crypto.CryptoError):
        open_mail_row(seal(ROWS["hostile"], keypair_a), keypair_b.private_key, "passphrase-b")


# -- the deliveries --------------------------------------------------------------------------------------------------

EMAIL = {
    "type": "email_received", "holder_id": "h1", "message_id": "m1", "chat_id": "c1",
    "contact": {"id": "ct1", "email_address": "a@example.test", "origin": "inbound", "channel": "email", "status": "pending", "user_id": None, "junk": 1},
    "trusted_sender": False, "content_trust": "untrusted_external", "tag": "bsky",
    "attachments": [{"message_id": "m2", "size": 12, "filename": "leak"}], "withheld_count": 1, "received_at": "2026-11-24T10:00:00Z",
    "alias_wake": {"tag": "bsky", "purpose": "Bluesky sign-up", "plan_ref": "acct-7", "from_host": "mail.bsky.example", "from_domain": "bsky.example", "needs_ok": True, "evil": "x"},
}


def test_classify_knows_the_two_mail_events_without_a_message_field():
    assert classify({"type": "mail_grant_changed", "holder_id": "h1"}) == "mail_grant_changed"
    assert classify(EMAIL) == "email_received"


def test_parse_alias_wake_allow_lists_and_keeps_host_and_domain_apart():
    wake = parse_alias_wake(EMAIL)
    assert wake == {"tag": "bsky", "purpose": "Bluesky sign-up", "plan_ref": "acct-7", "from_host": "mail.bsky.example", "from_domain": "bsky.example", "needs_ok": True}
    assert parse_alias_wake({"alias_wake": {"tag": "t", "purpose": None, "plan_ref": "p"}}) == {"tag": "t", "purpose": None, "plan_ref": "p"}
    assert parse_alias_wake({"type": "email_received"}) is None
    assert parse_alias_wake({"alias_wake": "x"}) is None


def test_parse_email_received_allow_lists_fields():
    e = parse_email_received(EMAIL)
    assert e["contact"] == {"id": "ct1", "email_address": "a@example.test", "origin": "inbound", "channel": "email", "status": "pending", "user_id": None}
    assert e["attachments"] == [{"message_id": "m2", "size": 12}] and e["withheld_count"] == 1 and e["content_trust"] == "untrusted_external"
    assert "front_desk" not in e
    fd = parse_email_received({**EMAIL, "front_desk": True, "conversation_id": "cv1"})
    assert fd["front_desk"] is True and fd["conversation_id"] == "cv1"


def make_agent():
    client = async_client(lambda r: httpx.Response(200, json={}))
    return Agent(host=HOST, api_key="key", public_key="pub", private_key="priv", agent_id="self", client=client)


def test_agent_dispatches_mail_grant_changed_and_email_received():
    async def run():
        agent = make_agent()
        changed, mail = [], []

        @agent.on_mail_grant_changed
        async def a(ctx):
            changed.append(ctx)

        @agent.on_email_received
        async def b(ctx):
            mail.append(ctx)

        body = {"type": "mail_grant_changed", "holder_id": "h1", "paused": True, "changed": [{"capability": "mail.read", "mode": "alone"}, {"capability": "mail.send.new", "mode": None}], "changed_at": "2026-11-24T12:00:00Z"}
        await agent.dispatch(Event(type="mail_grant_changed", body=body, delivery_id="d1"))
        await agent.dispatch(Event(type="email_received", body=EMAIL, delivery_id="d2"))
        await agent.dispatch(Event(type="email_received", body={**EMAIL, "alias_wake": None}, delivery_id="d3"))
        assert changed[0].paused is True and changed[0].holder_id == "h1"
        assert changed[0].changed == [{"capability": "mail.read", "mode": "alone"}, {"capability": "mail.send.new", "mode": None}]
        assert mail[0].needs_ok is True and mail[0].alias_wake["from_host"] == "mail.bsky.example" and mail[0].message_id == "m1"
        assert mail[1].alias_wake is None and mail[1].needs_ok is False

    asyncio.run(run())


def test_events_without_handlers_are_dropped_quietly():
    async def run():
        agent = make_agent()
        await agent.dispatch(Event(type="email_received", body=EMAIL))
        await agent.dispatch(Event(type="mail_grant_changed", body={"type": "mail_grant_changed"}))

    asyncio.run(run())
