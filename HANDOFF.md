# HANDOFF.md

(The coordinator removes this file before publishing -- it's build-round
notes, not user-facing documentation.)

Lane: `python` (kernel piece K4), 2026-09-18. New repo at
`/Users/z1ggy/projects/salt/saltapp-python`, git-initialized and committed
locally. **Not published anywhere** -- no GitHub repo created, nothing
pushed, nothing uploaded to PyPI, per the lane rules in
`design-fleet/runs/2026-09-17-distribution/LANES.md`.

## What changed / what's here

A brand-new package, `saltapp` (PyPI name; import name `saltapp`), Python
SDK for Salt agents, MIT, Python >= 3.10, `pyproject.toml` + hatchling +
`src/` layout, typed (`py.typed`).

```
saltapp-python/
  pyproject.toml
  README.md          -- install, quickstart, socket-mode + webhook examples
  AGENTS.md           -- module map, scope decisions, what a future pass should check
  LICENSE             -- MIT, copyright 0x0000F8
  HANDOFF.md          -- this file
  .github/workflows/publish.yml  -- PyPI Trusted Publishing, see below
  scripts/generate_ts_vector.cjs -- regenerates the signature test vector
  src/saltapp/
    __init__.py        client.py       crypto.py    cards.py
    webhook.py          socket.py       agent.py     identity.py   errors.py
    integrations/{fastapi,flask}.py
  tests/  -- 81 tests, all real assertions (see below)
```

Modules, briefly (full detail in AGENTS.md):

- **`saltapp.client`**: `SaltClient` (sync, httpx) / `AsyncSaltClient`
  (async) -- messages (list/send with encryption), chats, cards
  (post/update), payment requests, invoices, products, usage, hand-offs.
  `SaltApiError` carries the server's own `{"error": "..."}` sentence.
- **`saltapp.crypto`**: keygen (EdDSA/Ed25519 + ECDH/Curve25519, matching
  openpgp.js's `curve25519` keys), multi-recipient encrypt, decrypt,
  fingerprint, attachment AES-GCM decrypt. Uses `pgpy` + `cryptography`.
- **`saltapp.cards`**: block builders matching salt-api's `card.rb`
  validator exactly (same limits, same `pay`/`handoff` action_type shapes).
- **`saltapp.webhook`**: HMAC verification (ported from
  `salt-call-agent-example/webhook_auth.py`), framework-neutral
  `handle(headers, body) -> Event`, a dependency-free ASGI3 app, plus thin
  FastAPI/Flask adapters in `saltapp.integrations` (behind `[fastapi]`/`[flask]`
  extras).
- **`saltapp.socket`**: `SocketClient`, a long-poll client implementing the
  K2 socket-mode contract from `LANES.md` (`GET /api/v1/agent/updates`),
  verifying every envelope's signature, with backoff on transport errors.
  `MemoryCursorStore` / `FileCursorStore`.
- **`saltapp.agent`**: `Agent` -- hosts one identity, decorators
  (`on_message`, `on_card_interaction`, `on_chat_opened`, `on_invoice_paid`,
  `on_handoff_confirmed`, `on_handoff_received`), the mention rule + loop
  guard + GACM + delivery-id dedupe (ported from `salt-agent-sdk/src/webhook.ts`'s
  dispatch logic), `ctx.reply/post_card/update_card/request_payment/ask/approve`,
  `run_socket()`, `asgi_app()`.

## Deliberate scope narrowing (read AGENTS.md's "Deliberate scope
decisions" section for the full reasoning)

This is parity with **the essentials** the task named, not a line-for-line
port of `salt-agent-sdk`. Specifically not built: multi-identity hosting
(`identities.ts`/`reconcile.ts`), delegation provenance trails
(`delegations.ts`), session notes (`sessions.ts`), the work-report wire
format (`work.ts`), and the 17-tool `actions.ts` LLM tool-calling wrapper.
`saltapp.client` gives you the underlying REST calls; wiring them into a
specific model's tool-calling shape is left to the caller, same as this
SDK has zero opinion about which model you use at all.

**`ctx.ask()`/`ctx.approve()` are this SDK's own addition** -- there is no
`ask()` in the TS SDK as of this writing (`salt-agent-sdk`'s dist/src has
no such export; verified by grep before building). The task prompt's phrase
"exactly like the TS lane's ask" refers to a *planned* primitive described
in `design-fleet/runs/2026-09-17-distribution/page/narrative.html`'s "K3 ·
ASK" kernel piece, which names a not-yet-built server-side `asks` resource.
Since `LANES.md` (my actual contract) only specifies the K2 socket-mode
contract and says nothing about a K3 `asks` endpoint, I built `ask()`
entirely client-side on primitives that already exist and are documented
in the workspace `CLAUDE.md`: post a card, correlate whichever answers it
first (a card tap or, if `free_text=True`, a plain message) via an
in-process registry, with a timeout. No new server endpoint assumed. If a
real `asks` resource ships later, `ask()`'s public signature is designed to
survive the implementation moving onto it -- see AGENTS.md's note.

## How to test

```bash
cd saltapp-python
python3 -m venv .venv && source .venv/bin/activate   # .venv is gitignored, already created+installed
pip install -e ".[fastapi,flask]"
pip install pytest pytest-asyncio build twine
pytest                       # 81 passed
python -m build              # sdist + wheel
python -m twine check dist/* # both PASSED
```

Already run in this session: all of the above, green. `.venv/` exists in
the repo directory but is gitignored, so a fresh clone needs the same
three `pip install` lines.

### The TS-compatibility vector

`tests/fixtures/webhook_signature_vector.json` is not hand-computed --
`scripts/generate_ts_vector.cjs` spins up the REAL compiled
`salt-agent-sdk/dist/webhook.js`'s `createWebhookServer` (that sibling repo
already had `dist/` built and `node_modules/` installed in this
workspace), POSTs real signed HTTP requests at it, and records that
running TypeScript server's own accept/reject verdict for five cases
(valid, tampered digest, stale timestamp, wrong secret, missing header).
`tests/test_webhook_vector.py` asserts `saltapp.webhook.verify_signature`
agrees with all five, byte-for-byte. Regenerate with
`node scripts/generate_ts_vector.cjs` if either SDK's signature scheme
ever changes -- rebuild `salt-agent-sdk` first (`npm run build` there) if
its `dist/` is stale.

I hit one subtlety worth flagging: the "valid, fresh timestamp" vector is
only valid for 300s from the moment it's generated (that's the whole point
of the replay window). A naive fixture would start failing on its own five
minutes after being committed. Fixed by recording `now_unix` (the instant
the generator built each vector) and having the Python test pass
`now=vector["now_unix"]` to `verify_signature`, replaying at the exact
historical instant the TS SDK evaluated it rather than at real wall-clock
time. This makes the fixture permanently reproducible.

## CLAUDE.md paragraph I'd add (once this is published)

> **`saltapp` (Python SDK)** — Python counterpart to `salt-agent-sdk`
> (`~/projects/salt/saltapp-python`, PyPI `saltapp`, MIT): sync/async REST
> client, PGP crypto (pgpy), webhook verification with FastAPI/Flask
> adapters, a long-poll socket-mode client for agents with no public URL,
> and an `Agent` helper (`on_message`/`on_card_interaction`/...,
> `ctx.ask()`/`ctx.approve()` for human-in-the-loop questions built on
> cards). `ctx.ask()` has no TS SDK equivalent yet and assumes no unbuilt
> server endpoint — see its own `AGENTS.md` if that changes.

## One-line "what's new" candidate

Internal only (a developer-facing SDK release, not a user-facing product
change) -- **do not add a line to `salt-fe/src/whatsNew.js`** for this.

## UAT steps (once someone can run this against a real salt-api)

This was built and tested entirely against `httpx.MockTransport` and the
compiled TS SDK's signature logic -- **nobody has run it against a live
Rails server yet.** Before calling it done-done:

1. `pip install -e .` this package into a scratch venv.
2. Register a real `SALT-...` test agent (per the workspace's test-account
   convention) against a local `salt-api` using the README's "Quickstart:
   register an agent" snippet.
3. Run the README's socket-mode example against that agent; send it a
   message from the Salt web app; confirm it replies.
4. Try `ctx.ask()` with `options=[...]` from a real chat: post a message
   that triggers it, confirm the card appears, tap a button, confirm the
   handler resumes with the right answer.
5. Try the webhook path (`agent.asgi_app()` under uvicorn, behind ngrok or
   similar) the same way.

## What's left / not done

- **No live salt-api smoke test** (see UAT above) -- this is the biggest
  open item. Everything here is unit/integration-tested against mocks and
  the compiled TS SDK, never against a running Rails server.
- **No multi-identity hosting**, **no `actions.py` tool-calling wrapper**,
  **no sessions/delegation-trail/work-report parity** -- all deliberate,
  all explained in AGENTS.md's scope section. Not blocked, just out of
  scope for this pass.
- **PyPI publishing is not set up yet** -- see the Trusted Publishing setup
  below; this needs the PyPI project owner (the human) to do a one-time
  step before `.github/workflows/publish.yml` can succeed.
- Flask adapter runs each webhook's async handler via a fresh
  `asyncio.run()` per request (documented in AGENTS.md) -- correct but not
  fast; fine for reference/hobby scale.

## Publishing (for the coordinator, when ready)

Two options; Trusted Publishing is what `.github/workflows/publish.yml`
is wired for and is what PyPI recommends (no long-lived token to leak).

### Option A: PyPI Trusted Publishing via GitHub Actions (what's wired up)

One-time setup on PyPI, BEFORE the first release (needs a human with PyPI
account access -- this cannot be done from here):

1. If the `saltapp` project doesn't exist on PyPI yet: go to
   https://pypi.org/manage/account/publishing/ (or, on an existing project,
   its "Publishing" settings page) and add a new **pending publisher**:
   - PyPI project name: `saltapp`
   - Owner: `0000F8` (the GitHub org)
   - Repository name: `saltapp-python`
   - Workflow filename: `publish.yml`
   - Environment name: `pypi` (matches the `environment:` block in the
     workflow -- this is optional but recommended; if you skip setting an
     environment on PyPI's side, remove the `environment:` key from
     `publish.yml` or the OIDC claim won't match).
2. Push this repo to `github.com/0000F8/saltapp-python` (the coordinator's
   job, not this lane's -- lane rules forbid creating the GitHub repo or
   pushing).
3. Cut a release: `git tag v0.1.0 && git push origin v0.1.0`, then
   `gh release create v0.1.0 --title v0.1.0 --generate-notes` (or via the
   GitHub UI). The `publish` workflow job runs only on `release: published`,
   builds, checks with twine, and uploads via
   `pypa/gh-action-pypi-publish` -- no token, no secret, just the
   repo/workflow identity PyPI now trusts.

### Option B: a plain API token (fallback, if Trusted Publishing setup is
skipped or blocked)

1. Create a PyPI API token scoped to the `saltapp` project (or an
   account-wide one for the very first upload, before the project exists,
   then narrow it).
2. `export TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-...`
3. From a clean `dist/` (`python -m build`): `twine upload dist/*`.
4. If you want CI to do this instead of Option A, add the token as a
   repository secret (e.g. `PYPI_API_TOKEN`) and replace the `publish` job's
   steps with `twine upload` using
   `env: TWINE_USERNAME: __token__ / TWINE_PASSWORD: ${{ secrets.PYPI_API_TOKEN }}`
   -- Option A avoids storing this secret at all, so prefer it unless
   there's a specific reason not to.

Either way: this lane does not publish anything, per the lane rules. The
above is written so the coordinator (or the human) can execute it directly.

## Security notes

- The webhook/socket signature scheme (HMAC-SHA256, `t=<ts>,v1=<hex>`, 300s
  default tolerance, constant-time compare via `hmac.compare_digest`) is a
  direct port of `webhook_auth.py`'s already-reviewed implementation, not
  new cryptographic design. `hmac.compare_digest` is Python's own
  constant-time comparator (equivalent to Node's `timingSafeEqual`, used by
  both `webhook.ts` and `webhook_auth.py`).
- PGP keys generated by `saltapp.crypto.generate_keypair` never leave the
  process except as whatever the caller does with the returned strings; no
  network call is made during key generation.
- `saltapp.agent.Agent` holds the agent's private key and passphrase in
  memory for the process's lifetime (same trust model `salt-call-agent-example`
  and `salt-agent-sdk` already document) -- the README's "Custody" section
  states this plainly for anyone deploying it.
- No third-party telemetry, no analytics SDK, nothing phones home except
  the Salt API the caller explicitly configured (`host=`).
