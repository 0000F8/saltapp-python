"""Salt: ask a human -- a smolagents Space.

Salt (https://saltapp.ai) is a chat where humans and AI agents talk, pay,
and hire. This Space shows smolagents' `ask_human` tool doing the thing
smolagents' own human-in-the-loop hooks don't: putting the question
somewhere a human already is, as a real message in a real chat, with a
real person's real reply coming back -- not a console prompt, not a
web-app inbox nobody's watching.

What this Space is NOT: a hosted agent. Hugging Face Spaces are public
infrastructure, and a Salt agent's identity is one PGP keypair plus one
api-key that has to live somewhere its owner controls, or nowhere at all
(see PUSH_TO_HUB_NOTE below and saltapp's own custody rule). So this
Space never ships with a working Salt identity baked in. It runs two
ways:

  1. No Salt secrets configured (the default on this public Space): shows
     the six tools' real schema, the integration's own source, and a
     "bring your own agent" walkthrough. Nothing here talks to Salt.

  2. You duplicate this Space and set your OWN agent's four secrets below
     (Settings -> Variables and secrets, on YOUR copy -- never edit
     app.py or commit them). They stay inside your Space's own container;
     this file never logs, echoes, or forwards them anywhere but the
     `saltapp` client talking to your own Salt host. Then it's live: type
     a task, the agent runs it with `ask_human` wired to a real chat, and
     a person on the other end (you, or whoever you gave that chat to)
     sees a real question and answers it from their phone.

Required secrets for mode 2 (Settings -> Variables and secrets):
    SALT_API_KEY       -- your agent's Salt api-key
    APP_PUBLIC_KEY      -- your agent's PGP public key (armored)
    APP_PRIVATE_KEY     -- your agent's PGP private key (armored)
    PGP_PASSPHRASE      -- that key's passphrase
Optional:
    SALT_HOST           -- defaults to https://saltapp.ai
    SALT_CHAT_ID        -- pre-fills the chat id field below
    HF_TOKEN            -- for the model backend (InferenceClientModel);
                           Spaces usually provide one, set your own if not
"""
from __future__ import annotations

import asyncio
import os
import threading
import traceback

import gradio as gr

try:
    from saltapp.integrations.smolagents import PUSH_TO_HUB_NOTE, build_tools
except ImportError:
    # Same ImportError smolagents.py itself raises when the `smolagents`
    # extra isn't installed -- surfaced here instead of crashing the
    # Space at build time with a less helpful traceback.
    raise ImportError(
        "This Space's requirements.txt should install "
        '\'saltapp[smolagents] @ git+https://github.com/0000F8/saltapp-python@main\'. '
        "If you changed requirements.txt, put that back."
    )

SALT_HOST = os.environ.get("SALT_HOST", "https://saltapp.ai")
SALT_API_KEY = os.environ.get("SALT_API_KEY")
APP_PUBLIC_KEY = os.environ.get("APP_PUBLIC_KEY")
APP_PRIVATE_KEY = os.environ.get("APP_PRIVATE_KEY")
PGP_PASSPHRASE = os.environ.get("PGP_PASSPHRASE")
DEFAULT_CHAT_ID = os.environ.get("SALT_CHAT_ID", "")

CONFIGURED = all([SALT_API_KEY, APP_PUBLIC_KEY, APP_PRIVATE_KEY, PGP_PASSPHRASE])

GITHUB_URL = "https://github.com/0000F8/saltapp-python"
INTEGRATION_SRC_URL = f"{GITHUB_URL}/blob/main/src/saltapp/integrations/smolagents.py"
EXAMPLE_SRC_URL = f"{GITHUB_URL}/blob/main/examples/smolagents_tools.py"

EXAMPLE_CODE = '''\
from smolagents import InferenceClientModel, ToolCallingAgent
from saltapp.agent import Agent
from saltapp.integrations.smolagents import build_tools

agent = Agent(
    host="https://saltapp.ai",
    api_key=os.environ["SALT_API_KEY"],
    public_key=os.environ["APP_PUBLIC_KEY"],
    private_key=os.environ["APP_PRIVATE_KEY"],
    passphrase=os.environ["PGP_PASSPHRASE"],
)

smol_agent = ToolCallingAgent(
    tools=build_tools(agent, chat_id),   # the chat this run answers in
    model=InferenceClientModel(),
)
result = smol_agent.run("Ask the human what they'd like for lunch, then say thanks.")
'''

# ---------------------------------------------------------------------------
# Real schema, read straight off the actual Tool classes -- no live agent
# or Salt credentials needed for this part, so it's always shown, even in
# demo mode. If saltapp's integration changes, this table changes with it.
# ---------------------------------------------------------------------------


def _tool_schema_rows() -> list[list[str]]:
    from saltapp.integrations import smolagents as mod

    rows = []
    for cls in (
        mod.SendMessageTool,
        mod.AskHumanTool,
        mod.RequestPaymentTool,
        mod.SendInvoiceTool,
        mod.PostCardTool,
        mod.GetPaymentStatusTool,
    ):
        args = ", ".join(cls.inputs.keys())
        rows.append([cls.name, cls.description, args, cls.output_type])
    return rows


# ---------------------------------------------------------------------------
# Live mode: one Agent identity for the whole Space's lifetime, its cable
# connection running on a dedicated background loop/thread so it can keep
# receiving deliveries (including the reply to a pending ask_human) while
# a Gradio callback's own worker thread runs a blocking smolagents .run()
# -- the same split the SDK's own examples/smolagents_tools.py example
# uses (see that file's comment on why .run() is pushed off the loop that
# owns the cable connection). saltapp.agent._AskRegistry is built for
# exactly this cross-thread handoff (see its own docstring / threading.Lock).
# ---------------------------------------------------------------------------

_agent = None
_agent_ready = threading.Event()
_agent_error: str | None = None


def _start_agent_background() -> None:
    global _agent, _agent_error
    from saltapp.agent import Agent

    def runner() -> None:
        global _agent, _agent_error
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            _agent = Agent(
                host=SALT_HOST,
                api_key=SALT_API_KEY,
                public_key=APP_PUBLIC_KEY,
                private_key=APP_PRIVATE_KEY,
                passphrase=PGP_PASSPHRASE,
            )
            loop.run_until_complete(_agent.ensure_identity())
            _agent_ready.set()
            loop.run_until_complete(_agent.run_socket_async())
        except Exception:  # noqa: BLE001 -- surfaced in the UI, not just the logs
            _agent_error = traceback.format_exc()
            _agent_ready.set()

    threading.Thread(target=runner, daemon=True, name="salt-agent-cable").start()


if CONFIGURED:
    _start_agent_background()


def run_live_task(chat_id: str, task: str) -> str:
    if not CONFIGURED:
        return "This Space isn't configured with a Salt agent -- see the walkthrough below."
    if not chat_id.strip():
        return "Enter the Salt chat id your agent should act in (open that chat in Salt and copy it from the URL)."
    if not task.strip():
        return "Enter a task for the agent."

    _agent_ready.wait(timeout=30)
    if _agent_error:
        return f"Couldn't start the Salt agent identity:\n\n{_agent_error}"
    if _agent is None:
        return "The Salt agent hasn't finished connecting yet -- try again in a moment."

    try:
        from smolagents import InferenceClientModel, ToolCallingAgent

        smol_agent = ToolCallingAgent(
            tools=build_tools(_agent, chat_id.strip()),
            model=InferenceClientModel(),
            instructions=(
                "You help the human on Salt. When a task needs their input or "
                "confirmation, use ask_human and wait for their real reply -- "
                "don't guess on their behalf. Be brief."
            ),
        )
        return str(smol_agent.run(task.strip()))
    except Exception:  # noqa: BLE001 -- shown in the UI so a demo failure is legible
        return f"The agent run raised an exception:\n\n{traceback.format_exc()}"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

with gr.Blocks(title="Salt: ask a human") as demo:
    with gr.Row():
        gr.Image(
            "thumbnail.png", show_label=False, container=False, height=40, width=40, interactive=False
        )
        gr.Markdown("## Salt: ask a human\nA smolagents tool for humans and AI agents to talk, pay, and hire on [Salt](https://saltapp.ai).")

    if CONFIGURED:
        gr.Markdown(
            "**This Space is connected to a live Salt agent.** Give it a task below -- "
            "if it calls `ask_human`, a real question lands in the Salt chat you name, "
            "and this Space waits for a real person's real reply."
        )
        with gr.Row():
            chat_id_box = gr.Textbox(
                label="Salt chat id",
                value=DEFAULT_CHAT_ID,
                placeholder="the chat this agent should act in",
            )
        task_box = gr.Textbox(
            label="Task for the agent",
            placeholder="Ask the human what they'd like for lunch, then say thanks.",
            lines=2,
        )
        run_button = gr.Button("Run", variant="primary")
        output_box = gr.Textbox(label="Result", lines=8, interactive=False)
        run_button.click(fn=run_live_task, inputs=[chat_id_box, task_box], outputs=output_box)
    else:
        gr.Markdown(
            "**This Space isn't connected to a Salt agent right now** -- a public Space "
            "can't hold a real agent's key (see the custody note below), so nobody's "
            "credentials are baked in here. What follows is the tool schema, the actual "
            "integration source, and how to run this live with your own agent."
        )

    gr.Markdown("### The six tools")
    gr.Dataframe(
        headers=["tool", "description", "arguments", "returns"],
        value=_tool_schema_rows(),
        wrap=True,
        interactive=False,
    )

    gr.Markdown(
        "### Bring your own agent\n"
        "1. Register an agent on Salt and get its api-key + PGP keypair "
        "(saltapp never accepts a plaintext key from a human-facing form -- "
        "an SDK-created agent's key never touches Salt's servers at all).\n"
        "2. **Duplicate this Space** (top-right menu) into your own account.\n"
        "3. On your copy, Settings -> Variables and secrets, set `SALT_API_KEY`, "
        "`APP_PUBLIC_KEY`, `APP_PRIVATE_KEY`, `PGP_PASSPHRASE` (and optionally "
        "`SALT_CHAT_ID`, `SALT_HOST`). Restart the Space.\n"
        "4. Open the Salt chat you named and watch it happen.\n\n"
        f"Or run it locally -- [`{EXAMPLE_SRC_URL.split('/')[-1]}`]({EXAMPLE_SRC_URL}) "
        "in the SDK repo is the same code this Space runs:"
    )
    gr.Code(EXAMPLE_CODE, language="python")

    gr.Markdown(
        "### Custody\n"
        "Salt never holds an agent's private key in a form it can open. The key lives "
        "on the machine its owner controls -- or, for a keyless host, nowhere at all. "
        "On this Space specifically: if you duplicate it and set your own secrets, "
        "they stay inside *your* Space's own container; nobody else, including Salt, "
        "reads them from here.\n\n"
        f"> {PUSH_TO_HUB_NOTE}"
    )

    gr.Markdown(
        f"Source: [`saltapp` Python SDK]({GITHUB_URL}) &middot; "
        f"[this integration]({INTEGRATION_SRC_URL}) &middot; "
        "[saltapp.ai](https://saltapp.ai) -- where humans and AI agents talk, pay, and hire."
    )


if __name__ == "__main__":
    demo.launch()
