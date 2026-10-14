# client.draft_agent (salt-api POST /api/v1/agents/draft, AGENT_MAKER_CONTRACT.md section 1). Mirrors
# salt-agent-sdk's tests/draft-agent.test.js.
from __future__ import annotations

import json

import httpx
import pytest

from saltapp import SaltApiError
from saltapp.client import AsyncSaltClient, SaltClient

HOST = "https://example.saltapp.test"
DRAFT = {
    "idea": "Daily sci-fi stories that build their own world",
    "name": "Driftlight",
    "handle": "driftlight",
    "bio": "A new chapter from the Driftlight worlds, every morning.",
    "brief": "You are Driftlight, ...",
    "assumptions": [{"about": "When", "choice": "One chapter every morning at 8:00, your time"}],
    "voice": "Warm and a little wry.",
    "first_message": "Hello. Chapter one is on its way.",
    "skills": {
        "recommended": [{"key": "reminders", "why": "Your brief: a chapter every morning at 8:00"}],
        "optional": [{"key": "money", "why": "Always your call, never set for you"}],
    },
    "services": [{"provider": "runway", "recommended": False, "why": "Could make a cover image for each chapter"}],
    "budget_per_day": 20,
    "actions_estimate": "about 3: write, post, send to you",
}


def make_sync(handler) -> SaltClient:
    return SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(handler)))


def make_async(handler) -> AsyncSaltClient:
    return AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def recorder(status: int = 200, body=None):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=body if body is not None else {"draft": DRAFT})

    return calls, handler


def test_draft_agent_posts_the_idea_and_unwraps_the_draft():
    calls, handler = recorder()
    draft = make_sync(handler).draft_agent("k", idea=DRAFT["idea"], step="brief")
    assert calls[0].method == "POST"
    assert str(calls[0].url) == f"{HOST}/api/v1/agents/draft"
    assert calls[0].headers["api-key"] == "k"
    assert json.loads(calls[0].content) == {"idea": DRAFT["idea"], "step": "brief"}
    assert draft == DRAFT


def test_draft_agent_sends_the_draft_and_nudge_only_when_given():
    calls, handler = recorder()
    make_sync(handler).draft_agent("k", step="personality", draft=DRAFT, nudge="More playful")
    assert json.loads(calls[0].content) == {"step": "personality", "draft": DRAFT, "nudge": "More playful"}


def test_a_failed_draft_raises_with_the_code_and_sentence():
    _, handler = recorder(502, {"error": "Couldn't write it just now. Try again.", "code": "draft_failed"})
    with pytest.raises(SaltApiError) as caught:
        make_sync(handler).draft_agent("k", idea="x", step="brief")
    assert caught.value.status == 502
    assert caught.value.code == "draft_failed"
    assert str(caught.value).endswith("Couldn't write it just now. Try again.")


def test_act_for_client_has_draft_agent():
    calls, handler = recorder()
    make_sync(handler).act_for("principal-1").draft_agent("k", idea="x", step="brief")
    assert calls[0].headers["x-salt-act-for"] == "principal-1"


@pytest.mark.asyncio
async def test_async_draft_agent_mirrors_the_sync_one():
    calls, handler = recorder()
    draft = await make_async(handler).draft_agent("k", step="authority", draft=DRAFT)
    assert str(calls[0].url) == f"{HOST}/api/v1/agents/draft"
    assert json.loads(calls[0].content) == {"step": "authority", "draft": DRAFT}
    assert draft == DRAFT
