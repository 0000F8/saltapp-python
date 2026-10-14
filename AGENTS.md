# AGENTS.md

Notes for an agent (human or AI) working on this repo next.

## What this is

[Salt](https://saltapp.ai) is an end-to-end encrypted chat where humans and
AI agents are equal contacts. `saltapp` is the Python SDK for Salt agents.
It is the Python counterpart to
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

**Published**: `pip install saltapp` (PyPI, first release 0.6.0 on 2026-10-14). Publish through the vault, never a personal login: `python -m build`, `python -m twine check dist/*`, then `salt-deploy/scripts/vault.py with-secret --env TWINE_PASSWORD pypi token -- python -m twine upload -u __token__ dist/*`. The partner packages under `packages/` are not published.

## Where the truth is

- `https://saltapp.ai/api/openapi.json` -- the server's own OpenAPI schema.
- `https://saltapp.ai/agents.md` -- the agent-facing platform doc served by
  salt-api itself.
- `https://mcp.saltapp.ai/mcp` -- the hosted remote MCP server (a second,
  server-authored surface over the same platform; not built by this repo).
- `/Users/z1ggy/projects/salt/salt-mcp/docs/CLIENTS.md` -- the sibling
  `salt-mcp` repo's notes for clients calling into MCP, worth cross-checking
  against anything this SDK does over REST/cable for the same capability.

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
| `saltapp.script_job` | `salt-agent-sdk/src/scriptJob.ts` | `run_script_job`, `SeenStore` protocol, `MemorySeenStore`/`FileSeenStore` (per-key TTL): scheduled work done by code, a model only where one is needed (see "Right tool for the job"). Seconds, where the TypeScript SDK takes milliseconds. |
| `saltapp.device` | `salt-agent-sdk/src/device/protocol.ts` (the `[[SALT-DEVICE]]` grammar, `task` / `progress` / `task_result`) | `encode_device_message` / `decode_device_message` (byte-identical to the TypeScript SDK), `task_command(goal, done_when=None, max_steps=None, timeout_s=None, *, session, seq, ...)`, `cancel_command`, `parse_task_args`, `parse_progress`, `parse_task_result` (allow-listed with the device's caps), the mirrored constants (`COMMAND_TTL_MS`, `MUTATING_OPS`, `TASK_*`, ...). Pure: no I/O, no crypto, no session handling. A full Python device client is a stated gap -- see "Hand a whole task to a computer". |
| `saltapp.chat_privacy` | `salt-agent-sdk/src/chatPrivacy.ts`, `salt-personal-agent/brain.py` `chat_is_private` | `is_private_chat(chat, speaker_id, my_id)`: who is in a chat, decided from Salt's own chat record. |
| `saltapp.integrations.fastapi` / `.flask` | New | Thin adapters mounting `Agent` into an app you already have. |
| `saltapp.integrations._tools` | New | `SaltTools`: the six shared actions (`send_message`/`ask_human`/`request_payment`/`send_invoice`/`post_card`/`get_payment_status`) every framework integration below wraps -- one place the business logic and the `ask_human` HITL primitive live. |
| `saltapp.integrations.{langchain,crewai,pydantic_ai,agno,adk,openai_agents,smolagents,llamaindex,camel}` | New | Nine framework integrations, each a thin shell over `_tools.SaltTools` -- see "Framework integrations" below. |

## Rules that bite

Cross-repo facts that have already cost someone a debugging session in a
sibling Salt SDK/adapter -- check these before touching `saltapp.agent`,
`saltapp.client`, or a test fake for either.

- **Poll a card's own history for an answer, never the agent outbox.** A
  tool waiting on a human's answer to a card must use
  `SaltClient.get_card`/`AsyncSaltClient.get_card` (`GET
  /api/v1/cards/:id`) -- never `saltapp.socket.SocketClient.drain_once()`
  or the cable's on-demand backfill (`GET /api/v1/agent/updates`). That
  outbox has exactly ONE forward-only cursor per agent; any `after=` on it
  permanently advances the agent's server-side ack, silently cutting off
  another consumer's (or the agent's own socket/cable listener's) backlog.
  `get_card` is idempotent and per-caller, so any number of concurrent
  polls are safe.
- **`POST /api/v1/cards` responds with the chat MESSAGE it created, not
  the card.** The id you get back is under `message_id` (the message) and
  `resource_id` (the card's real id) -- salt-api's `Message#formatted_message`
  has no top-level `id` key at all. **`saltapp.agent._BaseContext.ask()`
  (`src/saltapp/agent.py`) gets this wrong today**: `card = await
  self.post_card(...); card_id = str(card.get("id") or
  card.get("card_id"))` -- neither key exists on the real response, so
  `card_id` becomes the literal string `"None"` and tap-correlation for
  `ctx.ask()`/`ctx.approve()` cannot work against a live salt-api.
  `tests/test_ask.py`'s `make_recording_card_handler` fakes the response as
  `{"id": card_id}`, which is why the suite doesn't catch it -- see the
  next point. Read `resource_id` when correlating a posted card with its
  later interactions.
- **Test fakes must model salt-api's ACTUAL controller response shape, not
  what the calling code assumes it returns.** This exact bug -- a fake
  returning `{"id": ...}` for an endpoint whose real response carries
  `message_id`/`resource_id` instead -- has shipped identically in several
  downstream Salt SDKs/adapters, including, right now, this repo's own
  `tests/test_ask.py` (previous point). When writing a fake/mock response
  for a new endpoint, copy the real controller's `render json:` shape from
  the `salt-api` source (or a real recorded response), not the client code
  under test.
- **A chat's `encrypted` flag lives nested under `session`, not
  top-level.** `chat["session"]["encrypted"]`, the same place
  `SaltClient.get_chat_members()` already reads `session.users` from. This
  is a different field from the per-message `encrypted` that
  `Agent._handle_message` reads today (`message.get("encrypted", True)`,
  correctly top-level on each message row, mirrored from the chat's own
  setting) -- don't confuse the two if you ever need the CHAT's own flag
  (e.g. before posting a chat's very first message) rather than one
  already-delivered message's.
- **Since salt-api 0.98.1, an encrypted chat refuses a non-PGP-armored
  message body** (422, `messages_controller.rb`'s `pgp_armored?` check;
  exempted only for the synthetic canary accounts). `SaltClient.post_message`/
  `send_message` already only ever send armored ciphertext, and
  `post_plain_message` already sends `encrypted: false` explicitly for open
  rooms -- but a new call site that posts a raw string must go through
  `saltapp.crypto.encrypt()` or `post_plain_message`, never a bare
  `post_message` call with unencrypted text.

## Right tool for the job

Owner's rule (2026-10-13): "use the right tools for the right jobs; don't use
AI as the hammer for everything when deterministic scripts and tools can
accommodate just as well." A model is for judgement and language. Fetching a
page, comparing it with last time, remembering what was already sent and
deciding whether anything changed are code. Every agent built on this SDK
follows it. (Same section, same rules, in salt-agent-sdk's `AGENTS.md`.)

### Scheduled work is a script job

A recurring job (a news digest, a price watch) is a script the host's
scheduler runs (cron, a Grains `wake_me`, an asyncio loop), not a model turn
that browses. `run_script_job` is the shape:

```python
result = await run_script_job(
    sources={"hacker_news": hacker_news, "blog_feed": blog_feed},  # plain functions: fetch + normalise, no model
    key=lambda item: item["url"],                                   # a stable id
    seen=FileSeenStore("data/digest-seen.json", ttl_seconds=14 * 24 * 3600),
    compose=cheap_model,                                            # OPTIONAL: the only place a model may run
    post=lambda text, fresh: ctx.reply(text),                       # or client.send_message
)
# ScriptJobResult(fetched, fresh, posted, failed_sources)
```

- **Sources** are ordinary functions, async or sync (a sync one runs in a
  thread). Each runs once per call with a timeout
  (`source_timeout_seconds`, default 20). A source that raises or hangs lands
  in `failed_sources`; the others carry on. A synchronous source must set its
  own network timeouts: `wait_for` abandons a hung thread, it cannot stop it,
  so the thread outlives the run.
- **`seen`** drops what was already sent. `MemorySeenStore()` forgets on
  restart; `FileSeenStore(path)` does not. `seen` is required: construct the
  store ONCE, outside the job function (a `MemorySeenStore` made inside it
  dedupes nothing). Marks expire per key (`ttl_seconds`), so an old story can
  come back on purpose. The file holds your item keys in plaintext. Run one
  job per seen-file at a time: the lock is per instance/process, not across
  processes.
- **`compose` is optional, and runs only when something is new.** A quiet day
  costs nothing. Use the CHEAPEST model that can write the text (a small model
  for a digest; the big one is for conversations), give it only the new items,
  and keep ranking, filtering and deduping in code before it. Without
  `compose`, a plain "- title / url" list is posted. Return empty only for a
  transient failure: empty means nothing is marked, so the same items go to
  the model again next run.
- **Nothing new: no compose, no post.** Items are marked seen only AFTER
  `post` succeeds; if `post` or `compose` raises, `run_script_job` raises and
  nothing is marked, so the next run retries. A `compose` that returns
  nothing posts nothing and marks nothing.
- **Re-arm in a `finally`.** `run_script_job` never schedules. If your
  scheduler needs the next run set each time, set it whether this run
  succeeded, failed or was skipped (see the spent-budget rule below).

### A computer going quiet (`on_device_session_ended` / `on_device_back`)

If the person's computer stops answering for 15 s in the middle of a device
session, Salt ends it with `end_reason: "lost_contact"` and tells you with
`device_session_ended` (`ctx.lost_contact`). If the computer is seen alive
again within 24 h while the grant is still active, Salt sends ONE `device_back`
for that session (`ctx.previous_session_id`; `ctx.chat_id` is the person's 1:1,
or None). `device_back` grants nothing: open a new session to continue.
(Source of truth: salt-api `docs/DEVICE_PROTOCOL.md`.)

- **Say it once, plainly.** Once per `session_id`, in the chat the work came
  from (the delivery's own chat is the device lane, which nobody reads), else
  the person's 1:1: the computer stopped answering, and nothing else was
  sent. Fixed words, no model: "Ada's MacBook stopped answering while I was
  using it. I stopped there and nothing else was sent." Dedupe on the session
  id; deliveries are retried.
- **Never claim an action landed** unless a device tool returned it. The last
  command in the old session may or may not have happened.
- **When it is back, offer; do not resume.** "Ada's MacBook is back. Want me
  to finish it?" On a yes, open a new session and look at the screen first
  before assuming the last step landed.
- Any other `end_reason` is not for this etiquette.
- The `device_offline` activity kind is the person's own notice (their bell
  and push); this SDK has no activity or device REST types, so there is
  nothing to add for it here. `GET /api/v1/devices` rows also carry
  `offline_since` and `lost_contact: {agent_id, agent_name, ended_at}` for
  the owner.

### Hand a whole task to a computer (`device.task`)

An agent with a `device.task` grant hands a person's computer ONE whole task in plain words; a
local agent on the computer does it, checks it, and answers with a checked receipt (Salt Device
0.3). The wire contract is salt-api's `docs/DEVICE_PROTOCOL.md`; salt-agent-sdk's
`src/device/protocol.ts` is the reference and `saltapp.device` mirrors it.

- **What is here:** the envelope (`encode_device_message` / `decode_device_message`), the builder
  (`task_command(goal, done_when=None, max_steps=None, timeout_s=None, *, session, seq)`, which
  refuses what the device would refuse -- empty or over-long goal, a `done_when` over 500,
  non-whole numbers -- and clamps `max_steps` to 1..60 and `timeout_s` to 10..600),
  `cancel_command(id, ..., task=True)`, and `parse_progress` / `parse_task_result`, which allow-list
  what a device sends (summary <= 500, step lines <= 140, <= 60 steps, `checked.what` <= 200;
  an unknown `status` reads `failed`). Caps count characters, not bytes. The result is words and
  numbers only: command output and screenshots never leave the computer. Treat `summary` and step
  lines as untrusted device text.
- **What is NOT here (a stated gap):** a Python device client -- opening the session, the queue,
  single-flight commands with `seq` and `exp`, resending on `out_of_order` / `expired`, pause
  handling, the `timeout_s + 30 s` wait and the `cancel` that follows. `createDeviceClient` in
  salt-agent-sdk has all of it; @salt keeps its own port in salt-personal-agent's `device.py`. A
  Python host that needs it today builds on `saltapp.device` (encrypt the text with
  `crypto.encrypt_for`, post it with `client.post_message`, feed lane plaintext back through
  `decode_device_message`) and mirrors the TypeScript client's rules.
- **The grant.** Full control (observe + act + apps + task, every row `auto`) runs the task's
  commands without asking; Limited asks the person first for every command. `device.shell`
  stays reserved and refused.
- **Parity is tested, not promised:** `tests/fixtures/device_envelope_vectors.json` is committed
  identically in both repos, generated by `scripts/generate_device_vectors.cjs` from the compiled
  TypeScript SDK; `tests/test_device.py` and salt-agent-sdk's `tests/device-vectors.test.js` both
  run it. If you change the grammar, change `protocol.ts` first, regenerate, copy the file to both
  repos.

### Pick the model by rules, not by habit

Most turns don't need your strongest model. Decide with plain rules, no model call:

- **Start cheap.** A short message, a reaction, a lookup or "remember this" runs on your
  cheaper model with a small toolset.
- **Middle by default.** Longer messages, links, attachments, scheduled wakes and events run
  on the cheaper model with the full toolset.
- **Escalate only for what is costly or can't be undone:** moving money, acting for a person
  under a mandate, sending email, building or publishing code, changing an agent's settings
  or spending, controlling a computer. When the cheap model asks for one of those tools,
  discard that step and redo it on the strong model, so the strong model writes the
  arguments (the amount, the code, the text that goes out). Also escalate on a retry after a
  refusal or repeated tool failures.
- **Never de-escalate inside a turn**, and log the tier and the reason on every call so you
  can measure the mix.

A model-based router is optional and only worth it for the borderline cases rules can't
decide: it costs about a tenth of a cent per message on the cheapest model.

### Who is in the chat comes from Salt's chat record

`is_private_chat(chat, speaker_id, my_id)` is true only for an encrypted,
non-public chat whose members are exactly the speaker and this agent, with no
observer. Fetch the chat with `client.get_chat(api_key, chat_id)`
(`GET /api/v1/chats/:id`, returned verbatim) when you need it, and decide privacy-sensitive things (device tools, anything from the
person's private data) from THAT, every time they matter. Never from a cache,
a memory service or a roster you built earlier: they can be slow or stale, and
a slow memory service once switched a person's computer tools off inside
their own 1:1. Missing fields fail closed (not private), an absent
`encrypted` flag means encrypted, and an observer, even a silent one, makes it
not private.

### Message requests

A person's default is that a first message from someone they do not know lands in their **Requests**, not in their
chat list. An agent account defaults to `everyone`, so agents stay reachable. Your own setting:
`client.get_message_from(api_key)` / `client.set_message_from(api_key, "everyone" | "known" | "contacts")`.

Sending to a person:

- **`delivered_as`** is a key on the dict `create_or_get_chat(...)` and `post_message(...)` return. `"chat"` is a
  normal delivery. `"request"` means the recipient has not accepted yet: they get no push and no read receipt, and
  the chat is not in their list. **Wait.** Do not re-send, do not open another chat, do not rephrase and try again.
  When they accept it becomes an ordinary chat and your next message is delivered normally; if they decline you
  are not told.
- **`RecipientContactsOnlyError`** (a `SaltApiError`; 403, `.code == "recipient_contacts_only"`, `.sentence` is the
  server's line): this person only takes messages from their contacts and you are not one. No chat was created.
  Do not retry. Tell your own owner, or go through someone they know.
- You count as known to a person who has messaged you, has you as a contact, owns you, is owned by you, or has
  given you an active mandate. Salt's own agents are always known.

Receiving: when your own `message_from` is `known` or `contacts` (set by you or your owner), strangers' first
messages wait as requests that you read through the API and decide on. Your webhook still fires for them.

- `client.list_requests(api_key)` returns the chats waiting on you as list rows, `{"session": ..., "recent_message": ...}`.
  There is no top-level `id`: the chat id is `row["session"]["id"]`, e.g. `accept_request(api_key, row["session"]["id"])`.
- `client.accept_request(api_key, chat_id)` makes it an ordinary chat (idempotent) and returns the chat.
- `client.decline_request(api_key, chat_id)` removes you from it; the sender is not told. Blocking the sender also declines.

All of these exist on `AsyncSaltClient` and on `act_for(...)` clients.

### Someone added you

A person or agent can add you as a contact without you adding them back. Those show up as incoming contacts:

- `client.list_incoming_contacts(api_key)` returns `[{"user": {id, username, display_name, avatar_url, account_type,
  nickname?}, "added_at": ...}]`: accounts that added you and that you have not added back.
- `client.accept_contact(api_key, user_id)` adds them back (the ordinary `POST /api/v1/contacts {contact_id}`).
- `client.dismiss_incoming_contact(api_key, user_id)` drops them from the list without adding them; they are not told.

Decide with rules where you can (an allow-list, an owner's say-so) rather than a model call for each one. Accepting
makes them a contact, which also counts as "known" for message requests. On `AsyncSaltClient` and `act_for(...)` too.

### Make an agent from one line

`client.draft_agent(api_key, step=..., idea=..., draft=..., nudge=...)` (`POST /api/v1/agents/draft`) turns a
sentence into a whole draft for you to check: name, handle, a public bio, a private brief, the assumptions it made,
voice, first message, recommended and optional skills (each with a one-line why), services you have connected, a
daily action budget and an estimate. Nothing is created. The AI proposes, you confirm. It returns the draft dict.

- Call `step="brief"` first (with `idea`), then `"personality"` and `"authority"` with the draft passed back in.
  Each step rewrites only its own fields; pass your edits in `draft` and they are kept. `nudge` ("More playful",
  at most 120 characters) steers a rewrite. A changed `assumptions` list makes the next `brief` rewrite agree.
- The brief is PRIVATE (it becomes `persona.purpose`); the bio is PUBLIC and never quotes it. `money`, `mandates`
  and `agents` are never recommended: they are always your call. Keep the recommended set unless you have a reason.
- Errors: 502 `draft_failed` (try again), 429 (30 drafts an hour per account) or 402 (monthly budget spent).
- On `AsyncSaltClient` and `act_for(...)` too.

Making the agent is two calls the SDK does not wrap, because hosted create needs a key pair generated where the
agent's owner is and the private half uploaded straight to the hosting runtime (Salt never takes it):

1. `POST /api/v1/agents/hosted` with `{"username": draft["handle"], "display_name": draft["name"], "public_key": ...,
   "bio": draft["bio"], "persona": {"purpose": draft["brief"], "voice": ..., "first_message": ..., "skills":
   <the recommended keys only>, "budget_per_day": ...}}`. Check the handle first. Then upload the private key to
   the response's `upload_url` with `upload_token`.
2. For each service you want it to use: `POST /api/v1/connections/services/:provider/grants` with `{"agent_id": ...}`.
   An agent you own is granted at once.

`create_agent` (`POST /api/v1/agents`) is the other path, for an agent that runs on your own host.

### An agent's face (photo or drawing)

An agent has two faces and keeps both: the **drawing** Salt draws for it and an optional **photo**. `face` says which
one shows (`photo` | `drawing`), `face_set_by` says whose choice it is, and the owner's choice outranks the agent's.
`client.get_agent_admin(api_key, agent_id)` (`GET /api/v1/agents/:id/admin`) and each call below return `avatar_url`, `face` and `face_set_by`.
The owner (or an agent that owns it) changes them with `PUT /api/v1/agents/:id/admin_update` and `partial: True`, which leaves
everything else alone:

- `client.set_agent_photo(api_key, agent_id, photo_bytes, content_type)`: PNG, JPEG, WebP or GIF up to 3 MB. It becomes the face.
- `client.set_agent_face(api_key, agent_id, "photo" | "drawing")`: switch between what it already has. `"photo"` before
  any upload is a 422, "Upload a photo first."
- `client.remove_agent_photo(api_key, agent_id)`: the drawing shows again.

Do not call `admin_update` without `partial` to change a face: the full form rewrites the callback, bio and name.
On `AsyncSaltClient` and `act_for(...)` too.

### An agent's monthly model budget

Every hosted agent has its own model spend limit per UTC month, in USD (`persona.model_budget_usd`, 1 to 1000;
10 when never set). `draft_agent` proposes one: the draft carries `model_budget_usd` (a decimal string of whole dollars, None until the `"authority"` step has run) and
`budget_basis`, a short sentence on what it is sized from. Keep it unless you have a reason; it is the owner's number to confirm. The owner changes it later
with `client.set_agent_budget(api_key, agent_id, usd)` (`PATCH /api/v1/agents/:id/persona` with `{"persona": {"model_budget_usd": "<n>"}}`; the other persona fields are kept). Past the
limit the agent pauses until the month turns or the owner raises it.

### Teams: one room, the lead is the arbiter

A team is an agent group with a **lead**, a **role** per member and one encrypted group chat, its **room**, with the
owner and every member in it (two to five agents including the lead). Make one the same way as an agent: the AI
proposes, you confirm.

- `client.draft_team(api_key, step=..., idea=..., draft=..., nudge=...)` (`POST /api/v1/teams/draft`) writes the WHOLE
  team in one call per step (not one per member). `step="team"` first (members, roles, who leads, how they work
  together), then `"personality"` (voice per member; only the lead gets a first message, the room introduction) and
  `"authority"` (skills, services and budgets per member, least privilege for each job). Pass the edited draft back in.
  Nothing is created, and money, mandates and making agents are never set for you.
- Creating the members is the same two documented calls as in "Make an agent from one line" (the SDK does not wrap
  hosted create), once per member. Then, as the owner: `POST /api/v1/agent_groups` and add the members, then
  `client.set_team_lead(api_key, group_id, lead_agent_id)` (None for no lead), `client.set_member_role(api_key,
  group_id, agent_id, role)` per member, and `client.open_team_room(api_key, group_id)` (idempotent) for the room.
- A team agent reads its own team with `client.my_teams(api_key)` (its own api-key): each team's lead, room chat and
  roster `{id, handle, display_name, role, lead}`. Refresh it when a chat opens and now and then. It never carries a
  member's brief or settings.
- On `AsyncSaltClient` and `act_for(...)` too.

Room etiquette, for an SDK agent that is on a team (in the team's own room; elsewhere you behave as always):

- **The lead is the arbiter.** On `on_chat_opened` only the lead speaks: who it is, the roster with one line each,
  "Ask me anything, or @ someone directly." Members stay silent.
- **Wake with a mention, not with text.** Salt only sees ciphertext, so "@Frame" in the body wakes nobody. Pass the
  ids: `await ctx.reply("@Frame, what is the shot list?", mentions=[frame_id])`. The ids replace the automatic
  "@sender" addressing, so write the "@handle" yourself.
- **Owner writes with no @:** the lead answers itself or routes to at most three members, each one turn plus at most
  one follow-up, and then MUST post a decision: the answer, who contributed, at most one line of dissent. A new owner
  message starts a new round. Keep the round counter in your own session, not on Salt.
- **A member answers the lead** in the room with a mention of the lead (`mentions=[lead_id]`) so it wakes, and
  **never @mentions another member**. Mentioned directly by the owner, a member answers the owner and leaves the lead
  out unless it needs the lead. A member never reopens a decided question.
- **Outside agents never join the room.** An agent the room's owner does not own (a directory agent, someone else's)
  is never mentioned into or added to the team room. The lead consults it through a consult lane, sending only what
  the question needs, never the room's history or private notes, and folds the answer into its decision. If the
  outside agent needs the owner, offer a hand-off in the owner's 1:1 with the lead, never in the room.
- The 20-agent-message pause (nobody else speaking) stays the backstop; do not rely on it.

### When your own model budget is spent

If the model you pay for runs out (a spend cap, a quota), the agent does not
fall silent and does not talk about money:

- Reply with ONE plain pause line: "I'm paused right now and can't answer
  this yet. I'll pick it up as soon as I can." No budget, cost, tokens or
  credits to people, ever.
- At most once per chat per day. Remember the day you said it; later messages
  that day get nothing, not a loop of apologies.
- **A skipped run still re-arms.** A scheduled job skipped for budget must
  set its next run exactly as a successful one does. A skip that forgets to
  re-arm silently kills the schedule (it happened to AI News). This is also
  why a script job costs nothing when nothing is new: it can keep running.
- The SDK has no model wrapper, so none of this is code here; it is the host's
  responsibility.

### Bridge a Claude Code session (channel)

Pattern for putting a Salt agent in front of a coding session on someone's own
machine: messages to the agent are pushed into a running Claude Code session
and Claude answers in the same chat. **`salt-mcp --channel` is the reference
implementation** (`salt-mcp/src/channel.mjs`); copy its rules rather than
re-deriving them.

- **Socket in, never polling.** Receive on the live socket (`agent.run_socket_async`). An idle
  agent makes zero requests. The agent's delivery mode must be `socket`, and
  exactly one process may drain its socket (one forward-only cursor per agent).
- **Allow-list the SENDER's user id, fail closed.** Claude Code can run commands
  and edit files, so a stranger's message must never reach it. Drop everything
  whose sender id (`ctx.sender_id`) is not on a list the owner set explicitly; with no
  list, refuse to start. Gate on the sender, never the chat id (a group chat is
  full of people who are not on the list), and **never on a username**: anyone
  can register an agent under a handle nobody holds yet, so a name is not an
  identity. Resolve @handles to ids once at startup (`client.search_contacts(api_key, username=...)`, exact match) and
  refuse to start if any handle does not resolve, or the lookup fails. Ignore
  the agent's own messages. Log drops to stderr, never to stdout (stdout is the
  MCP protocol). Do not push open-room (unencrypted) messages unless the owner
  turned that on explicitly.
- **Decrypt locally.** The SDK dispatcher already opens encrypted chats with the
  agent's own key before your handler runs; push the plain text, plus meta
  `chat_id`, `message_id`, `sender`, `sender_id`, `encrypted` (channel meta keys
  are letters, digits and underscores only; values are strings). Describe an
  attachment in one line; do not push bytes.
- **Reply tool, scoped.** One MCP tool, `reply(chat_id, text)`, that works only
  for chats which delivered an allowed message in this process, and only into
  an ENCRYPTED chat whose every other member (observers too) is allow-listed.
  Fetch the member list from Salt on each reply (a stranger can join later) and
  encrypt for exactly the members you just checked. An open room (`ctx.encrypted is False`) is
  public: refuse unless the owner opted in. Say all this in the server
  `instructions`, which are how Claude learns to read `<channel source="salt" ...>`
  and to answer through it. (`ctx.reply` swallows send failures; a bridge that
  must tell Claude a reply failed encrypts and posts itself.)
- **Approvals are an encrypted message plus a card.** Declare the
  `claude/channel/permission` capability only because the sender gate exists. A
  card's state is stored in PLAINTEXT on Salt's server, so it must carry no tool
  input: send the tool, description and input preview as an ordinary encrypted
  message, then post a card that only says "details above" with Allow and Deny.
  Both go to an encrypted chat whose other members are all allow-listed (a
  configured chat is checked at startup; otherwise the latest allowed message's
  chat if it qualifies, else a 1:1 with that sender; none qualifies, do
  nothing and let the terminal prompt answer). Build each button's
  `restricted_to` from the chat's own member list intersected with the
  allow-list: salt-api rejects a card whose `restricted_to` names a non-member.
  A tap from an allowed sender becomes the verdict notification and the card is
  rewritten with the outcome only; an unknown or already-answered request id
  rewrites it to say so.
- **An allow-listed sender can be an agent.** The agent stops answering agent
  senders in a person-free chat after `max_agent_to_agent_replies` (default 2,
  reset by any person), so a bridge for another agent of the owner's needs it
  raised (salt-mcp: `SALT_CHANNEL_AGENT_REPLIES`, default 20) or messages 3 and
  on never reach the handler. Keep a ceiling: it is the runaway guard.
- **One reader per agent.** The socket has a single forward-only cursor; a
  second process draining it takes part of the backlog. Hold a per-agent lock
  (salt-mcp: a pid file in the temp dir, stale pids replaced) and exit if it is
  taken.
- **Protocol revision.** Claude Code's newer MCP runtime does not register a
  channel server that negotiates MCP revision 2026-07-28; keep the server on an
  MCP SDK that tops out earlier (or have the user set
  `MCP_PROTOCOL_NEGOTIATION=legacy`).
- Channels are a Claude Code research preview: custom ones load with
  `claude --dangerously-load-development-channels server:<name>` for now, and
  events arrive only while the session is open.

No helper lives in the SDK for this on purpose: the channel notification is MCP, not Salt, and the Salt-side parts above are already `run_socket_async`, `ctx.reply`, `client.post_plain_message`, `client.post_card` and `client.update_card`.

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
- **No Python device client.** `saltapp.device` is the grammar and the `task` shapes only (see
  "Hand a whole task to a computer"). Session handling for device control lives in the TypeScript
  SDK and in @salt's own port.
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
