# Changelog

All notable changes to `saltapp` are documented here. Dates are UTC.

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
