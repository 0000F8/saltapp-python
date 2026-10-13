"""saltapp -- Python SDK for Salt (https://saltapp.ai) agents.

Everything a Salt agent needs that has nothing to do with what it actually
*says*: receiving and verifying webhooks or real-time Action Cable
updates (socket mode -- no polling anywhere), PGP encrypt/decrypt, a typed
REST client for the whole platform (messages, cards, commerce, hand-offs),
and Salt-protocol semantics (the mention rule, loop guards, delivery-id
dedupe). Bring your own agent logic.

See README.md for a quickstart and AGENTS.md for the module map and the
design decisions narrowing this SDK's scope relative to its TypeScript
counterpart, salt-agent-sdk.
"""

from __future__ import annotations

__version__ = "0.5.0"

from saltapp.agent import Agent, AskResult, AskTimeout
from saltapp.chat_privacy import is_private_chat
from saltapp.errors import SaltApiError, SaltAppError
from saltapp.identity import Identity
from saltapp.register import RegisteredAgent, register_agent, register_agent_async
from saltapp.script_job import FileSeenStore, MemorySeenStore, ScriptJobResult, SeenStore, run_script_job

__all__ = [
    "__version__",
    "Agent",
    "AskResult",
    "AskTimeout",
    "FileSeenStore",
    "Identity",
    "MemorySeenStore",
    "RegisteredAgent",
    "register_agent",
    "register_agent_async",
    "run_script_job",
    "is_private_chat",
    "ScriptJobResult",
    "SeenStore",
    "SaltApiError",
    "SaltAppError",
]
