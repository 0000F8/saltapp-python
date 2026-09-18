# AGENTS.md

Notes for an agent (human or AI) working on this repo next.

## What this is

`saltapp` is the Python SDK for [Salt](https://saltapp.ai) agents. It is
the Python counterpart to
[`salt-agent-sdk`](https://github.com/0000F8/salt-agent-sdk) (TypeScript,
sibling repo at `../salt-agent-sdk` in the workspace) and reuses the proven
pieces of `../salt-call-agent-example` (a private Python reference agent):
`webhook_auth.py`'s HMAC scheme ported into `saltapp/webhook.py`,
`pgp_bundle.py`'s pgpy idioms informing `saltapp/crypto.py`, and
`salt_client.py`'s httpx client shape informing `saltapp/client.py`.

Read `/Users/z1ggy/projects/salt/CLAUDE.md`'s "salt-api (Rails backend)"
section for the server-side ground truth this SDK talks to -- especially
the Agents, Webhook delivery, and Card protocol paragraphs. This file
assumes you've read that.

## Module map

| Module | Ported from / based on | What it does |
|---|---|---|
| `saltapp.identity` | `salt-agent-sdk/src/identities.ts` (simplified) | One `Identity` dataclass: api_key, PGP keypair, passphrase, agent_id. |
| `saltapp.errors` | `client.ts`'s `SaltApiError` | `SaltApiError` (server refusals), `SaltAppError` (base for everything else). |
| `saltapp.crypto` | `crypto.ts` + `pgp_bundle.py` idioms | Keygen (EdDSA/Ed25519 + ECDH/Curve25519, matching openpgp.js's `{type:"ecc",curve:"curve25519"}`), multi-recipient encrypt, decrypt, fingerprint, attachment AES-GCM decrypt. |
| `saltapp.cards` | `card.rb`'s validator (salt-api) + `actions.ts`'s card builder comments | Block builders: `section`/`field`/`divider`/`image`/`button`/`pay_button`/`handoff_button`/`actions`/`blocks`, with the same limits (`MAX_BLOCKS` etc.) as the server validator. |
| `saltapp.client` | `client.ts` + `salt_client.py` | `SaltClient` (sync) / `AsyncSaltClient` (async): messages, chats, cards, payment requests, invoices, products, usage, hand-offs. |
| `saltapp.webhook` | `webhook.ts`'s signature check + `webhook_auth.py` | `verify_signature`, `handle()` (framework-neutral), `create_asgi_app` (zero-dependency ASGI). |
| `saltapp.socket` | New (K2 contract in `design-fleet/runs/2026-09-17-distribution/LANES.md`) | `SocketClient`: long-polls `GET /api/v1/agent/updates`, verifies every envelope the same way a webhook is verified, backs off on transport errors. |
| `saltapp.agent` | `webhook.ts`'s dispatch logic (mention rule, loop guard, GACM, dedupe) + this SDK's own `ask()`/`approve()` | `Agent`: decorators, dispatch, `run_socket()`, `asgi_app()`. |
| `saltapp.integrations.fastapi` / `.flask` | New | Thin adapters mounting `Agent` into an app you already have. |

## Deliberate scope decisions (read this before "fixing" a gap)

The TypeScript SDK is considerably larger (`identities.ts`, `reconcile.ts`,
`delegations.ts`, `sessions.ts`, `work.ts`, `actions.ts` -- 17 agent-facing
tools, multi-identity hosting, hand-off session notes, delegation
provenance trails, work-report streaming). This SDK's brief was parity with
**the essentials**, not a line-for-line port. Specifically NOT built here,
on purpose:

- **One identity per `Agent`.** The TS SDK's `IdentityStore` lets one
  process host several spawned agents and resolves incoming ciphertext by
  trial-decrypting against every hosted identity. `saltapp.agent.Agent`
  hosts exactly one. If you need to host several Python-side identities in
  one process, run several `Agent` instances (each with its own
  `asgi_app()` mounted at a different path, or each running its own
  `run_socket()` task) rather than extending `Agent` into a registry --
  that keeps the mention-rule/loop-guard state (which is per-identity)
  simple and per-instance.
- **No delegation/hand-off provenance trail, no sessions, no work
  reporter.** `on_handoff_confirmed`/`on_handoff_received` fire with the
  raw event body; there's no `[[SALT-SESSION-NOTE]]` parsing, no
  `report_progress`/`[[SALT-WORK ...]]` wire format, no `delegate_to_agent`
  depth tracking. A caller who needs these can read `saltapp.webhook.Event.body`
  directly (it's the same JSON shape the TS SDK parses) and implement the
  wire markers itself, or reach for the TS SDK.
- **No `actions.py`/tool-calling wrapper.** The TS SDK's `createActions()`
  wraps 17 platform capabilities as JSON-Schema tool definitions for an
  LLM's function-calling loop. This SDK gives you the same underlying REST
  calls (`saltapp.client`) and lets you wire them into whatever tool-calling
  shape your own model client wants -- there's no `toAnthropicTools`/
  `toOpenAITools` equivalent here yet.

## `ctx.ask()` / `ctx.approve()` -- built without a new server endpoint

`design-fleet/runs/2026-09-17-distribution/page/narrative.html`'s "K3 · ASK"
kernel piece describes a *planned* `ask(chat, question, {options|free_text,
timeout}) -> answer` primitive backed by "a thin server-side `asks`
resource so a caller in socket mode can wait." **That server-side resource
does not exist yet** -- `LANES.md` (the contract file for this build round)
only specifies the K2 socket-mode contract, not a K3 `asks` API. Rather than
guess at an unbuilt endpoint's shape, `saltapp.agent`'s `ask()`/`approve()`
are built entirely on primitives that already exist and are documented in
the workspace `CLAUDE.md`: post a card (the question), then correlate
whichever comes back first -- a `card_interaction` on that card, or (if
`free_text=True`) a plain chat message -- via an in-process registry
(`saltapp.agent._AskRegistry`). No new server endpoint, no assumption about
one. If/when a real `asks` resource ships server-side, `ask()`'s public
signature (`options`, `free_text`, `timeout` -> `AskResult`) is designed to
stay the same while its *implementation* moves onto that resource --
revisit this file's note if that lands.

Concurrency note: `ask()`'s wait is implemented with a `threading.Event`
awaited via `asyncio.to_thread`, specifically so it does not block the
event loop `Agent.run_socket_async()`/`asgi_app()` run on -- the socket
poller keeps long-polling (and can deliver the very update that resolves
the ask) while a handler is suspended inside `ctx.ask()`. `SocketClient.run()`
schedules each event's dispatch as its own `asyncio.create_task`, not an
inline `await`, for the same reason.

## The webhook-signature test vector

`tests/fixtures/webhook_signature_vector.json` is generated by
`scripts/generate_ts_vector.cjs`, which does NOT just recompute an HMAC in
Node and call it a day -- it spins up the REAL compiled TypeScript SDK
(`../salt-agent-sdk/dist/webhook.js`'s `createWebhookServer`), POSTs signed
requests at it over a real HTTP server on an ephemeral port, and records
that SDK's own accept/reject verdict (its literal HTTP status code) for
each vector. `tests/test_webhook_vector.py` then asserts `saltapp.webhook`
agrees with every recorded verdict. If you touch the signature scheme in
either SDK, regenerate: `node scripts/generate_ts_vector.cjs` (requires
`../salt-agent-sdk` to have a built `dist/` -- `npm run build` there first
if it doesn't).

Each vector carries `now_unix`, the instant the generator captured right
when building that vector's signature; the Python test replays verification
pinned to that instant (`verify_signature(..., now=vector["now_unix"])`)
rather than the real wall clock, because a `t=now` signature is only valid
for the 300s tolerance window -- a fixture that didn't pin `now` would
start failing on its own five minutes after being committed.

## Testing

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[fastapi,flask]"
pip install pytest pytest-asyncio build twine
pytest
python -m build
python -m twine check dist/*
```

All 81 tests are real assertions against either: real `pgpy` keys and
round-trip crypto (`test_crypto.py`), the TS-SDK-verified signature vector
(`test_webhook_vector.py`), `httpx.MockTransport`-backed request-shape
checks (`test_client.py`, `test_socket.py`), or direct exercises of
`Agent`'s dispatch/ask logic with a mock transport underneath
(`test_agent.py`, `test_ask.py`). None of them stub the thing they claim to
test.

## Things a future pass should look at

- **No real end-to-end smoke test against a live salt-api.** Everything is
  tested against `httpx.MockTransport` or the compiled TS SDK's signature
  logic; nobody has run this SDK against a running Rails server yet. Before
  calling this production-ready, register a real `SALT-...` test agent
  against a local `salt-api` and run through the README's socket-mode
  quickstart by hand.
- **`Agent` assumes handlers are `async def`.** The Flask adapter and
  `dispatch_sync()` both bridge into async via `asyncio.run()` per call,
  which is correct but not efficient (a fresh event loop per webhook POST
  under Flask). Fine for a reference/hobby-scale agent; revisit if someone
  wants Flask + high throughput.
- **No multi-identity hosting** (see above). Revisit if someone actually
  needs one Python process to host several spawned sub-agents the way the
  TS SDK's `IdentityStore`/`reconcile.ts` do.
- **`saltapp.webhook.classify()`'s shape-sniffing** mirrors `webhook.ts`'s
  dispatch but hasn't been checked against every real webhook payload
  salt-api sends (billing/call events aren't in the six families this SDK
  names) -- if salt-api adds a new webhook event family, check `classify()`
  still does something sane with it (falls through to `"unknown"`, which
  `Agent.dispatch()` currently just ignores).
