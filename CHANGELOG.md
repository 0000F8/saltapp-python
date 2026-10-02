# Changelog

## 0.4.2

**Agents can react to messages, sparingly.** `client.react(api_key, message_id, emoji)` (sync and async; a toggle, the same emoji again removes yours) and `my_reactions(api_key)`; `ctx.react(emoji)` and `ctx.message_id` on `MessageContext`. The server's 422 sentences ("Pick a single emoji.", "You can react with up to 12 emoji.") arrive as `SaltApiError`. The owner's rule, in the README and the docstrings: react "not all the time, just when they choose", and only "if it relevantly complements the chat in a friendly way"; never instead of answering, never every message, never your own, at most one per message. Matches salt-agent-sdk 0.16.0. Not published.

## 0.4.1

**Keys made by openpgp.js 6, the agent asks first, quieter.** openpgp.js 6 lists SHA3 (ids 12, 14) among a key's preferred hashes and PGPy 0.6 refused to parse such a key, so a Python agent could not encrypt for any chat member whose key v6 made; `saltapp.crypto` now accepts those two ids (any other unknown id still raises). `Agent.open_chat_with(handle)` and `SaltToolkit.for_human(agent, handle)` let an agent open the 1:1 and ask first; `examples/langchain_ask_first.py` runs with no LLM key. PGPy's `CryptographyDeprecationWarning` is filtered for module `pgpy` only. Install lines use `saltapp[extra] @ git+https://github.com/0000F8/saltapp-python` until the registry packages are published.

## 0.4.0

**Register without a human account, and the card says it was answered.** `register_agent` / `register_agent_async` register a root agent (`POST /auth`, versions from `GET /api/v1/config`) with a key pair generated locally, returning a `RegisteredAgent` (`agent`, `api_key`, `private_key`, `public_key`, `passphrase`, `identity`, `.build_agent()`). `ask()` now updates its card to "Answered: <answer>" once settled, best-effort, like the TypeScript SDK (a stranger saw the buttons stay live after tapping). `search_contacts` on both clients; `SaltApiError` carries the `status.message` sentence `/auth` refusals use. README leads with the GitHub install and the register → socket mode → ask quickstart.

## 0.3.5

**A refused cable handshake now says why.** `CableClient` logged only "[cable] reconnecting in 0.8s" when the server refused the websocket; it now logs `[cable] handshake failed: HTTP <status> (<reason>)` at warning level first. The refusal a stranger hit (production answered 404 to any handshake without an `Origin` header) is fixed server-side in salt-api 0.117.0, so no `Origin` is needed. A new test pins that a handler waiting on a later frame cannot stall pings or the answer.

## 0.3.4

**`ctx.ask()` with buttons could never receive an answer.** `_BaseContext.ask()` computed the card
id as `card.get("id") or card.get("card_id")`, but `POST /api/v1/cards` answers with the card's chat
bubble, which carries `message_id` and `resource_id` and neither of those keys — so the id was the
literal string `"None"` and every `ask()`/`approve()` offering buttons registered a waiter that no
real tap could ever match. It timed out silently, in production, through all nine framework
integrations built on `SaltTools` (LangChain, CrewAI, LlamaIndex, OpenAI Agents, Agno, ADK,
smolagents, pydantic-ai, camel-ai). Five test fakes answered with `{"id": card_id}` — the exact
shape the bug expected — which is why the suite was green. Now reads `resource_id` with a
`resource.id` fallback and raises if neither is present rather than degrading to a string that
cannot match; all five fakes corrected to the real envelope. `AGENTS.md` records the rule: generate
fakes from the API's real render, which since salt-api 0.98.3 is published as byte-real examples in
`https://saltapp.ai/api/openapi.json`.

All notable changes to `saltapp` are documented here. Dates are UTC.

## 0.3.3 - 2026-09-26

### Added

- **`SaltClient.get_card` / `AsyncSaltClient.get_card`** (`GET
  /api/v1/cards/:id`): a card's OWNER polls its own tap history instead
  of the agent's socket-mode outbox, which has exactly one forward-only
  cursor per agent -- two concurrent pollers (or one running beside a
  socket listener) can otherwise silently consume each other's answers.
  Returns `{id, state, owner_id, interactions}`; a "pay" tap's
  interaction carries a live `transfer_request_status`, not a snapshot
  from tap time. Takes an optional `after` (another interaction's id or
  an ISO 8601 timestamp) to page forward; an unrecognised value fails
  open (the full list, still 200) rather than raising. 404 (never 403)
  for anyone but the owner, byte-identical to an unknown id.
- **`SaltApiError.retry_after`**: the `Retry-After` response header
  (whole seconds, as Rack::Attack always sends it), parsed onto every
  raised `SaltApiError`; `None` when the response carried none.

## 0.3.2 - 2026-09-26

CI has been fully red since 2026-09-18: every integration test file
imported its framework before its own `pytest.importorskip()` guard,
so a CI job installing only one extra crashed collecting the other
eight instead of skipping them -- meaning nothing in this repo,
including the core suite, had actually run in CI for over a week.
Fixing that (a separate commit, not versioned on its own) surfaced
three further, previously-invisible problems fixed here.

### Fixed

- **`ask_via_interrupt()`/`SaltInterruptRunner` (the langchain/langgraph
  human-in-the-loop bridge) now refuse clearly on Python 3.10, instead
  of failing unpredictably.** langgraph's `interrupt()` reads its run's
  config through a mechanism that has to survive a hop into a
  background thread whenever the calling node is a plain function --
  which Python's asyncio/contextvars can't reliably do before 3.11.
  langgraph has its own guard for this, but that guard's own error is
  silently swallowed by an unrelated `except` clause (confirmed
  upstream, still open: langchain-ai/langgraph#8203), so instead of a
  clear message, calling code got an unpredictable native error. Salt's
  SDK now raises its own clear, actionable version check before ever
  reaching that broken code. `SaltToolkit`, the rest of this
  integration, is unaffected and still works on Python 3.10.
- **`import saltapp` works again on Python 3.13.** PGPy (a hard
  dependency, used for message encryption) does `import imghdr` at
  module scope; `imghdr` was removed from the stdlib by PEP 594 in
  Python 3.13, so `import saltapp` raised a bare `ModuleNotFoundError:
  No module named 'imghdr'` with no saltapp code involved and PGPy has
  no newer release fixing it. Added `standard-imghdr` (the official
  python-deadlib backport) as a dependency, installed only on Python
  3.13+.
- **`pip install "saltapp[camel]"` installs a working camel-ai
  integration again.** camel-ai 0.2.90's own `mcp>=1.3.0` is unbounded,
  and `mcp` 2.0.0 removed `FastMCP`, which `camel.toolkits.base` imports
  at class-definition time -- `from camel.toolkits import BaseToolkit`
  raised `ImportError: cannot import name 'FastMCP' from 'mcp.server'`.
  camel-ai's own maintainers confirmed the fix is capping
  `mcp>=1.3.0,<2` (camel-ai/camel#4293), but only shipped it in a
  0.2.91 pre-release so far; the `camel` extra now caps `mcp<2` itself
  until a stable camel-ai release carries that cap.
- **Every `saltapp.integrations.<framework>` test file now skips
  cleanly when its framework extra isn't installed**, instead of
  crashing collection for every other framework's tests in the same
  run.

## 0.3.1 - 2026-09-26

### Fixed

- **`pip install "saltapp[smolagents]"` now actually imports.** Every
  `saltapp.integrations.<framework>` module shares
  `saltapp.integrations._tools`, which does `from pydantic import
  BaseModel, Field` unconditionally at module scope (used by the
  frameworks whose tool primitive wants an explicit args model --
  LangChain, CrewAI, Pydantic AI). smolagents doesn't use that path (it
  builds tools via `build_plain_functions()`/type-hint introspection
  instead) and, uniquely among the nine integration extras, its own
  dependency chain (`huggingface-hub`, `jinja2`, `pillow`,
  `python-dotenv`, `requests`, `rich`) never pulls pydantic in --
  every other extra's framework package does. So a clean venv with only
  the `smolagents` extra installed raised `ModuleNotFoundError: No
  module named 'pydantic'` on `from saltapp.integrations.smolagents
  import build_tools`, with no signal at install time. `smolagents`'s
  extra now pins `pydantic>=2.0` directly. Found while building the
  smolagents Hugging Face Space example (`examples/hf-space/`).

## 0.3.0 - 2026-09-23

Mandates R2 ("Acting for you"), mirroring salt-agent-sdk 0.12.0.

- **`SaltClient.act_for(principal_id, mandate_id=None)` /
  `AsyncSaltClient.act_for(...)`** return an `ActingSaltClient` /
  `AsyncActingSaltClient` -- a subclass sharing the base client's
  `httpx` session, same method surface, overriding only `_request` to
  send `X-Salt-Act-For` (+ `X-Salt-Mandate` when pinned) and an
  auto-generated `Idempotency-Key` on every POST/PATCH that doesn't
  already have one. The delegate's own api-key is still passed per call,
  exactly like the base client -- `act_for` only adds headers, it never
  substitutes whose key authenticates the request.
- **The `{"asked": True, "exercise_id":, "expires_at":}` shape.** An
  ask-mode mandate call answers `202 {"status": "asked", ...}` rather
  than raising -- both `SaltClient`/`AsyncSaltClient._request` now
  resolve that to `{"asked": True, "exercise_id":, "expires_at":}`
  (`saltapp.client.is_asked` narrows it). The base (non-acting) client is
  unaffected in practice: salt-api's ask/mandate resolver only runs when
  `X-Salt-Act-For` is present at all.
- **`client.prepare_transfer(...)`** (`POST /transfers/prepare`):
  `money.pay`'s mode is forced to `ask`, so this always resolves the
  asked shape, never a Transfer.
- **Mandate management**, always as yourself, never through `act_for`:
  `list_mandates`, `get_mandate`, `propose_mandate`, `update_mandate`,
  `accept_mandate`, `renew_mandate`, `pause_mandate`, `resume_mandate`,
  `revoke_mandate`, `get_mandate_exercises`, `get_open_mandate_exercises`,
  `decide_mandate_exercise`.
- **Six new `Agent` events**, same rail as
  `card_interaction`/`invoice_paid` (`saltapp.webhook.classify` extended
  to recognize them, so webhook, socket-drain and cable delivery all pick
  them up for free): `on_mandate_offered` (`MandateOfferedContext`:
  `.mandate`, `await .accept()`), `on_mandate_activated` /
  `on_mandate_paused` / `on_mandate_revoked` (`MandateLifecycleContext`:
  `.mandate`, informational), `on_approval_requested`
  (`ApprovalRequestedContext`: `.exercise`, `await .decide(decision,
  note=None)`), `on_approval_decided` (`ApprovalDecidedContext`:
  `.exercise`, informational only). None of the six carry
  `reply()`/`ask()` -- they aren't chat messages.

## 0.2.0 - 2026-09-22

Open rooms, interests, and a real-time Action Cable transport for socket
mode -- plus a genuine decrypt bug fix found while building the first of
those.

- **Fixed: an encrypted chat's message was never actually decrypted.**
  `Agent._handle_message` set `ctx.text` to the raw value of
  `message["message"]` with no call to `saltapp.crypto.decrypt()`
  anywhere in the dispatch path -- for any chat, not just open ones. A
  handler (and `ctx.ask(free_text=True)`'s answer) got the literal
  PGP-armored ciphertext string. Fixed; see `tests/test_agent_crypto.py`.
- **Open rooms**: `MessageContext.encrypted`/`.delivered_because`;
  `ctx.reply()` posts plain text (`client.post_plain_message`) instead of
  PGP when the chat is open. `client.get_chat` gained `last=` and now
  works with no `api_key` at all against a `public && !encrypted` room.
- **Interests**: `client.get_chat_subscription`/`set_chat_subscription`/
  `clear_chat_subscription` (`mode`: `addressed`|`keywords`|`all`) against
  `/api/v1/chats/:id/subscription`.
- **Socket mode is push, not poll -- nowhere, not even as a fallback.**
  New `saltapp.cable.CableClient` opens a real websocket to salt-api's
  Action Cable and stays connected; `Agent.run_socket_async()` now runs on
  it. `saltapp.socket.SocketClient`'s old adaptive poll-forever loop
  (`run()`, `poll_once()`, `ACTIVE_POLL_DELAY_SECONDS`/
  `IDLE_POLL_DELAY_SECONDS`) is deleted, not kept as a fallback -- a
  documented poll loop is still a poll loop. What's left is `SocketClient.
  drain_once(after=None)`: an on-demand call that pages `GET /api/v1/
  agent/updates` (`timeout=0`) until a page comes back empty, then returns
  and stops. `CableClient` reuses it for its own on-demand backfill
  (`replay_done.more` only) and coalesced ack, never on an interval; a
  tool-shaped host (a Langflow/Dify-style integration that only runs when
  invoked) can call `drain_once` directly the same way. Adds a new hard
  dependency, `websockets>=12.0`.

## 0.1.2 - 2026-09-22

Narrows the socket-mode signature tolerance from a week to the same ~300s
window the webhook path uses.

`SOCKET_SIGNATURE_TOLERANCE_SECONDS` was `RETENTION + 1h` (7 days plus an
hour), on the reasoning that an outbox row can sit unpolled for days, so its
signing timestamp would routinely look stale. That reasoning stopped being
true when salt-api moved to **serve-time signing** (LANES.md "fix A"): an
envelope is re-signed with the agent's current webhook secret at the moment
it is served, so what a client receives is always freshly stamped —
measured at ~1.1s against the live production gate on 2026-09-22, not days.
The week-wide window bought nothing and cost real replay resistance. The
constant keeps its name; adapters import it by name.

The test that pinned the old behaviour is replaced by two that pin the real
contract: an old outbox row signed at serve time is accepted, and an
envelope whose signature is genuinely an hour old is rejected.

## 0.1.1 - 2026-09-18

Aligns `saltapp.socket`/`saltapp.agent`'s ask/approve with the socket-mode
contract as revised by a 2026-09-18 security review
(`design-fleet/runs/2026-09-17-distribution/LANES.md`), and fixes a
documentation gap around agent key custody. Merges the `integrations`
branch (nine agent-framework integrations) into `main`.

### Changed

- **Socket mode is now an adaptive short poll, not a long poll.**
  `GET /api/v1/agent/updates`'s `timeout` default drops from 25 to 2
  seconds (matching the server's own clamp -- Action Cable, not this
  endpoint, is the real push path). `SocketClient.run()`/
  `Agent.run_socket_async()` poll adaptively: 1 second right after real
  activity, backing off one step at a time to 5 seconds when idle,
  snapping back the moment something arrives.
- **Verification tolerance widened for socket mode.**
  `SOCKET_SIGNATURE_TOLERANCE_SECONDS` (retention, 7 days, plus an hour of
  slack) replaces the webhook path's 300-second default for envelopes
  arriving over the socket -- an outbox row can legitimately sit unpolled
  for days before a client ever sees it. Replay protection no longer
  leans on the timestamp at that width: it now comes from the cursor plus
  a persistent per-agent delivery-id dedupe set.
- **A transient verification failure no longer advances the cursor.** If
  fetching the signing secret fails (a network blip, not a bad signature),
  `SocketClient.poll_once()` halts processing the current batch right
  there and retries with backoff, instead of skipping the row forever.
- **The default cursor and dedupe stores are now file-based**, at
  `~/.salt/agents/<agent_id>/cursor.json` and `.../seen.json`
  (directories mode 0700, files mode 0600), rather than in-memory --
  `Agent.run_socket_async()` no longer silently loses its place on every
  restart. Pass `saltapp.socket.MemoryCursorStore()`/`MemoryDedupeStore()`
  explicitly to opt back into memory-only (always do this in a test).
- **`ctx.ask()`/`ctx.approve()` now name one answerer.** `ask()` restricts
  itself to a single expected answerer -- defaulting to whoever triggered
  the enclosing context (`MessageContext`'s sender, `ChatOpenedContext`'s
  opener, `InvoicePaidContext`'s buyer) when that party isn't itself an
  agent -- posts `restricted_to: [answerer]` on every button, and ignores
  any tap or typed reply from an agent or from anyone else. `approve()`'s
  typed-reply match is now the exact `^(y|yes)[.!]?$` (case-insensitive);
  "yeah"/"yes please" no longer count.
- `SaltClient.get_agent_updates`/`AsyncSaltClient.get_agent_updates`'s
  `timeout` parameter now defaults to 2 (was 25), matching the server's
  clamp.

### Fixed

- **Agent private keys never cross the wire.** The README's registration
  quickstart no longer sends `private_key` to `create_agent(...)` --
  Salt refuses a plaintext agent key outright. Only `public_key` and
  `public_fingerprint` are sent; the private key stays with whoever runs
  the process. Documented the rotation path for a lost key:
  `POST /api/v1/settings/keys`, authenticated with the agent's own
  api-key.

### Added

- `saltapp.socket.MemoryDedupeStore`/`FileDedupeStore`,
  `saltapp.socket.default_state_dir`.
- `.github/workflows/ci.yml`: pytest across Python 3.10-3.13, with each
  framework integration extra installed and tested in its own matrix job
  (crewai and openai-agents pin conflicting `openai` versions, so they can
  never share one job).

## 0.1.0 - 2026-09-18

Initial release: `saltapp`, the Python SDK for Salt agents (parity with
`salt-agent-sdk`'s essentials -- webhook/socket receiving, PGP
encrypt/decrypt, a typed REST client, card builders, `Agent` with
`ask()`/`approve()`), plus nine agent-framework integrations
(`saltapp.integrations.{langchain,crewai,pydantic_ai,agno,adk,openai_agents,smolagents,llamaindex,camel}`)
and two standalone partner packages (`langchain-saltapp`,
`llama-index-tools-saltapp`).
