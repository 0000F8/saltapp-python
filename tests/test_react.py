"""client.react / ctx.react against a mock server shaped like salt-api's
ReactionsController: {message_id, reactions: [{emoji, count, user_ids}]} on
success, 422 {error: "<sentence>"} on a refusal. Reactions are plaintext."""
from __future__ import annotations

import json

import httpx
import pytest

from saltapp.agent import Agent, MessageContext
from saltapp.client import AsyncSaltClient, SaltClient
from saltapp.errors import SaltApiError

HOST = "https://example.saltapp.test"


def toggle_server(calls: list):
    mine: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append({"method": request.method, "url": str(request.url), "key": request.headers.get("api-key"), "body": body})
        if request.url.path.endswith("/reactions"):
            emoji = body["emoji"]
            if emoji in mine:
                mine.discard(emoji)
            elif len(emoji) != 1:
                return httpx.Response(422, json={"error": "Pick a single emoji."})
            elif len(mine) >= 12:
                return httpx.Response(422, json={"error": "You can react with up to 12 emoji."})
            else:
                mine.add(emoji)
            mid = request.url.path.split("/")[-2]
            return httpx.Response(200, json={"message_id": mid, "reactions": [{"emoji": e, "count": 1, "user_ids": ["a1"]} for e in sorted(mine)]})
        if request.url.path.endswith("/reactions/mine"):
            return httpx.Response(200, json={"recent": sorted(mine)})
        return httpx.Response(200, json={})

    return handler


def test_sync_react_toggles_and_raises_the_server_sentence():
    calls: list = []
    client = SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(toggle_server(calls))))

    first = client.react("key", "m-1", "✅")
    assert first == {"message_id": "m-1", "reactions": [{"emoji": "✅", "count": 1, "user_ids": ["a1"]}]}
    assert calls[0] == {"method": "POST", "url": f"{HOST}/api/v1/messages/m-1/reactions", "key": "key", "body": {"emoji": "✅"}}
    assert client.react("key", "m-1", "✅")["reactions"] == []

    with pytest.raises(SaltApiError) as err:
        client.react("key", "m-1", "hi")
    assert err.value.status == 422
    assert "Pick a single emoji." in str(err.value)
    assert client.my_reactions("key") == {"recent": []}


def test_sync_react_encodes_the_id_into_one_path_segment():
    calls: list = []
    client = SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(toggle_server(calls))))
    client.react("key", "../agents/callback", "👍")
    assert "/agents/callback" not in calls[0]["url"].replace("%2F", "")[len(HOST):].split("/messages/")[0]
    assert calls[0]["url"].startswith(f"{HOST}/api/v1/messages/..%2Fagents%2Fcallback/reactions")


@pytest.mark.asyncio
async def test_async_react_cap_sentence():
    calls: list = []
    client = AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(toggle_server(calls))))
    for e in "ABCDEFGHIJKL":
        await client.react("key", "m", e)
    with pytest.raises(SaltApiError, match="You can react with up to 12 emoji."):
        await client.react("key", "m", "Z")


@pytest.mark.asyncio
async def test_ctx_react_reacts_to_the_handled_message():
    calls: list = []
    client = AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(toggle_server(calls))))
    agent = Agent(host=HOST, api_key="key", public_key="pub", private_key="priv", passphrase="pw", agent_id="agent-1", client=client)
    ctx = MessageContext(
        agent, chat_id="chat-1", sender_id="h1", sender={"id": "h1", "username": "dan", "account_type": "User"},
        text="thanks!", room_id="chat-1", chat_meta={}, raw_message={"message_id": "msg-42"},
    )
    assert ctx.message_id == "msg-42"
    out = await ctx.react("🎉")
    assert out["message_id"] == "msg-42"
    assert calls[0]["url"] == f"{HOST}/api/v1/messages/msg-42/reactions"
    assert calls[0]["body"] == {"emoji": "🎉"}

    bare = MessageContext(agent, chat_id="c", sender_id="h1", sender={}, text="", room_id="c", chat_meta={}, raw_message={})
    with pytest.raises(ValueError):
        await bare.react("👍")
