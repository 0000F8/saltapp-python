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
| `saltapp.socket` | K2 contract in `design-fleet/runs/2026-09-17-distribution/LANES.md`, revised 2026-09-18 after a security review, then reduced to an on-demand primitive 2026-09-22 (owner, via team lead: "never polling" applies to a documented fallback too) | `MemoryCursorStore`/`FileCursorStore`, `MemoryDedupeStore`/`FileDedupeStore` (persistent, file-backed by default -- shared with `saltapp.cable`). `SocketClient.drain_once(after=None)`: pages `GET /api/v1/agent/updates` (`timeout=0`) until a page comes back empty, then RETURNS -- no loop, no interval, nothing runs on its own schedule. `Agent.run_socket_async()` doesn't use this at all (see `saltapp.cable`); `CableClient._backfill` calls `drain_once` for its own on-demand catch-up, and a tool-shaped host can call it directly each time it's invoked. |
| `saltapp.cable` | `salt-agent-sdk/src/socket.ts`'s `createSocketClient`, rewritten onto Action Cable 2026-09-22 (owner, via team lead: "DO NOT USE POLLING as a mechanic EVER: pull on demand, push on address") | `CableClient`: a real websocket to `wss://<host>/cable` (`AgentUpdatesChannel`, api-key handshake header), subscribe/replay/`replay_done`, live dispatch, on-demand backfill (`more: true` only) and a coalesced ack via `GET /api/v1/agent/updates` -- never on an interval. Reconnects with jittered exponential backoff (1s..60s), honours `Retry-After` on a 429 handshake, treats 30s without a `{type:"ping"}` as dead. Deliberately simpler than the TS reference in one place (no strict buffer-then-drain ordering during backfill -- correctness still holds via DedupeStore, see the module's header) and stricter in another (dispatch runs as its own `asyncio.create_task`, not awaited inline, because `ctx.ask()` can block a handler on a reply that can only arrive by this same loop continuing to run -- a primitive the TS SDK doesn't have yet). |
| `saltapp.agent` | `webhook.ts`'s dispatch logic (mention rule, loop guard, GACM, dedupe) + this SDK's own `ask()`/`approve()` | `Agent`: decorators, dispatch, `run_socket()` (now cable-backed), `asgi_app()`. `MessageContext.encrypted`/`.delivered_because` (open rooms, 2026-09-22); `_handle_message` actually calls `crypto.decrypt()` for an encrypted delivery now -- see the dated note below, this was a real bug. |
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
event loop `Agent.run_socket_async()`/`asgi_app()` run on -- the cable
connection keeps running (and can deliver the very update that resolves
the ask) while a handler is suspended inside `ctx.ask()`.
`saltapp.cable.CableClient.run()` schedules each event's dispatch as its
own `asyncio.create_task`, not an inline `await`, for the same reason --
see `saltapp/cable.py`'s header comment ("DELIBERATE STRENGTHENING") for
why that matters specifically because of `ctx.ask()`.

**M2 parity fix (security review, 2026-09-18)**: `ask()`/`approve()` now
name ONE expected answerer (`from_user_id`, defaulting to whoever
triggered the context -- `MessageContext`'s sender, `ChatOpenedContext`'s
opener, `InvoicePaidContext`'s buyer, each only when that party isn't
itself an agent), post `restricted_to: [answerer]` on every button
(enforced server-side too, `cards_controller#actions`/`Card#find_button`),
and `_AskRegistry` ignores any tap/reply from an agent outright plus any
reply from someone other than the named answerer. This mirrors
salt-agent-sdk's `ask.ts` M2 fix with one deliberate simplification: that
SDK keys a pending ask by (identity, chat) because its `IdentityStore` can
host several identities sharing one process/registry; `saltapp.agent.Agent`
hosts exactly one identity per instance and `_AskRegistry` is a per-`Agent`
attribute (see "One identity per `Agent`" above), so the identity
dimension is already the instance boundary -- adding it to the key would
just repeat information that boundary already carries. One real,
documented divergence: `saltapp.integrations.*`'s bare `tool_context()`
(no live triggering message behind it) has no natural default answerer,
so `ask()` there falls back to its old permissive behavior (any non-agent
member may answer) rather than raising synchronously the way `ask.ts`
does -- pass `from_user_id` explicitly through that path if you need the
restriction there too. `approve()`'s typed-reply match is now the exact
`^(y|yes)[.!]?$` (case-insensitive) -- "yeah"/"yes please" no longer
count; a tapped "Yes" button still matches by its own label, not this
regex.

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
- **`saltapp.socket`/`saltapp.agent.run_socket_async()` now match the
  REVISED socket-mode contract** (lane `py-align`, 2026-09-18, folding in
  the `socket` lane's security review): `timeout` defaults to 2 (the
  server clamps it to 0..2 regardless), polling is adaptive (1s after
  activity, backing off to 5s idle), verification tolerance is
  `SOCKET_SIGNATURE_TOLERANCE_SECONDS` (retention + 1h, not the webhook
  path's 300s), a transient verification failure halts without advancing
  the cursor, and the default cursor/dedupe stores are file-based under
  `~/.salt/agents/<agent_id>/` (0700 dirs, 0600 files) rather than
  in-memory. Every `examples/*.py` file and the README were updated to
  drop the old explicit `FileCursorStore("./data/..._cursor.txt")` in
  favor of the new default.
- **Open rooms, interests, and Action Cable (2026-09-22)**, from a survey
  of every Salt agent integration against three in-flight server changes:
  1. **A genuine decrypt bug, found and fixed.** `Agent._handle_message`
     used to set `ctx.text = message.get("message")` directly, raw --
     `saltapp.crypto.decrypt()` was never called anywhere in the dispatch
     path, for ANY chat, encrypted or not, contradicting the README's own
     quickstart. It happened to look like it worked because every test
     fixture's `text` was already plaintext. Fixed in `_handle_message`:
     `encrypted = bool(message.get("encrypted", True))` (default True --
     safe for an envelope shape that predates the field), and a real
     `crypto.decrypt()` call when it is, with a `CryptoError` dropping the
     message (logged) rather than handing a handler unreadable ciphertext.
     `tests/test_agent_crypto.py` is the regression suite, built on real
     PGP round trips via `conftest.py`'s `keypair_a`/`keypair_b` fixtures
     -- including proof that `ctx.ask(free_text=True)`'s answer used to be
     raw ciphertext too (same bug, same fix, one call site).
  2. **Open rooms.** `MessageContext.encrypted`/`.delivered_because`
     (`False`/set only for an open chat's delivery); `ctx.reply()` branches
     on `self.encrypted` to call `client.post_plain_message` instead of
     `client.send_message` when the chat is open. `client.
     post_plain_message` sends `encrypted: false` explicitly (not just
     "post whatever string" and hope) so misuse against a real encrypted
     chat gets salt-api's actual 422 refusal, not silently-stored
     plaintext. `client.get_chat` gained `last=` (catch-up cursor,
     `Api::V1::ChatsController#show`'s `?last=`) and now sends NO
     `api-key` header at all when `api_key` is falsy -- `_request` in both
     `SaltClient`/`AsyncSaltClient` was changed the same way -- which is
     what lets an anonymous caller read a `public && !encrypted` room (see
     `resolve_readable_chat` in salt-api).
  3. **Interests.** `client.get_chat_subscription`/`set_chat_subscription`/
     `clear_chat_subscription` against `/api/v1/chats/:id/subscription`
     (`mode`: `addressed`|`keywords`|`all`).
  4. **Action Cable, not polling** (owner, via team lead: "DO NOT USE
     POLLING as a mechanic EVER: pull on demand, push on address"): new
     `saltapp.cable.CableClient` is what `Agent.run_socket_async()` runs.
     A follow-up from the SAME owner ruling, once "kept `SocketClient` as
     a documented fallback" was pointed out to still BE a poll loop
     (2026-09-22, second pass): `SocketClient.run()` -- the adaptive
     poll-forever loop, and `ACTIVE_POLL_DELAY_SECONDS`/
     `IDLE_POLL_DELAY_SECONDS` -- is DELETED, not kept. What's left is
     `SocketClient.drain_once(after=None)`: pages `GET /api/v1/agent/
     updates` (`timeout=0` always) until a page comes back empty, then
     returns and stops -- one on-demand call, never a loop that decides to
     call itself again. `CableClient._backfill` reuses it (`self._drain`)
     for its own on-demand catch-up, the ONLY time it's invoked at all
     (`replay_done.more`, never on an interval); a tool-shaped host (runs
     only when invoked, never in the background) can call it directly the
     same way. `SocketClient.exhausted` (set by every `drain_once` call)
     tells a caller whether it actually reached empty or stopped early on
     a transient verification halt -- an empty `events` list alone can't
     say which. See the module map above and `saltapp/cable.py`'s/
     `saltapp/socket.py`'s own header comments for the full contracts, and
     `tests/test_cable.py`/`tests/test_socket.py` for coverage (no real
     network in any test here).
  5. **Not done, and why**: health-from-evidence work was N/A for this
     repo -- it never self-initiated `/health` polling to begin with
     (only ever served one passively), so there was nothing to change.

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
- **Resolved (lane `py-align`, 2026-09-18)**: the socket-mode contract and
  the README's "Custody" quickstart (which used to show `create_agent(...,
  {"private_key": keys.private_key, ...})`) are both now current -- see
  the "Framework integrations" section above and the README's Custody /
  Quickstart sections. `create_agent` sends only `public_key` +
  `public_fingerprint`; the private key never crosses the wire. Rotating a
  lost private key is `POST /api/v1/settings/keys` with the agent's own
  api-key (no wrapper method yet -- call it directly), documented in the
  README next to `rotate_api_key`.
- **No client-side wrapper for `POST /api/v1/settings/keys`** (PGP key
  rotation) yet -- the README documents calling it directly. Add a typed
  `SaltClient.rotate_keys(api_key, public_key)` if this comes up again;
  it wasn't added here to keep this pass's blast radius to what the task
  actually needed.
