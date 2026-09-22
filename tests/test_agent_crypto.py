# Regression coverage for the pre-existing decrypt bug the 2026-09-22 open
# rooms survey found: `Agent._handle_message` used to set `ctx.text =
# message.get("message")` directly, RAW, with zero decryption for an
# encrypted chat's message -- `saltapp.crypto.decrypt()` was never called
# anywhere in the dispatch path, contradicting the README's own quickstart.
# These tests build real PGP ciphertext (via the session-scoped keypair
# fixtures in conftest.py) and prove the fix, alongside the open-rooms
# plaintext pass-through the same code path now also has to handle
# correctly without regressing the crypto case.
from __future__ import annotations

import json

import httpx
import pytest

from saltapp import crypto
from saltapp.agent import Agent
from saltapp.client import AsyncSaltClient
from saltapp.webhook import Event

HOST = "https://example.saltapp.test"


def make_agent(keypair, handler=None, *, agent_id="self-agent") -> Agent:
    def default_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"session": {"users": []}})

    transport = httpx.MockTransport(handler or default_handler)
    client = AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=transport))
    return Agent(
        host=HOST, api_key="key", public_key=keypair.public_key, private_key=keypair.private_key,
        passphrase="passphrase-a", agent_id=agent_id, client=client,
    )


def message_event(
    *, chat_id="c1", sender_id="human-1", sender_account_type="User", message,
    encrypted=None, mentions=None, chat_meta=None, delivered_because=None,
):
    body: dict = {
        "chat_id": chat_id,
        "message": message,
        "user": {"id": sender_id, "account_type": sender_account_type, "username": "someone"},
        "mentions": mentions or [],
    }
    if encrypted is not None:
        body["encrypted"] = encrypted
    if delivered_because is not None:
        body["delivered_because"] = delivered_because
    return Event(type="message", body={"message": body, "chat": chat_meta or {}})


@pytest.mark.asyncio
async def test_encrypted_message_is_actually_decrypted(keypair_a):
    """The bug: this used to hand the handler the raw PGP-armored
    ciphertext as ctx.text. It must now be the real plaintext."""
    agent = make_agent(keypair_a)
    ciphertext = crypto.encrypt_for("the real secret", [keypair_a.public_key])
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx.text)

    await agent.dispatch(message_event(message=ciphertext, encrypted=True))
    assert received == ["the real secret"]
    # It really was ciphertext, not accidentally already the plaintext.
    assert ciphertext != "the real secret"


@pytest.mark.asyncio
async def test_encrypted_defaults_true_when_field_absent(keypair_a):
    """An envelope shape that predates `encrypted` (or a hand-rolled test
    event) must still be treated as PGP ciphertext -- the safe default,
    matching every message before the column existed."""
    agent = make_agent(keypair_a)
    ciphertext = crypto.encrypt_for("still secret", [keypair_a.public_key])
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx.text)

    await agent.dispatch(message_event(message=ciphertext))  # no `encrypted` key at all
    assert received == ["still secret"]


@pytest.mark.asyncio
async def test_undecryptable_ciphertext_is_dropped_not_delivered_raw(keypair_a, keypair_b):
    """A message this agent's key genuinely can't open (encrypted to a
    DIFFERENT key here) must be dropped, never handed to on_message as
    garbage ciphertext -- and dispatch() must not raise out to the caller."""
    agent = make_agent(keypair_a)  # agent holds keypair_a's private key...
    ciphertext = crypto.encrypt_for("not for you", [keypair_b.public_key])  # ...but this is for keypair_b

    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx.text)

    await agent.dispatch(message_event(message=ciphertext, encrypted=True))
    assert received == []


@pytest.mark.asyncio
async def test_open_room_message_passes_through_as_plain_text_and_sets_ctx_encrypted(keypair_a):
    agent = make_agent(keypair_a)
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append((ctx.text, ctx.encrypted))

    await agent.dispatch(message_event(message="hello room", encrypted=False))
    assert received == [("hello room", False)]


@pytest.mark.asyncio
async def test_encrypted_room_sets_ctx_encrypted_true(keypair_a):
    agent = make_agent(keypair_a)
    ciphertext = crypto.encrypt_for("hi", [keypair_a.public_key])
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx.encrypted)

    await agent.dispatch(message_event(message=ciphertext, encrypted=True))
    assert received == [True]


@pytest.mark.asyncio
async def test_delivered_because_rides_through_to_ctx(keypair_a):
    agent = make_agent(keypair_a)
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx.delivered_because)

    await agent.dispatch(message_event(message="hi", encrypted=False, delivered_because="keyword"))
    assert received == ["keyword"]


@pytest.mark.asyncio
async def test_delivered_because_is_none_for_encrypted_delivery(keypair_a):
    agent = make_agent(keypair_a)
    ciphertext = crypto.encrypt_for("hi", [keypair_a.public_key])
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx.delivered_because)

    await agent.dispatch(message_event(message=ciphertext, encrypted=True))
    assert received == [None]


@pytest.mark.asyncio
async def test_ask_free_text_resolves_with_decrypted_plaintext(keypair_a):
    """The ask-registry free-text match reads from the SAME `text` the
    handler would get -- before this fix it was handed raw ciphertext,
    so AskResult.text would have been unreadable garbage for any caller
    using ctx.ask(free_text=True) in an encrypted chat."""
    agent = make_agent(keypair_a)
    waiter = agent._ask_registry.register_free_text("c1", None)

    ciphertext = crypto.encrypt_for("Dan", [keypair_a.public_key])
    await agent.dispatch(message_event(message=ciphertext, encrypted=True))

    assert waiter.event.is_set()
    assert waiter.result == {"kind": "text", "text": "Dan", "sender_id": "human-1"}


@pytest.mark.asyncio
async def test_reply_in_open_room_posts_plain_text_no_pgp(keypair_a):
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            posted["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "m1"})
        return httpx.Response(200, json={"session": {"users": []}})

    agent = make_agent(keypair_a, handler)
    received = []

    @agent.on_message
    async def on_message(ctx):
        received.append(ctx)
        await ctx.reply("hello back")

    await agent.dispatch(message_event(message="hi", encrypted=False))
    assert len(received) == 1

    assert posted["body"]["message"] == "hello back"  # plain text, not PGP armor
    assert posted["body"]["encrypted"] is False
    assert "sender_message" not in posted["body"]


@pytest.mark.asyncio
async def test_reply_in_encrypted_room_still_encrypts(keypair_a):
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            posted["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "m1"})
        return httpx.Response(200, json={
            "session": {"users": [
                {"id": "self-agent", "public_key": keypair_a.public_key},
                {"id": "human-1", "public_key": keypair_a.public_key, "account_type": "User"},
            ]}
        })

    agent = make_agent(keypair_a, handler)
    ciphertext = crypto.encrypt_for("hi", [keypair_a.public_key])

    @agent.on_message
    async def on_message(ctx):
        await ctx.reply("hello back")

    await agent.dispatch(message_event(message=ciphertext, encrypted=True))

    assert posted["body"]["message"] != "hello back"
    assert posted["body"]["message"].startswith("-----BEGIN PGP MESSAGE")
    assert "encrypted" not in posted["body"]  # send_message's shape, unchanged
