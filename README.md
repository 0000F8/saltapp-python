# saltapp

Python SDK for [Salt](https://saltapp.ai) agents: E2E-encrypted chat, in-chat
payments, and interactive cards, over a REST API + PGP-encrypted webhooks (or
a long-poll socket, if you have no public URL at all).

The package on PyPI is named `saltapp` (not `salt` -- that belongs to
[SaltStack](https://pypi.org/project/salt/)). Import it as `saltapp`.

This is the Python counterpart to
[`salt-agent-sdk`](https://github.com/0000F8/salt-agent-sdk) (TypeScript). It
covers the essentials with parity to that SDK's wire behavior -- header
names, HMAC signature format, encryption for every chat member's key, card
JSON -- but narrows scope in a few deliberate ways; see `AGENTS.md`.

## Install

```bash
pip install saltapp
# extras, only if you want an adapter for an app you already have:
pip install "saltapp[fastapi]"
pip install "saltapp[flask]"
```

Requires Python >= 3.10.

## Custody

**Your agent's private key stays with you.** Whoever runs the process
holding `APP_PRIVATE_KEY` can decrypt that agent's chats -- there is no way
around this, because Salt's server never has the key and never sees
plaintext. Running the examples below on your own laptop means your own
laptop holds that trust; handing this code and those keys to a third-party
host means *they* do.

## Quickstart: register an agent

You need a PGP keypair and an API key before you can send or receive
anything.

```python
import asyncio
from saltapp import crypto
from saltapp.client import AsyncSaltClient

async def main():
    keys = crypto.generate_keypair("a passphrase you'll reuse everywhere")

    async with AsyncSaltClient("https://saltapp.ai") as client:
        # human_api_key: create one from Account -> API keys after signing up.
        agent = await client.create_agent(human_api_key, {
            "username": "my_agent",
            "display_name": "My Agent",
            "description": "What it does, shown in the Agents directory.",
            "webhook": "",  # blank -> socket mode by default; see below
            "public_key": keys.public_key,
            "private_key": keys.private_key,
            "public_fingerprint": keys.fingerprint,
        })

        # agent["api_key"] rides on THIS response only -- salt-api stores
        # only a digest, so no later call can show it again. Capture it now.
        print({
            "SALT_APP_ID": agent["id"],
            "SALT_API_KEY": agent["api_key"],
            "APP_PUBLIC_KEY": keys.public_key,
            "APP_PRIVATE_KEY": keys.private_key,
        })

asyncio.run(main())
```

Save those values (plus your passphrase) somewhere safe. If you ever lose
`SALT_API_KEY`, there's no way to read it back out -- call
`client.rotate_api_key(human_api_key, agent_id)` to mint a new one.

## Agent on your laptop, no public URL (socket mode)

No ngrok, no reverse proxy, no open port. The agent long-polls Salt instead
of Salt POSTing to it -- exactly the same events a webhook would have
delivered, signature-verified the same way.

```python
import asyncio
import os

from saltapp.agent import Agent
from saltapp.socket import FileCursorStore

agent = Agent(
    host="https://saltapp.ai",
    api_key=os.environ["SALT_API_KEY"],
    public_key=os.environ["APP_PUBLIC_KEY"],
    private_key=os.environ["APP_PRIVATE_KEY"],
    passphrase=os.environ["PGP_PASSPHRASE"],
)


@agent.on_message
async def on_message(ctx):
    answer = await ask_whatever_model_you_want(ctx.text)
    await ctx.reply(answer)


async def ask_whatever_model_you_want(text: str) -> str:
    # Swap in Claude, GPT, a local model, a rules engine -- whatever you
    # want. saltapp has zero opinion about how you decide what to say.
    return f"You said: {text}"


async def main():
    await agent.ensure_identity()  # resolves agent_id + webhook_secret
    await agent.run_socket_async(cursor_store=FileCursorStore("./data/cursor.txt"))


if __name__ == "__main__":
    asyncio.run(main())
```

That's the whole thing (well under 25 lines of actual logic). Run it, send
the agent a message from the Salt app, and it replies -- no inbound network
access to your laptop required. `agent.run_socket()` is the same thing as a
plain blocking call if you don't want to manage your own event loop.

## Webhook mode (you have a public URL)

```python
import uvicorn
from saltapp.agent import Agent

agent = Agent(host="https://saltapp.ai", api_key=..., public_key=..., private_key=..., passphrase=...)


@agent.on_message
async def on_message(ctx):
    await ctx.reply(f"You said: {ctx.text}")


if __name__ == "__main__":
    # Point the agent's webhook (Account -> Your agents -> Edit, or
    # client.set_callback) at this process's public URL, then:
    uvicorn.run(agent.asgi_app(), host="0.0.0.0", port=8000)
```

`agent.asgi_app()` needs no framework at all -- it's a plain ASGI3 callable.
Already have a FastAPI or Flask app? Mount saltapp's routes into it instead:

```python
# FastAPI
from saltapp.integrations.fastapi import create_router
app.include_router(create_router(agent))

# Flask
from saltapp.integrations.flask import create_blueprint
app.register_blueprint(create_blueprint(agent))
```

## Asking a human something (`ctx.ask` / `ctx.approve`)

An agent can ask a real question in the chat and wait for the answer --
built on a card (the question) plus whichever comes back first, a tapped
button or a typed reply:

```python
@agent.on_message
async def on_message(ctx):
    if "delete everything" in ctx.text.lower():
        if await ctx.approve("Really delete everything?"):
            await ctx.reply("Done.")
        else:
            await ctx.reply("Cancelled.")
        return

    answer = await ctx.ask(
        "Which environment?",
        options=["staging", "production"],
        free_text=True,   # a typed answer also counts
        timeout=120,
    )
    if answer.kind == "option":
        await ctx.reply(f"Deploying to {answer.value}.")
    else:
        await ctx.reply(f"Got it: {answer.text}")
```

`ask()` raises `saltapp.AskTimeout` if nothing answers in time. `approve()`
is `ask()` with Yes/No buttons, returning a plain `bool`.

## Cards, payments, hand-offs

```python
from saltapp import cards

blocks = cards.blocks(
    cards.section(text="Coffee, $4.50?"),
    cards.actions([
        cards.pay_button("pay_coffee", "Pay", amount="4.50", currency="USDC"),
    ]),
)
await ctx.post_card(blocks, "Coffee, $4.50?")

# Or a plain payment request / invoice, off the same rail:
await ctx.request_payment(receiver_id=other_user_id, wallet_id=my_wallet_id, amount="4.50")
await agent.client.create_invoice(
    agent.identity.api_key, chat_id=ctx.chat_id, receiver_id=other_user_id,
    wallet_id=my_wallet_id, amount="9.00",
    line_items=[{"name": "Coffee", "qty": 2, "unit_price": "4.50", "subtotal": "9.00"}],
)

# Hand a conversation to another agent:
await agent.client.hand_off(agent.identity.api_key, ctx.chat_id, other_agent_id, "They asked about billing.")
```

## Module reference

| Module | What it's for |
|---|---|
| `saltapp.client` | `SaltClient` (sync) / `AsyncSaltClient` (async): messages, chats, cards, payment requests, invoices, products, usage, hand-offs. Raises `SaltApiError` (carries the server's own `{"error": "..."}` sentence) on any non-2xx response. |
| `saltapp.crypto` | `generate_keypair`, `encrypt_for`, `decrypt`, `fingerprint_of`, `decrypt_attachment`. |
| `saltapp.cards` | Block builders: `section`, `field`, `divider`, `image`, `button`, `pay_button`, `handoff_button`, `actions`, `blocks`. |
| `saltapp.webhook` | `verify_signature`, framework-neutral `handle(headers, body) -> Event`, and a dependency-free `create_asgi_app`. |
| `saltapp.socket` | `SocketClient` (long-poll per the socket-mode contract), `MemoryCursorStore`, `FileCursorStore`. |
| `saltapp.agent` | `Agent`: `@agent.on_message` / `on_card_interaction` / `on_chat_opened` / `on_invoice_paid` / `on_handoff_confirmed` / `on_handoff_received`, `ctx.reply()` / `post_card()` / `request_payment()` / `ask()` / `approve()`, `run_socket()`, `asgi_app()`. |
| `saltapp.integrations.fastapi` / `.flask` | Thin adapters mounting an `Agent` into an app you already have. |

See `AGENTS.md` for the module-by-module design notes and what's
deliberately out of scope for this first release.

## Links

- [saltapp.ai/developers](https://saltapp.ai/developers)
- [salt-agent-sdk](https://github.com/0000F8/salt-agent-sdk) (TypeScript)

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[fastapi,flask]"
pip install pytest pytest-asyncio build
pytest
python -m build
```
