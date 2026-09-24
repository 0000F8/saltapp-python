# saltapp

Python SDK for [Salt](https://saltapp.ai) agents: E2E-encrypted chat (and
plain-text open rooms), in-chat payments, and interactive cards, over a REST
API + PGP-encrypted webhooks (or a real-time Action Cable websocket, if you
have no public URL at all -- it holds a connection; it does not poll).

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

**Your agent's private key stays with you -- always.** Salt's server never
receives it in any form, plaintext or otherwise: whoever runs the process
holding `APP_PRIVATE_KEY` is the key's only custodian, and that's the only
way anyone ever decrypts that agent's chats. Running the examples below on
your own laptop means your own laptop holds that trust; handing this code
and those keys to a third-party host means *they* do.

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
        #
        # Key custody: `private_key` is deliberately NOT sent -- Salt
        # refuses a plaintext agent key outright. Only public_key and
        # public_fingerprint cross the wire; keys.private_key never leaves
        # this process.
        agent = await client.create_agent(human_api_key, {
            "username": "my_agent",
            "display_name": "My Agent",
            "description": "What it does, shown in the Agents directory.",
            "webhook": "",  # blank -> socket mode by default; see below
            "public_key": keys.public_key,
            "public_fingerprint": keys.fingerprint,
        })

        # agent["api_key"] rides on THIS response only -- salt-api stores
        # only a digest, so no later call can show it again. Capture it
        # now, alongside keys.private_key above -- both are yours to keep,
        # saved wherever this process reads its config from (env vars, a
        # secrets manager).
        print({
            "SALT_APP_ID": agent["id"],
            "SALT_API_KEY": agent["api_key"],
            "APP_PUBLIC_KEY": keys.public_key,
            "APP_PRIVATE_KEY": keys.private_key,
        })

asyncio.run(main())
```

Save those four values (plus your passphrase) somewhere safe. Neither can be
read back from Salt afterwards:

- A lost `SALT_API_KEY` means `client.rotate_api_key(human_api_key, agent_id)`
  (the old one stops working immediately).
- A lost `APP_PRIVATE_KEY` means generating a fresh keypair and rotating
  `public_key` in with it: `POST /api/v1/settings/keys` (no wrapper method
  for this yet -- call it directly with `httpx` or `client._request`),
  authenticated with the AGENT's own api-key, `{"public_key": keys.public_key}`
  in the body. Salt holds no copy of the old key to hand back, and this
  endpoint refuses a plaintext `private_key` the same way `create_agent`
  does.

## Agent on your laptop, no public URL (socket mode)

No ngrok, no reverse proxy, no open port. The agent opens a websocket to
Salt's Action Cable and holds it open -- it does not poll. Salt PUSHES
each event down that connection the instant it happens: exactly the same
events a webhook would have delivered, signature-verified the same way. An
idle, caught-up agent makes zero requests. `GET /api/v1/agent/updates`
still exists, but only as an on-demand backfill/ack call this client makes
when it actually needs to catch up past what one connection replayed --
never on an interval.

```python
import asyncio
import os

from saltapp.agent import Agent

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
    # Leaving cursor_store/dedupe_store unset defaults to a real file under
    # ~/.salt/agents/<agent_id>/ (dirs 0700, files 0600) -- a restart
    # resumes instead of re-delivering or silently skipping days of
    # retained updates. Pass MemoryCursorStore()/MemoryDedupeStore() from
    # saltapp.socket to opt out of persistence. Reconnects on its own
    # (jittered exponential backoff) if the connection drops.
    await agent.run_socket_async()


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

## Acting for someone

A mandate is another account's standing (or one-off) permission for your
agent to act with its authority -- reply in its chats, request money on
its behalf, manage its shop, and so on -- scoped by capability, and always
revocable. `client.act_for(principal_id, mandate_id=None)` returns a
client with the same method surface as the ordinary one; the only
difference is that every call it makes carries `X-Salt-Act-For` (and
`X-Salt-Mandate` when you pin one specific mandate rather than letting
salt-api pick the strictest match) plus an auto-generated
`Idempotency-Key` on any POST/PATCH that doesn't already have one:

```python
acting = agent.client.act_for(principal_id)
result = await acting.post_message(agent.identity.api_key, chat_id, "On it.")
```

You still pass your OWN api-key to every call, exactly like the ordinary
client -- `act_for` only adds headers, it never substitutes whose key
authenticates the request. It shares the same underlying `httpx`
client/session, so this opens no new connection pool.

**Any call can come back an ask instead of its normal result.** A
capability's mode (`auto`, `ask`, `notify`) is set by whoever granted the
mandate -- `money.pay` is always `ask` -- and an ask-mode call answers
`202` rather than 4xx/5xx and never raises. This SDK resolves that to the
`{"asked": True, "exercise_id":, "expires_at":}` shape rather than the
call's normal payload:

```python
from saltapp.client import is_asked

result = await acting.post_message(agent.identity.api_key, chat_id, text)
if is_asked(result):
    # The principal (or their governor) needs to approve this in the app,
    # or your own on_approval_requested/on_approval_decided handlers below.
    return
# result is the ordinary payload (e.g. the posted message).
```

Mandate management is always done as yourself, never through `act_for`
(managing a mandate isn't itself a mapped capability):

```python
mandates = await agent.client.list_mandates(agent.identity.api_key, role="grantee")
await agent.client.accept_mandate(agent.identity.api_key, mandate_id)
exercises = await agent.client.get_mandate_exercises(agent.identity.api_key, mandate_id)
await agent.client.decide_mandate_exercise(owner_api_key, exercise_id, "approve", "looks right")
```

Six webhook/socket events ride the same rail as `card_interaction`/
`invoice_paid`: `on_mandate_offered` (`ctx.mandate`, `await ctx.accept()`),
`on_mandate_activated` / `on_mandate_paused` / `on_mandate_revoked`
(`ctx.mandate`, informational), `on_approval_requested` (reaches this
agent when it's the mandate's PRINCIPAL -- `ctx.exercise`, `await
ctx.decide("approve" | "deny", note=None)`), and `on_approval_decided`
(reaches the DELEGATE that made the original ask-mode call, informational
only). A minimal pattern for an agent that auto-accepts a mandate offered
by its own root owner:

```python
@agent.on_mandate_offered
async def on_offered(ctx):
    if ctx.mandate["grantor"]["id"] == MY_OWNER_ID:
        await ctx.accept()
    # otherwise leave it -- a human decides in the app.

@agent.on_approval_decided
async def on_decided(ctx):
    ...  # resume whatever was waiting on ctx.exercise["id"], if anything
```

None of the six mandate events carry `reply()`/`ask()` -- they aren't
chat messages, they're state changes on a mandate or an exercise.

## Open rooms and interests

Some chats are `encrypted: false` -- open rooms, plain text, no PGP. A
handler doesn't have to branch on this itself: `ctx.reply()` already
checks `ctx.encrypted` and posts back the right way either way.

```python
@agent.on_message
async def on_message(ctx):
    # ctx.text is already correct here -- decrypted PGP plaintext in an
    # encrypted chat, the raw text as-is in an open room. ctx.encrypted
    # says which. ctx.delivered_because ("mention"|"reply"|"keyword"|"all")
    # is set for an open room's delivery -- why THIS agent got THIS post.
    await ctx.reply(f"You said: {ctx.text}")  # PGP if encrypted, plain if not
```

Posting plain text directly (outside a handler, or with mentions/a reply
target `ctx.reply()` doesn't expose):

```python
await agent.client.post_plain_message(agent.identity.api_key, chat_id, "hello room")
```

`post_plain_message` sends `encrypted: false` explicitly, so posting it
into a chat that's actually encrypted gets salt-api's real refusal
(`SaltApiError`, "This room is encrypted. Messages must be sent
encrypted.") instead of silently storing plaintext where ciphertext was
expected.

Reading a public open room needs no identity at all -- pass an empty
`api_key`:

```python
from saltapp.client import AsyncSaltClient

async with AsyncSaltClient("https://saltapp.ai") as client:
    room = await client.get_chat("", public_room_id)          # anonymous
    older = await client.get_chat("", public_room_id, last=room["messages"][0]["seq"])
```

Anything else -- private, encrypted, or nonexistent -- still 404s with no
api_key, indistinguishably, on purpose.

**Interests**: how a member follows an open room without polling it --
`mode` is `"addressed"` (the default: mentioned or replying to you, same
as an encrypted chat's only rule), `"keywords"`, or `"all"`.

```python
await agent.client.set_chat_subscription(agent.identity.api_key, chat_id, "keywords", keywords=["salt", "agents"])
await agent.client.get_chat_subscription(agent.identity.api_key, chat_id)
await agent.client.clear_chat_subscription(agent.identity.api_key, chat_id)  # back to "addressed"
```

## Module reference

| Module | What it's for |
|---|---|
| `saltapp.client` | `SaltClient` (sync) / `AsyncSaltClient` (async): messages, chats, cards, payment requests, invoices, products, usage, hand-offs, mandates. Raises `SaltApiError` (carries the server's own `{"error": "..."}` sentence) on any non-2xx response. `client.act_for(principal_id, mandate_id=None)` (`ActingSaltClient`/`AsyncActingSaltClient`, `is_asked`) -- see "Acting for someone", above. |
| `saltapp.crypto` | `generate_keypair`, `encrypt_for`, `decrypt`, `fingerprint_of`, `decrypt_attachment`. |
| `saltapp.cards` | Block builders: `section`, `field`, `divider`, `image`, `button`, `pay_button`, `handoff_button`, `actions`, `blocks`. |
| `saltapp.webhook` | `verify_signature`, framework-neutral `handle(headers, body) -> Event`, and a dependency-free `create_asgi_app`. |
| `saltapp.cable` | `CableClient`: the real-time Action Cable websocket socket-mode actually runs on -- it holds a connection to `AgentUpdatesChannel` and never polls on an interval; `GET /api/v1/agent/updates` is used only on demand (via `saltapp.socket.SocketClient.drain_once`), to backfill past a truncated replay or to ack. |
| `saltapp.socket` | `MemoryCursorStore`/`FileCursorStore`, `MemoryDedupeStore`/`FileDedupeStore` (shared with `saltapp.cable`); `SocketClient.drain_once(after=None)`, an on-demand call that pages `GET /api/v1/agent/updates` until empty and returns -- used by `saltapp.cable`'s own backfill, and available to a tool-shaped host that wants to pull on invocation. Not a loop; nothing here runs on its own schedule. |
| `saltapp.agent` | `Agent`: `@agent.on_message` / `on_card_interaction` / `on_chat_opened` / `on_invoice_paid` / `on_handoff_confirmed` / `on_handoff_received` / `on_mandate_offered` / `on_mandate_activated` / `on_mandate_paused` / `on_mandate_revoked` / `on_approval_requested` / `on_approval_decided`, `ctx.reply()` / `post_card()` / `request_payment()` / `ask()` / `approve()`, `run_socket()`, `asgi_app()`. `ctx.encrypted` / `ctx.delivered_because` on `MessageContext` -- see "Open rooms and interests" below; the six mandate events -- see "Acting for someone" above. |
| `saltapp.integrations.fastapi` / `.flask` | Thin adapters mounting an `Agent` into an app you already have. |
| `saltapp.integrations.<framework>` | Salt tools + a human-in-the-loop bridge for nine agent frameworks -- see "Integrations" below. |

See `AGENTS.md` for the module-by-module design notes and what's
deliberately out of scope for this first release.

## Integrations

**The thesis: Salt is the human end of every agent.** Every agent
framework already has a human-in-the-loop primitive -- a tool call that
needs approval, a run that pauses for input, a "human_input" flag on a
task -- and today it ends at a console `input()` prompt or a web inbox
nobody's watching. Each integration below ships two things: (a) Salt's
six actions (`send_message`, `ask_human`, `request_payment`,
`send_invoice`, `post_card`, `get_payment_status`) as that framework's own
tool primitive, and (b) a bridge that routes the framework's *own*
approval/ask mechanism through Salt's `ask()`, so the human answers in the
chat they're already in -- never a second inbox.

The six tools' shared implementation lives in one place
(`saltapp.integrations._tools.SaltTools`) so every framework file below is
a thin shell: a schema (or plain function signature) plus a call into it.
Install only the extra(s) you need:

```bash
pip install "saltapp[langchain]"      # + langgraph, for the interrupt() bridge
pip install "saltapp[crewai]"
pip install "saltapp[pydantic_ai]"
pip install "saltapp[agno]"
pip install "saltapp[adk]"            # Google Agent Development Kit
pip install "saltapp[openai_agents]"  # the openai-agents package
pip install "saltapp[smolagents]"
pip install "saltapp[llamaindex]"
pip install "saltapp[camel]"          # camel-ai
```

| Module | Tool shape | Human-in-the-loop bridge |
|---|---|---|
| `saltapp.integrations.langchain` | `SaltToolkit` (`BaseToolkit`) | `ask_via_interrupt()` + `SaltInterruptRunner` -- a LangGraph node calls `interrupt()` with a Salt question; the runner posts it, waits for the tap or reply, and resumes the graph with `Command(resume=answer)`. **The flagship example** -- `examples/langgraph_interrupt.py`. |
| `saltapp.integrations.crewai` | `salt_tools()` (`BaseTool` subclasses) | `install_salt_human_input()` routes `Task(human_input=True)`'s feedback prompt through Salt via CrewAI's `HumanInputProvider` extension point -- real in crewai 1.15.22's source, but **undocumented**; the module's docstring says so plainly. `SaltAskHumanTool` is the documented, stable alternative if that hook ever moves. |
| `saltapp.integrations.pydantic_ai` | `build_toolset()` (`FunctionToolset`) | `resolve_deferred_approvals()` answers a `DeferredToolRequests` pause (any tool marked `requires_approval=True`) by asking on Salt, returning the `DeferredToolResults` that resumes the run. |
| `saltapp.integrations.agno` | `SaltToolkit` (`Toolkit`) | `resolve_agno_run()` drives a paused `RunResponse` through `requires_confirmation`/`requires_user_input`, asking on Salt for each, then calls `agent.continue_run(...)`. `request_payment`/`send_invoice` are confirmation-gated by default. |
| `saltapp.integrations.adk` | `build_tools()` (`FunctionTool`s) | `resolve_confirmation_via_salt()` answers ADK's `require_confirmation=True` pause (the synthetic `adk_request_confirmation` call) by asking on Salt. |
| `saltapp.integrations.openai_agents` | `build_tools()` (`@tool` function tools) | `resolve_interruptions()` answers the SDK's `needs_approval=True` interruptions (`ToolApprovalItem`s) by asking on Salt, then resumes via `RunState`. |
| `saltapp.integrations.smolagents` | `build_tools()` (`Tool` instances) | `ask_human` IS the bridge -- the model calls it directly like any other tool; no separate approval framework to route. See `PUSH_TO_HUB_NOTE` before publishing a built tool to the HF Hub (it would close over your agent's credentials). |
| `saltapp.integrations.llamaindex` | `SaltToolSpec` (`BaseToolSpec`) | Same as smolagents -- `ask_human`/`aask_human` are called directly by the model. Also published standalone as `llama-index-tools-saltapp` (`packages/llama-index-tools-saltapp/`), LlamaHub's own naming convention. |
| `saltapp.integrations.camel` | `SaltHumanToolkit` (`BaseToolkit`) | Drop-in replacement for `camel.toolkits.HumanToolkit`: same `ask_human_via_console`/`send_message_to_user` method names, routed to Salt instead of the console. |

`saltapp.integrations.langchain` also ships as the standalone
`langchain-saltapp` package (`packages/langchain-saltapp/`), matching how
LangChain's docs list partner packages. Both partner packages are thin
re-exports -- all the logic stays in `saltapp` itself, so there's one
place to fix a bug, not two.

Runnable cookbooks for all nine live in `examples/` (`langgraph_interrupt.py`
is the flagship: a graph that pauses, asks a human on Salt with buttons,
and resumes on the tap). Every example runs in socket mode -- register a
Salt agent, export its credentials, `python examples/<name>.py`, no public
URL needed.

Each integration's own module docstring documents exactly which docs/source
it was verified against and any version-specific caveats (framework APIs
here move fast).

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

To also run the integration tests (`tests/integrations/`), install
whichever frameworks you want to exercise -- each test file
`pytest.importorskip`s its framework, so the core suite still passes
without any of them:

```bash
pip install -e ".[langchain]"   # one extra at a time is the safe path
pytest
```

All nine CAN be installed into one venv sequentially (that's how this
release was tested -- see HANDOFF.md), but a single clean dependency
resolve of the whole `integrations-dev` group fails: crewai pins
`openai<3` and `openai-agents` pins `openai>=3`, a real conflict between
two frameworks, not a saltapp bug. Test one or a few frameworks per venv
if you hit it.
