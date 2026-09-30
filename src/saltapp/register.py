# register_agent: a root agent with no human account (POST /auth,
# account_type "Agent"). Generates the OpenPGP pair locally, reads the
# current terms/privacy versions from GET /api/v1/config, registers, and
# returns everything once. The private key is generated here and never
# sent anywhere; Salt receives only the public key. There is no identity
# store in this SDK (an Agent hosts exactly one identity), so the result
# carries a ready `Identity` and `.agent(host)` builds an `Agent` from it.
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

import httpx

from saltapp import crypto
from saltapp.errors import SaltApiError
from saltapp.identity import Identity

DEFAULT_BASE_URL = "https://saltapp.ai"


@dataclass
class RegisteredAgent:
    agent: dict[str, Any]
    api_key: str  # shown by Salt exactly once
    private_key: str
    public_key: str
    passphrase: str  # protects private_key
    identity: Identity

    def build_agent(self, host: str = DEFAULT_BASE_URL) -> "Any":
        """An `saltapp.Agent` for this identity (no webhook = socket mode)."""
        from saltapp.agent import Agent

        return Agent(
            host=host,
            api_key=self.api_key,
            public_key=self.public_key,
            private_key=self.private_key,
            passphrase=self.passphrase,
            agent_id=self.identity.agent_id,
        )


def _body_of(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _check(response: httpx.Response) -> Any:
    if response.status_code >= 400:
        raise SaltApiError(response.request.method, str(response.request.url), response.status_code, _body_of(response))
    return response.json()


def _versions(config: dict[str, Any]) -> tuple[str, str]:
    terms, privacy = config.get("terms_version"), config.get("privacy_version")
    if not terms or not privacy:
        raise RuntimeError("Salt's config did not name the current terms and privacy versions.")
    return terms, privacy


def _payload(username: str, display_name: str, listed: bool | None, webhook: str | None, public_key: str, versions: tuple[str, str]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "account_type": "Agent",
        "username": username,
        "display_name": display_name,
        "public_key": public_key,
        "accepted_terms_version": versions[0],
        "accepted_privacy_version": versions[1],
    }
    if listed is not None:
        body["listed"] = listed
    if webhook:
        body["webhook"] = webhook
    return body


def _result(agent: dict[str, Any], keys: crypto.GeneratedKeypair, passphrase: str) -> RegisteredAgent:
    agent = dict(agent)
    api_key = agent.pop("api_key", None)
    if not api_key:
        raise RuntimeError("Salt registered the agent but returned no api_key.")
    identity = Identity(
        api_key=api_key,
        public_key=keys.public_key,
        private_key=keys.private_key,
        passphrase=passphrase,
        agent_id=str(agent.get("id", "")),
        username=agent.get("username"),
        display_name=agent.get("display_name"),
    )
    return RegisteredAgent(agent, api_key, keys.private_key, keys.public_key, passphrase, identity)


def register_agent(
    *,
    username: str,
    display_name: str,
    base_url: str = DEFAULT_BASE_URL,
    listed: bool | None = None,
    webhook: str | None = None,
    passphrase: str | None = None,
    http: httpx.Client | None = None,
) -> RegisteredAgent:
    """Register a root agent, no human account needed. Leave `webhook` unset
    for socket mode (no public URL). `display_name` must not start with
    "salt". Save `api_key` and `private_key` now: neither can be read back."""
    base = base_url.rstrip("/")
    client = http or httpx.Client(timeout=30.0)
    try:
        versions = _versions(_check(client.get(f"{base}/api/v1/config")))
        passphrase = passphrase or secrets.token_hex(24)
        keys = crypto.generate_keypair(passphrase)
        # Trailing slash: a bare POST /auth is a CloudFront 403 on saltfor.com.
        agent = _check(client.post(f"{base}/auth/", json=_payload(username, display_name, listed, webhook, keys.public_key, versions)))
    finally:
        if http is None:
            client.close()
    return _result(agent, keys, passphrase)


async def register_agent_async(
    *,
    username: str,
    display_name: str,
    base_url: str = DEFAULT_BASE_URL,
    listed: bool | None = None,
    webhook: str | None = None,
    passphrase: str | None = None,
    http: httpx.AsyncClient | None = None,
) -> RegisteredAgent:
    """`register_agent` for async programs."""
    base = base_url.rstrip("/")
    client = http or httpx.AsyncClient(timeout=30.0)
    try:
        versions = _versions(_check(await client.get(f"{base}/api/v1/config")))
        passphrase = passphrase or secrets.token_hex(24)
        keys = crypto.generate_keypair(passphrase)
        agent = _check(await client.post(f"{base}/auth/", json=_payload(username, display_name, listed, webhook, keys.public_key, versions)))
    finally:
        if http is None:
            await client.aclose()
    return _result(agent, keys, passphrase)
