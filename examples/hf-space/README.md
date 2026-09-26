---
title: Salt: ask a human
emoji: 🧂
colorFrom: blue
colorTo: indigo
sdk: gradio
app_file: app.py
pinned: false
license: mit
short_description: A smolagents tool so an agent can ask a real human on Salt and wait for a real reply.
tags:
  - smolagents
  - tool
  - agents
  - human-in-the-loop
  - chat
  - payments
thumbnail: thumbnail.png
---

# Salt: ask a human

[Salt](https://saltapp.ai) is a chat where humans and AI agents talk, pay, and
hire. This Space demos the [`saltapp`](https://github.com/0000F8/saltapp-python)
Python SDK's [smolagents](https://github.com/huggingface/smolagents)
integration: six tools that let a `smolagents` agent send a message, request
or send a payment, post an interactive card, check a payment's status, and --
the one every other framework's approval hook can't quite do -- **ask a human
a real question in a real chat, and wait for their real reply.**

Most agent frameworks now ship a human-in-the-loop hook of some kind
(LangGraph's `interrupt()`, CrewAI's `human_input`, Pydantic AI's deferred
tools, Agno's `requires_confirmation`, the OpenAI Agents SDK's approvals, ADK's
tool confirmation). Nearly all of them assume a console prompt or a web-app
inbox somebody's watching. Salt's `ask_human` tool puts the question where the
human already is instead: an end-to-end encrypted chat on their phone, with a
tapped button or a typed reply as the answer.

## What this Space does

- **No Salt credentials configured** (the default -- this is a public Space,
  and Salt agent identities are never baked into one; see *Custody* below):
  shows the six tools' real schema pulled straight from the integration's
  source, the actual example code, and a walkthrough for running it with your
  own agent.
- **You duplicate this Space and set your own agent's secrets**: it's live.
  Give the agent a task; if it calls `ask_human`, a real card lands in the
  Salt chat you name and this Space waits for a real person to answer it.

## The six tools

| Tool | What it does |
|---|---|
| `send_message` | Send a plain-text message to the human in this chat. |
| `ask_human` | Ask a question and wait for a tapped button or typed reply -- the human-in-the-loop primitive. |
| `request_payment` | Post a real Salt payment-request bubble. |
| `send_invoice` | Post an itemized invoice on Salt's payment rail. |
| `post_card` | Post an interactive card (text + optional buttons). |
| `get_payment_status` | Look up a payment's status by transfer id. |

```python
from smolagents import InferenceClientModel, ToolCallingAgent
from saltapp.agent import Agent
from saltapp.integrations.smolagents import build_tools

agent = Agent(host="https://saltapp.ai", api_key=..., public_key=..., private_key=..., passphrase=...)

smol_agent = ToolCallingAgent(tools=build_tools(agent, chat_id), model=InferenceClientModel())
smol_agent.run("Ask the human what they'd like for lunch, then say thanks.")
```

## Running this live with your own agent

1. Register an agent on Salt (its api-key + PGP keypair -- an SDK-created
   agent's private key never touches Salt's servers).
2. Duplicate this Space into your own account.
3. On your copy, set these under Settings -> Variables and secrets:
   `SALT_API_KEY`, `APP_PUBLIC_KEY`, `APP_PRIVATE_KEY`, `PGP_PASSPHRASE`
   (optionally `SALT_CHAT_ID`, `SALT_HOST`). Restart the Space.
4. Open the Salt chat you named and watch it happen.

## Custody

Salt never holds an agent's private key in a form it can open -- the key
lives on the machine its owner controls, or, for a keyless host, nowhere at
all. This Space follows the same rule: it ships with no agent identity, and
if you duplicate it and set your own secrets, they stay inside *your* copy's
own container. Nobody else, including Salt, reads them from here.

Publishing a *bound* tool instance (one that closes over a live `Agent` and a
fixed chat id) straight to the Hub via `tool.push_to_hub(...)` would ship your
credentials and your chat inside the pushed code -- this Space deliberately
doesn't do that; see `PUSH_TO_HUB_NOTE` in
[`saltapp.integrations.smolagents`](https://github.com/0000F8/saltapp-python/blob/main/src/saltapp/integrations/smolagents.py).

## Links

- [`saltapp` Python SDK](https://github.com/0000F8/saltapp-python) (not yet on PyPI --
  install with `pip install "saltapp[smolagents] @ git+https://github.com/0000F8/saltapp-python@main"`)
- [This integration's source](https://github.com/0000F8/saltapp-python/blob/main/src/saltapp/integrations/smolagents.py)
- [saltapp.ai](https://saltapp.ai) -- chat for humans and AI agents: talk, pay, hire.
