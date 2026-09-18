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
| `saltapp.integrations._tools` | New | `SaltTools`: the six shared actions (`send_message`/`ask_human`/`request_payment`/`send_invoice`/`post_card`/`get_payment_status`) every framework integration below wraps -- one place the business logic and the `ask_human` HITL primitive live. |
| `saltapp.integrations.{langchain,crewai,pydantic_ai,agno,adk,openai_agents,smolagents,llamaindex,camel}` | New | Nine framework integrations, each a thin shell over `_tools.SaltTools` -- see "Framework integrations" below. |

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

## Framework integrations (`saltapp.integrations.<framework>`)

Nine agent frameworks, each shipping (a) Salt's six tools as that
framework's own tool primitive and (b) a bridge routing the framework's
*own* human-in-the-loop mechanism through Salt's `ask()` (see
`_tools.SaltTools.ask_human`, which every bridge calls). Read
`README.md`'s "Integrations" section for the table of what each module
exposes; this section is about how they were BUILT and what to check
before trusting one.

- **Every API shape below was verified against the real, currently
  installed package** (as of 2026-09), not memory -- each module's own
  top-of-file comment names exactly which doc pages or source files were
  read. Agent-framework APIs move fast (LangGraph's `interrupt()` return
  shape and `MemorySaver`->`InMemorySaver` rename, the OpenAI Agents SDK's
  `function_tool`->`@tool` decorator shift, Agno's `updated_tools`->
  `requirements` migration all happened recently) -- re-verify against
  current docs before trusting an integration's shape without re-checking,
  don't assume this file is still current a few months out.
- **`_tools.py`'s `LineItemInput` pydantic model matters.** `send_invoice`'s
  `line_items` parameter is typed `list[LineItemInput]`, not
  `list[dict[str, Any]]`, specifically because the OpenAI Agents SDK's
  strict-schema mode refuses a bare dict (`additionalProperties` isn't
  allowed there) -- this was caught by actually running `build_tools()`
  against a real `agents.Agent`, not by inspection. If you add a new tool
  parameter that's a list of records, use a pydantic model the same way,
  or you'll reintroduce the same failure the moment someone tries
  `saltapp.integrations.openai_agents`.
- **CrewAI's `human_input=True` bridge is built on an undocumented
  extension point** (`crewai.core.providers.human_input.set_provider`,
  verified present in crewai 1.15.22's actual source, absent from its
  public docs). `saltapp.integrations.crewai.install_salt_human_input()`
  raises a clear `ImportError` (not a confusing `AttributeError`) if a
  future CrewAI version removes it, pointing at `SaltAskHumanTool` as the
  documented, stable fallback. Don't upgrade crewai in this repo without
  re-checking that hook still exists.
- **Testing all nine in one venv hits real cross-framework dependency
  conflicts** -- crewai pins `openai<3`, the `openai-agents` package pins
  `openai>=3`; `instructor` (a crewai dependency) pins `jiter<0.15`,
  `openai-agents` pulls `jiter>=0.17`. This release tested all nine by
  installing each extra with its own `pip install` call, in sequence, in
  one venv (each subsequent install's pins silently won over the
  previous, and no runtime behavior actually broke) -- a single combined
  `pip install --group integrations-dev` resolve FAILS outright on the
  conflict. Building google-re2 (a crewai transitive dependency, via
  cel-python) from source also failed on this machine's outdated Xcode
  toolchain (no prebuilt wheel for this exact arm64/cp312 combination);
  worked around by installing the last version with a prebuilt wheel
  (`google-re2==1.1.20240702` from a `--only-binary` download) before
  `pip install crewai`. See `HANDOFF.md` for the exact commands.
- **`saltapp.socket`/`saltapp.agent.run_socket_async()` still use the OLD
  socket-mode contract** (`poll_timeout=25` default, matching the K2
  contract as it stood when the core SDK (lane `python`) was built).
  `design-fleet/runs/2026-09-17-distribution/LANES.md` was revised
  2026-09-18 (after this integrations lane started) to a short-poll
  contract: `timeout` clamped server-side to 0..2s, Action Cable as the
  primary push path, polling only as backlog catch-up. This still WORKS
  (the server just clamps the requested 25s down to its real ceiling), but
  every example in `examples/` and the README's socket-mode snippets
  should move to the revised contract's adaptive-polling shape once the
  `socket` lane's SDK-side change lands -- don't take `poll_timeout=25` in
  this repo as the current recommendation.

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

`tests/integrations/` adds 36 more (117 total) against each framework's
REAL classes -- `pytest.importorskip` at the top of each file means they
no-op cleanly if you haven't installed that framework's extra, so the core
81 keep passing on their own. Every HITL bridge test drives the actual
async round trip: it starts the bridge as a background task/thread, waits
briefly, resolves the pending `saltapp.agent.Agent._ask_registry` entry
the same way a real card tap or chat message would, then asserts on the
resumed value in THAT framework's own expected shape (a `RunState`, a
`DeferredToolResults`, a `types.FunctionResponse`, ...) -- see
`tests/integrations/conftest.py`'s `make_agent`/`recording_card_handler`
helpers, reused from `tests/test_ask.py`'s pattern. Install one or a few
frameworks' extras (see the "Framework integrations" section above for why
not all nine at once) before running these.

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
- **No integration was run against a live model + live salt-api together.**
  Every integration test drives the framework's REAL classes but a FAKE
  model (pydantic-ai's `FunctionModel`, the OpenAI Agents SDK's
  `ScriptedModel`, hand-built fakes for Agno's paused `RunResponse`) and a
  mocked Salt transport -- proving the wiring is correct, not that
  `examples/adk_confirmation.py` actually works end-to-end against Gemini
  and a running `salt-api`. Before calling any one integration
  production-ready, run its example by hand per the "UAT steps" in
  `HANDOFF.md`.
- **The socket-mode contract examples/README use is one revision behind**
  -- see this file's "Framework integrations" section's last bullet.
  `saltapp.socket`/`Agent.run_socket_async()` themselves are untouched by
  this lane (out of scope: that's the `socket` lane's SDK-side code to
  revise), but every `examples/*.py` file and the README's socket-mode
  snippets should move to the revised adaptive-polling shape once that
  lane's change lands here.
- **`README.md`'s "Custody" quickstart still shows `create_agent(...,
  {"private_key": keys.private_key, ...})`** -- flagged in
  `design-fleet/runs/2026-09-17-distribution/FOLLOWUPS.md` ("READMEs that
  still describe server-held keys") by the separate `custody` lane, which
  is changing salt-api to refuse a private key over the wire at all. This
  integrations lane did NOT touch that section: the exact new
  registration contract depends on the custody lane's still-in-progress
  server change, which wasn't available to verify against. Whoever lands
  the custody lane's change should update that quickstart in the same
  pass (and note it doesn't affect anything in `saltapp.integrations.*` --
  every integration and example here builds an `Agent` from
  already-issued credentials, never calls `create_agent`).
