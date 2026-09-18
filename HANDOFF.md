# HANDOFF.md

(The coordinator removes this file before publishing -- it's build-round
notes, not user-facing documentation.)

Lane: `py-integrations` (overnight run, `design-fleet/runs/2026-09-17-distribution/`),
2026-09-18. Repo `/Users/z1ggy/projects/salt/saltapp-python`, branch
`integrations`, on top of the existing `main` (the `python` lane's core
SDK, already committed). **This file replaces the previous HANDOFF.md**
(that lane's own build notes, superseded -- its content is still in git
history on `main` if you need it; the core SDK it describes is done and
untouched by this lane except one small addition to `agent.py`, noted
below). Nothing published, pushed, or PR'd, per the lane rules.

## What changed

Nine framework integrations under `src/saltapp/integrations/`, each
shipping (a) Salt's six tools (`send_message`, `ask_human`,
`request_payment`, `send_invoice`, `post_card`, `get_payment_status`) as
that framework's own tool primitive, and (b) a bridge routing the
framework's own human-in-the-loop mechanism through Salt's `ask()`:

```
src/saltapp/integrations/
  _tools.py          # NEW -- shared SaltTools + pydantic input schemas + build_plain_functions()
  langchain.py        # SaltToolkit + ask_via_interrupt()/SaltInterruptRunner (LangGraph)
  crewai.py            # salt_tools() (BaseTool subclasses) + install_salt_human_input()
  pydantic_ai.py        # build_toolset() (FunctionToolset) + resolve_deferred_approvals()
  agno.py               # SaltToolkit (agno.tools.Toolkit) + resolve_agno_run()
  adk.py                # build_tools() (FunctionTool) + resolve_confirmation_via_salt()
  openai_agents.py      # build_tools() (@tool) + resolve_interruptions()
  smolagents.py         # build_tools() (Tool instances) -- ask_human IS the bridge
  llamaindex.py         # SaltToolSpec (BaseToolSpec) -- ask_human/aask_human IS the bridge
  camel.py              # SaltHumanToolkit -- drop-in for camel.toolkits.HumanToolkit

packages/
  langchain-saltapp/            # thin partner package, LangChain's naming convention
    pyproject.toml, README.md, src/langchain_saltapp/__init__.py
  llama-index-tools-saltapp/    # thin partner package, LlamaHub's naming convention
    pyproject.toml, README.md, llama_index/tools/saltapp/__init__.py

examples/   # one runnable cookbook per framework, ~50-85 lines each, socket mode
  langgraph_interrupt.py        # THE FLAGSHIP: pauses, asks on Salt with buttons, resumes on tap
  crewai_human_input.py
  pydantic_ai_deferred_approval.py
  agno_confirmation.py
  adk_confirmation.py
  openai_agents_approval.py
  smolagents_tools.py
  llamaindex_agent.py
  camel_human_toolkit.py

tests/integrations/   # 36 new tests (117 total with the core 81)
  conftest.py, test_langchain.py, test_crewai.py, test_pydantic_ai.py,
  test_agno.py, test_adk.py, test_openai_agents.py, test_smolagents.py,
  test_llamaindex.py, test_camel.py
```

Plus small edits to existing files:
- **`src/saltapp/agent.py`**: added ONE new public function,
  `tool_context(agent, chat_id) -> _BaseContext`, at the end of the file
  (12 lines). It's the public constructor every integration's `SaltTools`
  uses to get `ask()`/`post_card()`/`request_payment()` scoped to a chat
  with no live `MessageContext` behind it (a framework's tool-calling loop
  doesn't hand us one). Nothing else in `agent.py` changed; all 81
  existing tests still pass unmodified.
- **`pyproject.toml`**: added nine `[project.optional-dependencies]`
  extras (`langchain`, `crewai`, `pydantic_ai`, `agno`, `adk`,
  `openai_agents`, `smolagents`, `llamaindex`, `camel`) and one
  `[dependency-groups]` group (`integrations-dev`, everything at once --
  read its comment before using it, see "Known conflicts" below).
- **`README.md`**: new "## Integrations" section (table of all nine +
  the thesis paragraph), a note in "## Development" about running
  `tests/integrations/`.
- **`AGENTS.md`**: new "## Framework integrations" section (build notes,
  the `LineItemInput` schema gotcha, the CrewAI undocumented-hook caveat,
  the dependency-conflict story, the socket-contract-revision note), a
  paragraph in "## Testing", three new bullets in "## Things a future
  pass should look at".

No migrations (this is a pure-Python SDK, no database). No changes to
`saltapp.client`, `saltapp.crypto`, `saltapp.cards`, `saltapp.webhook`,
`saltapp.socket`, or the FastAPI/Flask adapters.

## How to test

```bash
cd saltapp-python
source .venv/bin/activate     # already created + has every framework installed (see below)
pytest -q                     # 117 passed
python -m build                # sdist + wheel for saltapp itself (unaffected by this lane)
```

The venv at `.venv/` (gitignored) already has all nine frameworks
installed, in this order (matters -- see "Known conflicts" below):
`langchain-core`, `langgraph`, `google-re2==1.1.20240702` (pinned
prebuilt wheel, BEFORE crewai), `crewai`, `pydantic-ai-slim`, `agno`,
`google-adk`, `openai-agents`, `smolagents`, `llama-index-core`,
`camel-ai`. A fresh clone reproduces it with (roughly) that same
sequence; see the exact commands in "Known conflicts."

Every integration module and its tests were run and verified with the
REAL framework installed and imported -- not just read from docs. In
particular, `tests/integrations/test_openai_agents.py` drives a full
`Runner.run()` against `agents.testing.ScriptedModel` (a real scripted
model backend, no OpenAI API key needed) through an actual
`needs_approval=True` interruption and back; `tests/integrations/test_langchain.py`
drives a real compiled LangGraph graph through a real `interrupt()`/
`Command(resume=...)` pause and resume. Every "times out" test asserts
`saltapp.AskTimeout` actually raises (or, for CAMEL's console-compatible
method which has no timeout parameter, exercises the same 120s-default
code path directly through `_salt.ask_human(timeout_seconds=...)` rather
than blocking a real thread for two minutes -- see that test file's
comment).

### Known conflicts (read before reinstalling from scratch)

1. **`google-re2` (a `crewai` -> `cel-python` transitive dependency) has
   no prebuilt wheel for this machine's arm64/cp312 combination**, and
   building it from source fails on this machine's outdated Xcode
   (14.0.1 -- too old for the C++17 the sdist needs, and missing
   `pybind11` headers). Worked around by installing the last version
   with a prebuilt wheel FIRST:
   ```bash
   pip install --only-binary=:all: "google-re2==1.1.20240702"
   pip install crewai
   ```
   If the coordinator's real build machine has a current Xcode/clang,
   this workaround is unnecessary -- `pip install crewai` alone should
   build `google-re2` from source fine there.
2. **`crewai` pins `openai<3`; `openai-agents` pins `openai>=3`.** A
   single combined resolve (`pip install --group integrations-dev`, or
   any tool that does one real backtracking resolve of everything at
   once) FAILS outright with `ResolutionImpossible` -- verified by
   running exactly that command. Installing each extra with its own
   separate `pip install <extra>` call, in sequence, in one venv
   (what this lane actually did) works: pip doesn't re-validate the
   whole transitive graph on each subsequent install, so a later
   package's pin quietly wins. Every integration module still imports
   correctly and every integration's own tests still pass under that
   final combined state -- I re-ran `pytest tests/integrations/ -q`
   after all nine were installed and got 36/36 green. `instructor` (a
   crewai dependency, wants `jiter<0.15`) vs `openai-agents` (wants
   `jiter>=0.17`) is the same shape of conflict, same resolution.
3. **This is a real, load-bearing fact for anyone packaging `saltapp`
   with multiple integration extras at once** (e.g. a Docker image with
   `pip install "saltapp[crewai,openai_agents]"` in one `RUN` line) --
   that single-resolve install would fail the same way `--group
   integrations-dev` did. A real consumer should expect to run each
   framework in its own venv/container, which is normal practice for
   competing agent frameworks anyway, but it's worth stating plainly
   here rather than discovering it during a Docker build.

## The `LineItemInput` schema fix (a real bug this lane caught and fixed)

`_tools.py`'s `build_plain_functions()`'s `send_invoice` tool originally
typed `line_items: list[dict[str, Any]]` (mirroring the core SDK's own
`client.create_invoice` signature). Running `saltapp.integrations.openai_agents.build_tools()`
against a REAL `agents.Agent` failed immediately:

```
agents.exceptions.UserError: additionalProperties should not be set for
object types. This could be because you're using an older version of
Pydantic, or because you configured additional properties to be allowed.
```

The OpenAI Agents SDK's strict-schema mode refuses a bare
`dict[str, Any]` parameter (no fixed properties = `additionalProperties`
in the generated JSON schema, which strict mode disallows). Fixed by
typing it `list[LineItemInput]` (a pydantic model already defined in
`_tools.py` for the class-based frameworks) instead, flattening back to
plain dicts inside the function body before the REST call. This is a
better fix than a special case for one framework -- it also makes the
schema LlamaIndex/ADK/smolagents/CAMEL generate for `send_invoice` more
precise (named fields with descriptions, not an opaque object). Re-ran
every other framework's tests after the change; all still green. If you
add a new tool parameter that's "a list of records," use a pydantic model
the same way or you'll reintroduce this exact failure the moment someone
touches `saltapp.integrations.openai_agents`.

## CLAUDE.md paragraph I'd add (once this is published)

> **`saltapp` integrations** (`saltapp.integrations.<framework>`, nine
> agent frameworks) — each ships Salt's six actions (message, ask a
> human, request payment, invoice, post a card, check payment status) as
> that framework's own tool primitive, plus a bridge routing the
> framework's own human-in-the-loop mechanism (LangGraph `interrupt()`,
> CrewAI's `human_input=True`, Pydantic AI's deferred-tool approval,
> Agno's `requires_confirmation`/`requires_user_input`, ADK's
> `ToolConfirmation`, the OpenAI Agents SDK's `needs_approval`) through
> Salt's own `ask()` — so the human answers where they already are, never
> a console prompt or a second inbox. `langchain-saltapp` and
> `llama-index-tools-saltapp` also exist as standalone partner packages
> (`packages/`) matching those two ecosystems' own naming conventions;
> both are thin re-exports, saltapp itself stays the one source of truth.

## One-line "what's new" candidate

Internal only (a developer-facing SDK release, not a user-facing product
change) — **do not add a line to `salt-fe/src/whatsNew.js`** for this,
same as the core SDK's own HANDOFF said.

## UAT steps

Every integration was verified with the REAL framework's classes against
a MOCKED Salt transport (`httpx.MockTransport`) and, where the framework
supports it, a REAL scripted/fake model backend (OpenAI Agents SDK's
`ScriptedModel`, pydantic-ai's `FunctionModel`) -- see "How to test"
above. **Nobody has run any of these against a live `salt-api` + a real
LLM provider together.** Before calling any ONE integration
production-ready:

1. Register a real `SALT-...` test agent (per the workspace's naming
   convention) against a local or staging `salt-api`.
2. `pip install -e ".[<framework>]"` for the one you're checking.
3. Export `SALT_API_KEY`/`APP_PUBLIC_KEY`/`APP_PRIVATE_KEY`/`PGP_PASSPHRASE`
   and whatever model-provider key that example needs (each
   `examples/*.py` file's docstring says which).
4. `python examples/<name>.py`, then message the agent from the Salt app
   per that file's docstring and confirm the HITL round trip actually
   works end to end (the question/card really appears in the chat, a tap
   or reply really resumes it).
5. **The LangGraph flagship is the one to check first** (`langgraph_interrupt.py`)
   -- it's the example the "Integrations" README section and the task
   brief both call out by name.

## What's left / not done

- **No live salt-api + live LLM smoke test** for any of the nine (see
  UAT above) — this is the biggest open item, same shape as the core
  SDK's own outstanding item.
- **Combined-extras installs are genuinely broken** (crewai vs
  openai-agents' `openai` pin) — not a bug to fix in saltapp; documented
  in `pyproject.toml`, `AGENTS.md`, and `README.md` so nobody rediscovers
  it the hard way.
- **The socket-mode contract these examples use is one revision behind.**
  `LANES.md` was revised 2026-09-18 (after this lane started) to a
  short-poll contract (`timeout` clamped 0..2s server-side, Action Cable
  as the primary push path). `saltapp.socket`/`Agent.run_socket_async()`
  themselves are untouched here (that's the `socket` lane's own SDK-side
  code) and still default to `poll_timeout=25`, which still works (the
  server just clamps it down) but isn't the current recommended shape.
  Every `examples/*.py` file inherits that same "works but dated" default
  through `agent.run_socket_async()` — revisit once the `socket` lane's
  SDK-side change lands.
- **`README.md`'s pre-existing "Custody" quickstart still sends
  `private_key` to `create_agent(...)`** — flagged in
  `design-fleet/runs/2026-09-17-distribution/FOLLOWUPS.md` ("READMEs that
  still describe server-held keys") by the separate `custody` lane, which
  is changing salt-api to refuse a private key over the wire at all. I
  deliberately did NOT touch that section: the correct new registration
  contract depends on that lane's still-in-progress server change, which
  I have no visibility into from here, and guessing at it risks shipping
  a WRONG example rather than an outdated one. None of `saltapp.integrations.*`
  or any new example is affected — every integration builds an `Agent`
  from already-issued credentials and never calls `create_agent`. Whoever
  lands the custody change should fix that quickstart in the same pass.
- **CrewAI's `human_input=True` bridge rides an undocumented extension
  point** (`crewai.core.providers.human_input.set_provider`, real in
  1.15.22's source, absent from public docs) — `install_salt_human_input()`
  raises a clear `ImportError` if a future CrewAI version removes it, and
  `SaltAskHumanTool` is the documented fallback, but re-verify this
  specific hook before bumping the `crewai` extra's floor.
- **`llama-index-tools-saltapp`'s actual LlamaHub listing process is
  unresolved** (see "Listing steps" below) — LlamaHub itself now redirects
  to `developers.llamaindex.ai` and I could not find a documented
  replacement submission process (repeated 404s on plausible paths, per
  the research pass); the package exists and imports correctly, but "how
  it gets discovered" beyond a plain PyPI listing is an open question.

## Listing / PR steps for each framework (as requested)

Verified against each project's current (2026-09) docs/source directly —
not memory. Where a process is genuinely undocumented or unclear, that's
stated plainly rather than guessed.

### LangChain integrations docs
LangChain's current contributing docs state plainly: **"New integrations
are not accepted as pull requests to langchain-ai repositories."**
`langchain-saltapp` is published independently (PyPI + its own repo/dir,
`packages/langchain-saltapp/` here) and then LISTED:
- **Under ~50k monthly downloads**: open an "Integration listing" issue
  on `langchain-ai/docs` (GitHub). Automation adds a row to
  `integration_external_docs.yaml` (`name`, `pypi`, `docs_url`, capability
  flags like `stream`/`tool_calling`) that links out to this package's own
  README/docs — no hosted page.
- **Over ~50k downloads, or maintainer-featured**: eligible for a hosted
  MDX page built from `src/oss/python/integrations/tools/TEMPLATE.mdx` in
  that same docs repo.
- The only PR that should ever go to a `langchain-ai` repo for this is the
  docs-listing one (or, later, the hosted-page one) — never a PR adding
  `langchain-saltapp`'s actual code to their monorepo.

### LlamaHub
`run-llama/llama_index`'s current `CONTRIBUTING.md`: **"we are no longer
accepting new integration packages in this repository... PRs that add a
new `pyproject.toml` will be automatically closed."** `llamahub.ai` itself
now just redirects with "LlamaHub has moved. Browse LlamaIndex
integrations at developers.llamaindex.ai." I could not find a documented
replacement submission process on that site (repeated 404s on plausible
paths during research) — **this is a genuinely open question**, not
something I'm confident enough to prescribe steps for. `llama-index-tools-saltapp`
(`packages/llama-index-tools-saltapp/`) follows the EXISTING naming/metadata
convention (`[tool.llamahub]` in `pyproject.toml`, `import_path`,
`class_authors`, matching e.g. `llama-index-tools-arxiv`'s shape) so it's
ready the moment a real submission path becomes clear; for now, publishing
it to PyPI under its own name is the actionable step, and "getting listed
somewhere LlamaIndex users browse" needs a human to investigate
`developers.llamaindex.ai` directly (or ask in LlamaIndex's Discord/GitHub
discussions) rather than following stale docs.

### CrewAI tools
`crewAIInc/crewAI-tools` (the formerly-separate tools repo) was **archived
2025-11-10**; its README now points at the actively-maintained tools
living under `lib/crewai-tools/` in the main `crewAIInc/crewAI` repo. That
repo's contribution guidance for tools specifically is generic (fork,
branch, PR) with no documented criteria for "official" tool status, no
registration process, and no separate listing/directory found beyond the
docs site's own Tools pages. **Actionable step**: `salt_tools()` (this
lane's CrewAI tools) can be documented and published as an independent
package the same shape as the two partner packages above (not built in
this pass — the task named LangChain and LlamaIndex specifically for
partner packages, not CrewAI), or offered via a PR to `crewAIInc/crewAI`'s
`lib/crewai-tools/` docs describing it as a community tool — there's no
stronger, more official path documented right now.

### ADK integrations page
This one IS well-documented and straightforward: `google/adk-docs`
has a real `docs/integrations/` directory (Markdown files, one per
integration, e.g. GitHub, Daytona, AgentOps). To list Salt:
1. Add `docs/integrations/saltapp.md` following the existing template
   (frontmatter: catalog title, description, icon; body sections: use
   cases, prerequisites, installation, agent usage examples, available
   tools, resources).
2. Add a square PNG logo under `docs/integrations/assets/`.
3. Include a screenshot demonstrating the integration (e.g. the
   confirmation flow landing in a Salt chat).
4. Open a PR; that repo even ships `integration-create`/`integration-review`
   agent skills to help draft and validate the submission before you open
   it. Maintainers review for template/formatting compliance before
   merging — no other approval gate documented.

### smolagents Hub
No listing/review process at all — it's genuinely self-serve:
`tool_instance.push_to_hub(repo_id, ...)` publishes as a Space repo under
your own Hugging Face account/org; `Tool.from_hub(repo_id, trust_remote_code=True)`
loads it back for anyone. **Do not push a tool built by
`saltapp.integrations.smolagents.build_tools()` as-is** — see that
module's `PUSH_TO_HUB_NOTE` (already in the code): it closes over a live
`Agent` holding real credentials and a fixed `chat_id`. A shareable
version would need its `forward()` methods to read `SALT_API_KEY`/etc.
from the environment at call time instead, which is a separate,
not-yet-built variant.

### OpenAI Agents SDK examples
`openai/openai-agents-python`'s top-level `CONTRIBUTING.md` and
`examples/README.md` are both generic (standard fork/branch/PR flow, a
note to avoid committing real credentials in examples, a pointer at
`AGENTS.md`/`tests/README.md`) — I found **no example-specific
directory convention or acceptance criteria** beyond that. Actionable
step: open a PR adding `examples/salt/` (mirroring this repo's own
`examples/openai_agents_approval.py`) with a short README, following the
same generic contribution flow as any other PR to that repo — there's
nothing more specific documented to follow.
