# A hosted agent identity: the four values salt-agent-sdk's README tells a
# consumer to capture from `createAgent`'s response (SALT_APP_ID,
# SALT_API_KEY, APP_PUBLIC_KEY, APP_PRIVATE_KEY) plus the PGP passphrase
# protecting the private key. Unlike the TS SDK's IdentityStore (a
# multi-identity registry for a process hosting several spawned agents),
# `saltapp.agent.Agent` hosts exactly one identity -- see AGENTS.md for why
# that's the right scope here.
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Identity:
    """One Salt agent's credentials and PGP keypair.

    `agent_id` may be left empty at construction time and filled in later by
    `SaltClient.who_am_i` / `Agent.ensure_identity()` -- salt-api is the only
    authority on which id an api-key actually belongs to (see
    salt-agent-sdk's reconcile.ts for the history of why that matters: ids
    have migrated before).
    """

    api_key: str
    public_key: str
    private_key: str
    passphrase: str = ""
    agent_id: str = ""
    username: str | None = None
    display_name: str | None = None
    extra: dict = field(default_factory=dict)

    def with_agent_id(self, agent_id: str) -> "Identity":
        self.agent_id = agent_id
        return self
