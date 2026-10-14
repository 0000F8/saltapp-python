# Team helpers (TEAM_MAKER_CONTRACT.md): draft a whole team, set the lead and roles, open the room, read your own
# teams. Mirrors salt-agent-sdk's tests/team-room.test.js.
from __future__ import annotations

import json

import httpx
import pytest

from saltapp import SaltApiError
from saltapp.client import AsyncSaltClient, SaltClient

HOST = "https://example.saltapp.test"
MEMBERS = [
    {"id": "a1", "handle": "mara", "display_name": "Mara", "role": "Showrunner", "lead": True, "owned": True},
    {"id": "a2", "handle": "quill", "display_name": "Quill", "role": "Writer", "lead": False, "owned": True},
]
GROUP = {"id": "g1", "name": "Writers' room", "member_ids": ["a1", "a2"], "lead_agent_id": "a1", "room_chat_id": None, "members": MEMBERS}
DRAFT = {
    "idea": "A writers' room for my sci-fi world",
    "team_name": "Driftlight room",
    "description": "Writes and keeps the Driftlight world.",
    "how_they_work": "Mara decides; the others answer her.",
    "lead_index": 0,
    "members": [
        {"role": "Showrunner", "name": "Mara", "handle": "mara", "bio": "Runs the room.", "brief": "You are Mara...", "why": "Someone must decide."},
        {"role": "Writer", "name": "Quill", "handle": "quill", "bio": "Writes chapters.", "brief": "You are Quill...", "why": "The prose."},
    ],
    "totals": {"actions_per_day": None, "model_budget_usd": None, "budget_basis": ""},
}


def make_sync(handler) -> SaltClient:
    return SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(handler)))


def make_async(handler) -> AsyncSaltClient:
    return AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def recorder(status: int = 200, body=None):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=body if body is not None else GROUP)

    return calls, handler


def test_draft_team_posts_and_unwraps_the_draft():
    calls, handler = recorder(200, {"draft": DRAFT})
    draft = make_sync(handler).draft_team("k", idea=DRAFT["idea"], step="team")
    assert calls[0].method == "POST"
    assert str(calls[0].url) == f"{HOST}/api/v1/teams/draft"
    assert calls[0].headers["api-key"] == "k"
    assert json.loads(calls[0].content) == {"idea": DRAFT["idea"], "step": "team"}
    assert draft == DRAFT


def test_draft_team_sends_the_draft_and_nudge_only_when_given():
    calls, handler = recorder(200, {"draft": DRAFT})
    make_sync(handler).draft_team("k", step="personality", draft=DRAFT, nudge="More playful")
    assert json.loads(calls[0].content) == {"step": "personality", "draft": DRAFT, "nudge": "More playful"}


def test_a_failed_team_draft_raises_with_the_code():
    _, handler = recorder(502, {"error": "Couldn't write it just now. Try again.", "code": "draft_failed"})
    with pytest.raises(SaltApiError) as caught:
        make_sync(handler).draft_team("k", idea="x", step="team")
    assert caught.value.status == 502
    assert caught.value.code == "draft_failed"


def test_set_team_lead_patches_the_group_and_none_clears_it():
    calls, handler = recorder()
    client = make_sync(handler)
    group = client.set_team_lead("k", "g1", "a1")
    client.set_team_lead("k", "g1", None)
    assert calls[0].method == "PATCH"
    assert str(calls[0].url) == f"{HOST}/api/v1/agent_groups/g1"
    assert json.loads(calls[0].content) == {"lead_agent_id": "a1"}
    assert json.loads(calls[1].content) == {"lead_agent_id": None}
    assert group["members"][0]["lead"] is True


def test_set_team_lead_with_a_non_member_raises_the_sentence():
    _, handler = recorder(422, {"error": "The lead must be one of the team's members."})
    with pytest.raises(SaltApiError) as caught:
        make_sync(handler).set_team_lead("k", "g1", "zz")
    assert caught.value.status == 422


def test_set_member_role_patches_the_membership():
    calls, handler = recorder()
    make_sync(handler).set_member_role("k", "g1", "a2", "Writer")
    assert calls[0].method == "PATCH"
    assert str(calls[0].url) == f"{HOST}/api/v1/agent_groups/g1/members/a2"
    assert json.loads(calls[0].content) == {"role": "Writer"}


def test_open_team_room_posts_and_returns_the_chat_id():
    calls, handler = recorder(201, {"room_chat_id": "c9", "created": True})
    assert make_sync(handler).open_team_room("k", "g1") == {"room_chat_id": "c9", "created": True}
    assert calls[0].method == "POST"
    assert str(calls[0].url) == f"{HOST}/api/v1/agent_groups/g1/room"


def test_my_teams_unwraps_the_list():
    teams = [{"id": "g1", "name": "Writers' room", "lead_agent_id": "a1", "room_chat_id": "c9", "owner_id": "u1", "group_owner_id": "o2", "members": MEMBERS}]
    calls, handler = recorder(200, {"teams": teams})
    assert make_sync(handler).my_teams("agent-key") == teams
    assert teams[0]["group_owner_id"] == "o2"
    assert calls[0].method == "GET"
    assert str(calls[0].url) == f"{HOST}/api/v1/agent/teams"
    assert calls[0].headers["api-key"] == "agent-key"


def test_act_for_client_has_the_team_helpers():
    calls, handler = recorder()
    make_sync(handler).act_for("principal-1").set_member_role("k", "g1", "a2", "Writer")
    assert calls[0].headers["x-salt-act-for"] == "principal-1"


@pytest.mark.asyncio
async def test_async_helpers_mirror_the_sync_ones():
    calls, handler = recorder(200, {"draft": DRAFT, "teams": [], "room_chat_id": "c9", "created": False})
    client = make_async(handler)
    assert await client.draft_team("k", idea="x", step="team") == DRAFT
    assert await client.my_teams("k") == []
    await client.open_team_room("k", "g1")
    await client.set_team_lead("k", "g1", "a1")
    await client.set_member_role("k", "g1", "a2", "Writer")
    assert [(c.method, c.url.path) for c in calls] == [
        ("POST", "/api/v1/teams/draft"),
        ("GET", "/api/v1/agent/teams"),
        ("POST", "/api/v1/agent_groups/g1/room"),
        ("PATCH", "/api/v1/agent_groups/g1"),
        ("PATCH", "/api/v1/agent_groups/g1/members/a2"),
    ]


def test_draft_team_authority_step_returns_member_budgets_and_totals():
    filled = {
        **DRAFT,
        "members": [{**m, "model_budget_usd": "10", "budget_basis": "About 3 actions a day"} for m in DRAFT["members"]],
        "totals": {"actions_per_day": 20, "model_budget_usd": "20", "budget_basis": "About 20 actions a day across the team"},
    }
    calls, handler = recorder(200, {"draft": filled})
    draft = make_sync(handler).draft_team("k", step="authority", draft=DRAFT)
    assert json.loads(calls[0].content) == {"step": "authority", "draft": DRAFT}
    assert draft["totals"]["model_budget_usd"] == "20"
    assert draft["members"][0]["model_budget_usd"] == "10"


def test_set_member_role_none_clears_the_role():
    calls, handler = recorder()
    make_sync(handler).set_member_role("k", "g1", "a2", None)
    assert json.loads(calls[0].content) == {"role": None}


def test_open_team_room_reports_left_out():
    _, handler = recorder(201, {"room_chat_id": "c9", "created": True, "left_out": 1})
    assert make_sync(handler).open_team_room("k", "g1")["left_out"] == 1


def test_set_team_lead_with_an_unowned_agent_raises_the_422():
    _, handler = recorder(422, {"error": "The lead must be one of your own agents."})
    with pytest.raises(SaltApiError) as caught:
        make_sync(handler).set_team_lead("k", "g1", "outsider")
    assert caught.value.status == 422
