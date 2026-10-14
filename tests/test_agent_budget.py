# An agent's monthly model budget: the draft proposes it, the owner sets it (PATCH persona).
# Mirrors salt-agent-sdk's tests/agent-budget.test.js.
from __future__ import annotations

import json

import httpx
import pytest

from saltapp import SaltApiError
from saltapp.client import AsyncSaltClient, SaltClient

HOST = "https://example.saltapp.test"
URL = f"{HOST}/api/v1/agents/a1/persona"


def make_sync(handler) -> SaltClient:
    return SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(handler)))


def make_async(handler) -> AsyncSaltClient:
    return AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def recorder(status: int = 200, body=None):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=body if body is not None else {"id": "a1"})

    return calls, handler


def test_draft_agent_returns_the_budget_fields():
    draft = {"idea": "x", "model_budget_usd": "12", "budget_basis": "About three posts a day on a small model."}
    _, handler = recorder(200, {"draft": draft})
    result = make_sync(handler).draft_agent("k", idea="x", step="authority")
    assert result["model_budget_usd"] == "12"
    assert result["budget_basis"] == "About three posts a day on a small model."


def test_set_agent_budget_patches_the_persona_with_a_string():
    calls, handler = recorder()
    client = make_sync(handler)
    client.set_agent_budget("k", "a1", 25)
    client.set_agent_budget("k", "a1", "7")
    assert calls[0].method == "PATCH"
    assert str(calls[0].url) == URL
    assert calls[0].headers["api-key"] == "k"
    assert json.loads(calls[0].content) == {"persona": {"model_budget_usd": "25"}}
    assert json.loads(calls[1].content) == {"persona": {"model_budget_usd": "7"}}


def test_a_refusal_raises_with_the_sentence():
    _, handler = recorder(422, {"error": "persona.model_budget_usd must be a number from 1 to 1000."})
    with pytest.raises(SaltApiError) as caught:
        make_sync(handler).set_agent_budget("k", "a1", 5000)
    assert caught.value.status == 422
    assert str(caught.value).endswith("from 1 to 1000.")


def test_act_for_client_has_set_agent_budget():
    calls, handler = recorder()
    make_sync(handler).act_for("principal-1").set_agent_budget("k", "a1", 10)
    assert calls[0].headers["x-salt-act-for"] == "principal-1"


@pytest.mark.asyncio
async def test_async_set_agent_budget():
    calls, handler = recorder()
    await make_async(handler).set_agent_budget("k", "a1", 10)
    assert str(calls[0].url) == URL
    assert json.loads(calls[0].content) == {"persona": {"model_budget_usd": "10"}}
