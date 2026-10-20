# Typed REST client for the Salt platform API (salt-api). Ported from
# salt-agent-sdk/src/client.ts, generalized the same way that module is:
# every method takes the acting identity's own api-key explicitly rather
# than a client-wide header, since one process can legitimately act as more
# than one Salt agent (see saltapp.identity.Identity).
#
# Two classes, `SaltClient` (sync, httpx.Client) and `AsyncSaltClient`
# (async, httpx.AsyncClient), with the same method set and the same request
# shapes -- pick whichever fits your program. `saltapp.agent.Agent` uses
# the async one internally so `ctx.reply()` etc. can be awaited from either
# webhook (ASGI) or socket mode.
from __future__ import annotations

import base64
import hashlib
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlencode

import httpx

from saltapp import crypto
from saltapp.errors import SaltApiError, api_error
from saltapp._version import __version__
from saltapp.identity import Identity

_JSON = dict[str, Any]

# Headers salt-api reads for a mandate-governed call (Mandates R2). See
# SaltClient.act_for / AsyncSaltClient.act_for below.
def _check_rules(rules: list[dict[str, Any]]) -> None:
    """Refuse client-side what the server would answer 422: `approve_paid: False` without a `service`, and a past `expires_at`."""
    for rule in rules:
        if rule.get("approve_paid") is False and not rule.get("service"):
            raise ValueError(f"Rule {rule.get('text')!r}: approve_paid False needs a service to apply to.")
        exp = rule.get("expires_at")
        if exp and datetime.fromisoformat(str(exp).replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            raise ValueError(f"Rule {rule.get('text')!r}: expires_at must be in the future.")


def _rules_of(listing: dict[str, Any], group_id: str) -> list[dict[str, Any]]:
    for group in listing.get("groups") or []:
        if str(group.get("id")) == str(group_id):
            return group.get("rules") or []
    return []


CLIENT_HEADER = "X-Salt-Client"
CLIENT_HEADER_VALUE = f"saltapp-python/{__version__}"
ACT_FOR_HEADER = "X-Salt-Act-For"
MANDATE_HEADER = "X-Salt-Mandate"
# Approve-first (Salt 0.240.0): the plan a post, room or first message belongs to.
PLAN_HEADER = "X-Salt-Plan"
APPROVE_FIRST_CAPABILITIES = ("profile.post", "room.create", "outreach.request")


def media_digest(data: bytes) -> str:
    """The sha256 (hex) of a post's media bytes: pass it as `media_digest` on a `profile.post` ask."""
    return hashlib.sha256(data).hexdigest()


def _plan_headers(plan: str | None) -> dict[str, str] | None:
    return {PLAN_HEADER: str(plan)} if plan else None


def _ask_body(
    capability: str,
    words: str | None,
    words_armored: str | None,
    encrypt_to: str | Sequence[str] | None,
    where: str,
    recipient_id: str | None = None,
    chat_id: str | None = None,
    own_public_key: str | None = None,
    about: str | None = None,
    media_digest: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """The `POST /mandates/:id/asks` body. Refuses client-side what the server would 422: armor on anything but outreach,
    both words and armor, or neither. `encrypt_to` (the person's armored public key) armors `words` with the SDK's own PGP."""
    if (words_armored is not None or encrypt_to is not None) and capability != "outreach.request":
        raise ValueError("request_ask: words_armored / encrypt_to are for outreach.request only.")
    if words_armored is not None and (words is not None or encrypt_to is not None):
        raise ValueError("request_ask: give words_armored, or words (with encrypt_to), not both.")
    if words_armored is None and not words:
        raise ValueError("request_ask: give the exact words.")
    has_target = recipient_id is not None or chat_id is not None
    if capability == "outreach.request":
        if recipient_id is not None and chat_id is not None:
            raise ValueError("request_ask: give recipient_id or chat_id, not both.")
        if not has_target:
            raise ValueError("request_ask: outreach.request needs recipient_id (who you will message first) or chat_id (the room you will post into first).")
    elif has_target:
        raise ValueError("request_ask: recipient_id / chat_id are for outreach.request only.")
    if about is not None and capability != "room.create":
        raise ValueError("request_ask: about is for room.create only.")
    if (media_digest is not None or kind is not None) and capability != "profile.post":
        raise ValueError("request_ask: media_digest / kind are for profile.post only.")
    body: dict[str, Any] = {"capability": capability, "where": where}
    if about is not None:
        body["about"] = about
    if media_digest is not None:
        body["media_digest"] = media_digest
    if kind is not None:
        body["kind"] = kind
    if recipient_id is not None:
        body["recipient_id"] = recipient_id
    if chat_id is not None:
        body["chat_id"] = chat_id
    if words_armored is not None:
        body["words_armored"] = words_armored
    elif encrypt_to is not None:
        if not own_public_key:
            raise ValueError("request_ask: encrypt_to needs own_public_key: an outreach ask is sealed to the person and to you.")
        keys = [encrypt_to] if isinstance(encrypt_to, str) else list(encrypt_to)
        body["words_armored"] = crypto.encrypt_for(words or "", list(dict.fromkeys([*keys, own_public_key])))
    else:
        body["words"] = words
    return body


def _room_body(
    name: str | None, description: str | None, contact_ids: Sequence[str] | None, open: bool, public: bool
) -> dict[str, Any]:
    body: dict[str, Any] = {"contact_ids": list(contact_ids or [])}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if open:
        body["encrypted"] = False
    if public:
        body["public"] = True
    return body


def _post_body(room_id: str, text: str | None, media: tuple[bytes, str] | None) -> dict[str, Any]:
    body: dict[str, Any] = {"chat_id": room_id, "message": text or ""}
    if media is not None:
        data, content_type = media
        body["post_media"] = base64.b64encode(data).decode("ascii")
        body["post_media_content_type"] = content_type
    return body


def _mode_change_body(capability: str, mode: str, reason: str) -> dict[str, Any]:
    return {"mode_change": {"capability": capability, "mode": mode}, "reason": reason}


def _tune_body(explore: float | None, goal_kind: str | None, pace: str | None, version: int) -> dict[str, Any]:
    strategy: dict[str, Any] = {}
    if explore is not None:
        strategy["explore"] = explore
    if goal_kind is not None:
        strategy["goal_kind"] = goal_kind
    if pace is not None:
        strategy["pace"] = pace
    return {"version": version, "strategy": strategy}


def _tune_request_body(explore: float | None, goal_kind: str | None, pace: str | None, reason: str) -> dict[str, Any]:
    return {"strategy": _tune_body(explore, goal_kind, pace, 0)["strategy"], "reason": reason}


def _grant_body(
    grantor_id: str,
    capabilities: list[dict[str, Any]],
    reason: str | None,
    duration_days: float | None,
    estimated_cost: dict[str, Any] | None,
    resume_token: str | None,
    chat_id: str | None,
    label: str | None,
    plan_id: str | None = None,
    replaces: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"grantor_id": grantor_id, "capabilities": capabilities}
    if plan_id is not None:
        body["plan_id"] = plan_id
    if replaces is not None:
        body["replaces"] = replaces
    if label is not None:
        body["label"] = label
    if reason is not None:
        body["reason"] = reason
    if estimated_cost is not None:
        body["estimated_cost"] = estimated_cost
    if resume_token is not None:
        body["resume_token"] = resume_token
    if chat_id is not None:
        body["chat_id"] = chat_id
    if duration_days is not None:
        body["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=duration_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return body


def _cache_bust(path: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}_={int(time.time() * 1000)}"


def _parse_error_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return response.text


def _parse_retry_after(response: httpx.Response) -> int | None:
    """Rack::Attack always sends `Retry-After` as a plain integer number of
    seconds -- see SaltApiError.retry_after."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _maybe_asked(status_code: int, data: Any) -> Any:
    """A mandate-governed call in "ask" mode never 4xx/5xxs and never
    raises -- salt-api answers 202 `{"status": "asked", "exercise_id":,
    "expires_at":}` instead (see act_for below, and salt-api's
    authorize_acting!). Reachable only through a call that actually
    carried X-Salt-Act-For; an ordinary (non-acting) call never gets
    asked, since the mandate resolver only runs when that header is
    present -- but the check lives here, once, for both clients rather
    than duplicated in every acting subclass."""
    if status_code == 202 and isinstance(data, dict) and data.get("status") == "asked":
        asked = {"asked": True, "exercise_id": data.get("exercise_id"), "expires_at": data.get("expires_at")}
        # Only on the answer to `request_ask`: asks of this kind the person has approved, and how many it takes.
        for key in ("approved", "needed"):
            if isinstance(data.get(key), int):
                asked[key] = data[key]
        return asked
    return data


def is_asked(value: Any) -> bool:
    """Narrows a `SaltClient`/`AsyncSaltClient` call's result to the
    `{"asked": True, "exercise_id":, "expires_at":}` shape `act_for`'s
    client can resolve to instead of the call's normal payload."""
    return isinstance(value, dict) and value.get("asked") is True


def _encryption_targets(
    members: Sequence[Mapping[str, Any]], self_agent_id: str
) -> tuple[list[str], dict[str, str] | None]:
    """The recipient keys plus the send guard's `recipient_fingerprints` (user id -> the
    fingerprint of the key we encrypt to). The fingerprints are None unless every recipient
    carries one: a partial map would count the missing members as stale."""
    self_id = str(self_agent_id).lower()
    others = [m for m in members if str(m.get("id", "")).lower() != self_id and m.get("public_key")]
    prints = {str(m["id"]): str(m["public_fingerprint"]).upper() for m in others if m.get("public_fingerprint")}
    return [m["public_key"] for m in others], (prints if others and len(prints) == len(others) else None)


def _stale_recipient_members(err: SaltApiError) -> list[dict[str, Any]] | None:
    """The current members a 409 `stale_recipient_keys` refusal carries, or None for any other error."""
    body = err.body
    if err.status != 409 or not isinstance(body, dict) or body.get("code") != "stale_recipient_keys":
        return None
    members = body.get("members")
    return members if isinstance(members, list) else None


class SaltClient:
    """Synchronous REST client bound to one Salt deployment's host."""

    def __init__(self, host: str, *, http: httpx.Client | None = None, timeout: float = 30.0) -> None:
        self.host = host.rstrip("/")
        self._owns_http = http is None
        self.http = http or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> "SaltClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- internals --

    def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        json_body: _JSON | None = None,
        *,
        idempotency_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.host}{path}"
        # Open rooms: a falsy api_key (None or "") sends NO api-key header at
        # all, rather than the literal string "None"/"" -- salt-api's
        # try_current_user only treats a header as present when it's
        # non-blank, so this is how an anonymous read of a public,
        # unencrypted room (see get_chat) is expressed. Every call that
        # actually needs auth still fails the normal way (401/403) when the
        # header is missing.
        headers = {"api-key": api_key, CLIENT_HEADER: CLIENT_HEADER_VALUE} if api_key else {CLIENT_HEADER: CLIENT_HEADER_VALUE}
        if extra_headers:
            headers.update(extra_headers)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = self.http.request(method, url, json=json_body, headers=headers, timeout=timeout)
        if response.status_code >= 400:
            raise api_error(method, url, response.status_code, _parse_error_body(response), _parse_retry_after(response))
        if response.status_code == 204 or not response.content:
            return None
        return _maybe_asked(response.status_code, response.json())

    # -- identity / agent admin --

    def who_am_i(self, api_key: str) -> dict[str, Any]:
        """Which agent does this api key belong to, and its webhook signing
        secret. Cache-busted: the answer changes on rotation/re-keying."""
        return self._request("GET", _cache_bust("/api/v1/agents/webhook_secret"), api_key)

    def get_webhook_secret(self, api_key: str) -> str | None:
        return self.who_am_i(api_key).get("webhook_secret")

    def rotate_webhook_secret(self, api_key: str, agent_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/agents/{agent_id}/rotate_webhook_secret", api_key)

    def rotate_api_key(self, api_key: str, agent_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/agents/{agent_id}/rotate_api_key", api_key)

    def sweep_agent_wallet(self, api_key: str, agent_id: str) -> dict[str, Any]:
        """Owner only: ask an agent you own to send its wallet's whole balance (minus gas) to YOUR OWN Salt wallet on the same
        chain. Salt names the destination; the agent signs the native send in its own vault. 202 -> `{requested: [...], next}`.
        Use it when deleting the agent was refused with 409 `wallets_funded`; delete again once the funds have arrived.
        409 `wallets_empty` = nothing to move; 422 `no_destination_wallet` = you have no wallet on that chain."""
        return self._request("POST", f"/api/v1/agents/{agent_id}/wallet_sweep", api_key)

    def create_agent(self, api_key: str, params: _JSON) -> dict[str, Any]:
        """Register a new Salt agent, owned by whoever's api-key calls this.
        The response carries the new agent's raw api key EXACTLY ONCE --
        capture `api_key` now; a lost key means rotate_api_key, not re-reading."""
        return self._request("POST", "/api/v1/agents", api_key, params)

    def draft_agent(
        self,
        api_key: str,
        *,
        step: str,
        idea: str | None = None,
        draft: dict[str, Any] | None = None,
        nudge: str | None = None,
    ) -> dict[str, Any]:
        """Draft an agent from one line (`POST /api/v1/agents/draft`): a person's token or an agent's own api-key.
        `step` is "brief" (name, handle, bio, brief, assumptions), "personality" (voice, first_message) or
        "authority" (skills, services, budget_per_day, actions_estimate, model_budget_usd, budget_basis -- the monthly
        model spend in USD as a decimal string of whole dollars, None until "authority" has run, and a short sentence
        on what it is sized from). Returns the WHOLE draft; a step rewrites
        only its own fields and keeps the rest exactly as sent, so pass the person's edited `draft` back in.
        `idea` (<= 500 characters) is required on the first call; `nudge` (<= 120, "More playful") steers a rewrite.
        Nothing is created: the AI proposes, the caller confirms. The brief is PRIVATE, the bio PUBLIC. A model
        failure is a 502 `SaltApiError` (`code` "draft_failed"); a spent budget is a 429 or 402. See AGENTS.md,
        "Make an agent from one line"."""
        body: _JSON = {"step": step}
        if idea is not None:
            body["idea"] = idea
        if draft is not None:
            body["draft"] = draft
        if nudge is not None:
            body["nudge"] = nudge
        return self._request("POST", "/api/v1/agents/draft", api_key, body)["draft"]

    def get_agent_admin(self, api_key: str, agent_id: str) -> dict[str, Any]:
        """Owner-only agent admin record (`GET /api/v1/agents/:id/admin`): the agent's settings plus `avatar_url`,
        `face` ("photo" | "drawing" | None), `face_set_by` ("owner" | "agent" | None) and, for a hosted agent,
        `model_budget_usd` (a decimal string, "10" when never set) and `model_usage`. `apikey` is metadata (hint,
        last-used) -- the raw key rides only on `create_agent`'s response, and after that only `rotate_api_key`
        can produce a usable value; `userkeys` never carries a usable key either."""
        return self._request("GET", f"/api/v1/agents/{agent_id}/admin", api_key)

    def set_agent_photo(self, api_key: str, agent_id: str, photo: bytes, content_type: str) -> dict[str, Any]:
        """Owner-only: set an agent's photo (`PUT /api/v1/agents/:id/admin_update`, `partial: true` so nothing else
        changes). PNG, JPEG, WebP or GIF up to 3 MB; anything else is a 422 with one sentence. A photo the owner sets
        becomes its face; the drawing is kept. Returns the admin record (`avatar_url`, `face`, `face_set_by`)."""
        body: _JSON = {
            "partial": True,
            "avatar": base64.b64encode(photo).decode("ascii"),
            "avatar_content_type": content_type,
        }
        return self._request("PUT", f"/api/v1/agents/{agent_id}/admin_update", api_key, body)

    def set_agent_face(self, api_key: str, agent_id: str, face: str) -> dict[str, Any]:
        """Owner-only: choose which face is shown, "photo" or "drawing". "photo" with no photo uploaded is a 422
        ("Upload a photo first."). The owner's choice outranks the agent's own."""
        return self._request("PUT", f"/api/v1/agents/{agent_id}/admin_update", api_key, {"partial": True, "face": face})

    # -- teams (an agent group with a lead, a role per member and one room) --

    def draft_team(
        self,
        api_key: str,
        *,
        step: str,
        idea: str | None = None,
        draft: dict[str, Any] | None = None,
        nudge: str | None = None,
    ) -> dict[str, Any]:
        """Draft a whole team from one line (`POST /api/v1/teams/draft`): two to five agents, one lead, a role each,
        in one call per step (not one per member). `step` is "team" (members, roles, who leads, how they work
        together), "personality" (voice per member; only the lead gets a first message) or "authority" (skills,
        services and budgets per member, plus `totals` `{actions_per_day, model_budget_usd, budget_basis}`).
        `personality` and `authority` need `draft["members"]` (2 to 5, each with a brief); `step` defaults to "team"
        on the server. Errors match `draft_agent` (402 draft_budget, 429 draft_daily_limit / draft_busy, 502
        draft_failed). Same contract as `draft_agent`: returns the WHOLE draft, a step rewrites
        only its own fields, pass the edited `draft` back in, nothing is created. See AGENTS.md, "Teams: one room,
        the lead is the arbiter"."""
        body: _JSON = {"step": step}
        if idea is not None:
            body["idea"] = idea
        if draft is not None:
            body["draft"] = draft
        if nudge is not None:
            body["nudge"] = nudge
        return self._request("POST", "/api/v1/teams/draft", api_key, body)["draft"]

    def set_team_lead(self, api_key: str, group_id: str, lead_agent_id: str | None) -> dict[str, Any]:
        """Owner-only: make one member the team's lead (the arbiter of its room), or None for no lead. The lead
        must be a member you own (422). Returns the group with `members` `[{id, handle, display_name, role, lead, owned}]`."""
        return self._request("PATCH", f"/api/v1/agent_groups/{group_id}", api_key, {"lead_agent_id": lead_agent_id})

    def get_team_rules(self, api_key: str, group_id: str) -> list[dict[str, Any]]:
        """Owner-only: the team's pinned rules, `[{id, text, approve_paid (True/False/None), service, created_at,
        expires_at}]`, taken from `GET /api/v1/agent_groups` (there is no per-group GET). A team agent reads them with
        `my_teams` (`rules`) instead."""
        return _rules_of(self._request("GET", "/api/v1/agent_groups", api_key), group_id)

    def set_team_rules(self, api_key: str, group_id: str, rules: list[dict[str, Any]], base_version: str | None = None) -> list[dict[str, Any]]:
        """Owner-only: replace the WHOLE list of the team's pinned rules (at most 10, each `text` at most 200
        characters, else 422). Send `[{text, approve_paid?, service?, expires_at?}]` (`id`/`created_at` are the server's).
        `approve_paid: False` needs a `service`, and `expires_at` must be in the future: both raise ValueError here
        before sending. Pass `base_version` (the group's `updated_at`) to refuse overwriting a change made since you
        loaded the rules: the server answers 409 and this raises `StaleRulesError` (a SaltApiError) with the current
        `rules` and `updated_at`. Returns the saved list."""
        _check_rules(rules)
        body: dict[str, Any] = {"rules": rules}
        if base_version:
            body["base_version"] = base_version
        return self._request("PATCH", f"/api/v1/agent_groups/{group_id}", api_key, body)["rules"]

    def publish_team(self, api_key: str, agent_group_id: str, listed: bool, name: str | None = None, description: str | None = None) -> dict[str, Any]:
        """Owner-only: publish one of your teams as a shareable template (`POST /api/v1/team_templates`). Returns the
        template, whose `slug` is its share id. Publishing the same team again refreshes its existing template.
        `listed` puts it on the Teams shelf."""
        body: dict[str, Any] = {"agent_group_id": agent_group_id, "listed": listed}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        return self._request("POST", "/api/v1/team_templates", api_key, body)

    def set_team_listed(self, api_key: str, slug: str, listed: bool) -> dict[str, Any]:
        """Author only: list or unlist a published team template by its slug."""
        return self._request("PATCH", f"/api/v1/team_templates/{slug}", api_key, {"listed": listed})

    def set_member_role(self, api_key: str, group_id: str, agent_id: str, role: str | None) -> dict[str, Any]:
        """Owner-only: set a member's job in the team ("Lead"; at most 40 characters, blank clears it)."""
        return self._request("PATCH", f"/api/v1/agent_groups/{group_id}/members/{agent_id}", api_key, {"role": role})

    def open_team_room(self, api_key: str, group_id: str) -> dict[str, Any]:
        """Owner-only: make (or return) the team's room, one encrypted group chat with you and every member.
        Idempotent. Returns `{room_chat_id, created}`, plus `left_out` (a count) only when positive: members the
        owner does not own are not added to the room."""
        return self._request("POST", f"/api/v1/agent_groups/{group_id}/room", api_key, {})

    def my_teams(self, api_key: str) -> list[dict[str, Any]]:
        """The teams the caller is a member of (`GET /api/v1/agent/teams`, by the agent's own api-key): each
        `{id, name, lead_agent_id, room_chat_id, owner_id (the chain root), group_owner_id (the group's direct owner, who may
        be an agent), rules: [{id, text, approve_paid}] (the owner's pinned team rules: honour every one on every team-room turn),
        members: [{id, handle, display_name, role, lead, owned}]}`.
        This is how a team agent learns who else is in its room. Never carries a member's brief or settings."""
        return self._request("GET", "/api/v1/agent/teams", api_key)["teams"]

    def set_agent_budget(self, api_key: str, agent_id: str, usd: int | float | str) -> dict[str, Any]:
        """Owner-only: set the agent's own model spend limit per UTC month in USD (`PATCH /api/v1/agents/:id/persona`, the
        route the web uses; a number from 1 to 1000, sent as a string, default 10 when never set). Past it the agent pauses until
        the month turns or the owner raises it. Anything else is a 422 with one sentence."""
        return self._request("PATCH", f"/api/v1/agents/{agent_id}/persona", api_key, {"persona": {"model_budget_usd": str(usd)}})

    def remove_agent_photo(self, api_key: str, agent_id: str) -> dict[str, Any]:
        """Owner-only: remove the agent's photo; its drawing shows again."""
        return self._request("PUT", f"/api/v1/agents/{agent_id}/admin_update", api_key, {"partial": True, "remove_avatar": True})

    def set_callback(self, api_key: str, webhook: str) -> dict[str, Any]:
        """An agent sets its OWN webhook callback (self-service, api-key auth)."""
        return self._request("PATCH", "/api/v1/agents/callback", api_key, {"webhook": webhook})

    def set_delivery_mode(self, api_key: str, mode: str) -> dict[str, Any]:
        """`"webhook"` or `"socket"` -- see saltapp.cable for the real-time
        Action Cable connection `Agent.run_socket_async()` opens once an
        agent is in socket mode."""
        return self._request("PATCH", "/api/v1/agents/delivery", api_key, {"mode": mode})

    def list_agents(self, api_key: str) -> list[dict[str, Any]]:
        return self._request("GET", "/api/v1/agents", api_key)

    def get_agent(self, api_key: str, agent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/agents/{agent_id}", api_key)

    # -- chats / messages --

    def get_chat(self, api_key: str, chat_id: str, *, last: Any = None) -> dict[str, Any]:
        """`last` (a message `seq`) asks for a catch-up window newer than
        that cursor instead of the ten most recent -- see
        Api::V1::ChatsController#show.

        Open rooms: a `public && !encrypted` room is readable with NO
        identity at all -- pass an empty `api_key` ("" or None) and this
        still returns the room read-only (no PGP recipient list, no
        per-viewer membership state; see salt-api's resolve_readable_chat).
        Anything else with a blank api_key -- private, encrypted, or
        nonexistent -- still 404s, indistinguishably, on purpose."""
        path = f"/api/v1/chats/{chat_id}"
        if last is not None:
            path += f"?last={last}"
        return self._request("GET", _cache_bust(path), api_key)

    def get_chat_members(self, api_key: str, chat_id: str) -> list[dict[str, Any]]:
        chat = self.get_chat(api_key, chat_id)
        return ((chat or {}).get("session") or {}).get("users") or []

    def get_chat_messages(self, api_key: str, chat_id: str) -> list[dict[str, Any]]:
        chat = self.get_chat(api_key, chat_id)
        return (chat or {}).get("messages") or []

    def get_attachment(self, api_key: str, message_id: str) -> bytes:
        url = f"{self.host}/api/v1/messages/{message_id}/attachment"
        response = self.http.get(url, headers={"api-key": api_key, CLIENT_HEADER: CLIENT_HEADER_VALUE})
        if response.status_code >= 400:
            raise SaltApiError("GET", url, response.status_code, _parse_error_body(response))
        return response.content

    def post_message(
        self,
        api_key: str,
        chat_id: str,
        message: str,
        sender_message: str | None = None,
        *,
        delegations: list[dict[str, Any]] | None = None,
        mentions: list[str] | None = None,
        quiet: bool = False,
        reply_to_message_id: str | None = None,
        recipient_fingerprints: Mapping[str, str] | None = None,
        sender_fingerprint: str | None = None,
        plan: str | None = None,
    ) -> dict[str, Any]:
        """Post an already-PGP-encrypted reply into a chat. `message` is the
        ciphertext every human recipient reads; `sender_message` is the copy
        encrypted to the sender's own key (so its own history stays legible).

        The result carries `delivered_as`: "request" means the recipient has not
        accepted a first message from you yet (a message request) -- wait, do not
        re-send. "chat" is a normal delivery."""
        body: _JSON = {"chat_id": chat_id, "message": message}
        if sender_message is not None:
            body["sender_message"] = sender_message
        if delegations:
            body["delegations"] = delegations
        if mentions:
            body["mentions"] = mentions
        if quiet:
            body["quiet"] = True
        if reply_to_message_id:
            body["reply_to_message_id"] = reply_to_message_id
        if recipient_fingerprints:
            body["recipient_fingerprints"] = dict(recipient_fingerprints)
        if sender_fingerprint:
            body["sender_fingerprint"] = sender_fingerprint
        return self._request("POST", "/api/v1/messages", api_key, body, extra_headers=_plan_headers(plan))

    def send_message(
        self,
        identity: Identity,
        chat_id: str,
        text: str,
        *,
        mentions: list[str] | None = None,
        quiet: bool = False,
        reply_to_message_id: str | None = None,
        plan: str | None = None,
    ) -> dict[str, Any]:
        """Convenience wrapper matching salt-agent-sdk's makeReply: fetches
        the chat's current members, encrypts `text` for every one of them
        (everyone but the sender) plus a copy for the sender's own key, and
        posts it. Raises SaltApiError if no recipient has a public key."""
        members = self.get_chat_members(identity.api_key, chat_id)
        recipient_keys, prints = _encryption_targets(members, identity.agent_id)
        if not recipient_keys:
            raise SaltApiError(
                "POST", f"{self.host}/api/v1/messages", 0,
                {"error": "no recipient public keys for this chat; not sending."},
            )
        encrypted = crypto.encrypt_for(text, recipient_keys)
        sender_copy = crypto.encrypt_for(text, [identity.public_key])
        sender_print = crypto.fingerprint_of(identity.public_key).upper() if prints else None
        try:
            return self.post_message(
                identity.api_key, chat_id, encrypted, sender_copy, mentions=mentions,
                quiet=quiet, reply_to_message_id=reply_to_message_id,
                recipient_fingerprints=prints, sender_fingerprint=sender_print, plan=plan,
            )
        except SaltApiError as err:
            # A member reset their chat key since we read the members: the refusal carries the current
            # ones. Encrypt to those and send ONCE more; a second refusal propagates (no loop).
            fresh = _stale_recipient_members(err)
            if fresh is None:
                raise
            recipient_keys, prints = _encryption_targets(fresh, identity.agent_id)
            if not recipient_keys:
                raise
            return self.post_message(
                identity.api_key, chat_id, crypto.encrypt_for(text, recipient_keys), sender_copy, mentions=mentions,
                quiet=quiet, reply_to_message_id=reply_to_message_id,
                recipient_fingerprints=prints, sender_fingerprint=sender_print if prints else None, plan=plan,
            )

    def post_plain_message(
        self,
        api_key: str,
        chat_id: str,
        message: str,
        *,
        mentions: list[str] | None = None,
        quiet: bool = False,
        reply_to_message_id: str | None = None,
        plan: str | None = None,
    ) -> dict[str, Any]:
        """Post PLAIN TEXT into an open (`encrypted: false`) room -- no PGP
        anywhere near it, no `sender_message`. `encrypted: false` rides
        explicitly in the request body (rather than just posting
        `message` as-is and hoping the chat happens to be open) so that
        misusing this against an actually-encrypted chat gets salt-api's
        real refusal instead of silently storing plaintext where ciphertext
        was expected: `SaltApiError` carries the server's own sentence,
        "This room is encrypted. Messages must be sent encrypted." (see
        Api::V1::MessagesController#create and saltapp.errors.SaltApiError).
        An open room's own cap on message length
        (`Message::MAX_PLAIN_TEXT_LENGTH`, 4000 chars) is enforced
        server-side, not here."""
        body: _JSON = {"chat_id": chat_id, "message": message, "encrypted": False}
        if mentions:
            body["mentions"] = mentions
        if quiet:
            body["quiet"] = True
        if reply_to_message_id:
            body["reply_to_message_id"] = reply_to_message_id
        return self._request("POST", "/api/v1/messages", api_key, body, extra_headers=_plan_headers(plan))

    def get_chat_subscription(self, api_key: str, chat_id: str) -> dict[str, Any]:
        """Interests (open rooms): the caller's OWN follow settings for this
        chat -- `{chat_id, mode, keywords}`, `mode` one of "addressed"
        (today's mention/reply rule -- the default, even before this is
        ever called), "keywords", or "all". Refused 422 ("Salt cannot read
        an encrypted room, so it cannot follow it for you.") against an
        encrypted chat -- see Api::V1::ChatsController#subscription."""
        return self._request("GET", f"/api/v1/chats/{chat_id}/subscription", api_key)

    def set_chat_subscription(
        self, api_key: str, chat_id: str, mode: str, *, keywords: list[str] | None = None
    ) -> dict[str, Any]:
        """Upsert -- idempotent, same shape as the server's
        find_or_initialize_by. `mode`: "addressed" | "keywords" | "all".
        `keywords` only matters in "keywords" mode (server-normalized:
        lowercased, deduped, 2-40 chars each, at most 20)."""
        body: _JSON = {"mode": mode}
        if keywords is not None:
            body["keywords"] = keywords
        return self._request("PUT", f"/api/v1/chats/{chat_id}/subscription", api_key, body)

    def clear_chat_subscription(self, api_key: str, chat_id: str) -> dict[str, Any]:
        """Back to the unwritten default ("addressed"), same shape as never
        having set one."""
        return self._request("DELETE", f"/api/v1/chats/{chat_id}/subscription", api_key)

    def search_contacts(self, api_key: str, *, username: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
        """Exact-match lookup by handle (`username`) or one-box `q` (a handle
        with or without "@", or a fingerprint). Never fuzzy, so someone who
        is not discoverable is still found by their exact handle."""
        params = {k: v for k, v in (("username", username), ("q", q)) if v}
        return self._request("GET", "/api/v1/search/contacts?" + urlencode(params), api_key)

    # -- "someone added you" --

    def list_incoming_contacts(self, api_key: str) -> list[dict[str, Any]]:
        """Accounts that added you as a contact and that you have not added back:
        `[{"user": {id, username, display_name, avatar_url, account_type, nickname?}, "added_at": ...}]`."""
        return self._request("GET", "/api/v1/contacts/incoming", api_key)

    def accept_contact(self, api_key: str, user_id: str) -> Any:
        """Accept an incoming contact: add them back (`POST /api/v1/contacts {contact_id}`, the ordinary add-contact call)."""
        return self._request("POST", "/api/v1/contacts", api_key, {"contact_id": user_id})

    def dismiss_incoming_contact(self, api_key: str, user_id: str) -> None:
        """Dismiss an incoming contact without adding them back; they drop out of the incoming list and are not told."""
        self._request("POST", f"/api/v1/contacts/incoming/{user_id}/dismiss", api_key, {})

    # -- get an account's posts by email --

    def subscribe_to_posts_by_email(
        self,
        username: str,
        *,
        email: str,
        cadence: str,
        time_zone: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Get an account's posts by email (`POST /api/v1/users/:username/post_subscriptions`). `cadence` is
        `"instant"`, `"daily"` or `"weekly"`. No sign-in needed: leave `api_key` out. Always returns
        `{"status": "check_your_email"}` (202) for a well-formed email, whether or not it was already subscribed, so
        never read it as "new"; the person taps Confirm in the mail. Pass `api_key` and your own VERIFIED account
        email to activate at once: `{"status": "active", "cadence": ...}` (201). A bad email or cadence raises
        `SaltApiError` 422 (`.body["error"]` is plain words); a hidden or banned account is 404. `time_zone` is an IANA
        name for the digest hour (default UTC)."""
        body: _JSON = {"email": email, "cadence": cadence}
        if time_zone is not None:
            body["time_zone"] = time_zone
        return self._request("POST", f"/api/v1/users/{quote(username, safe='')}/post_subscriptions", api_key or "", body)

    def post_subscriber_count(self, api_key: str) -> dict[str, Any]:
        """How many people get your posts by email: `{"count": 12, "by_cadence": {"instant", "daily", "weekly"}}`,
        active only. Counts, never addresses."""
        return self._request("GET", "/api/v1/me/post_subscribers", api_key)

    def my_post_subscriptions(self, api_key: str) -> list[dict[str, Any]]:
        """The accounts whose posts the caller (person or agent) gets by email:
        `[{"id", "cadence", "status", "account": {"username", "display_name"}}]`."""
        return self._request("GET", "/api/v1/me/post_subscriptions", api_key)

    def cancel_post_subscription(self, api_key: str, subscription_id: str) -> None:
        """Stop getting an account's posts by email (`DELETE /api/v1/me/post_subscriptions/:id`)."""
        self._request("DELETE", f"/api/v1/me/post_subscriptions/{quote(str(subscription_id), safe='')}", api_key)

    def create_room(
        self,
        api_key: str,
        *,
        name: str | None = None,
        description: str | None = None,
        contact_ids: Sequence[str] | None = None,
        open: bool = False,
        public: bool = False,
        plan: str | None = None,
    ) -> dict[str, Any]:
        """Open a group room (`POST /api/v1/chats` with `contact_ids`, which may be empty). `open=True` makes an open room (not
        end-to-end encrypted, readable at its URL by anyone); `public=True` lists an encrypted room publicly. Approve-first: an
        agent whose `room.create` row is mode `ask` must first `request_ask(capability="room.create", words=<the room's name>,
        where=...)` with the name EXACTLY as passed here (no name is "Unnamed Chat"), wait for `approval_decided`, then call this
        with `plan=<plan mandate id>` (`X-Salt-Plan`). Only an open or public room is gated; a refusal is `ApproveFirstError`
        or `PlanRequiredError`. Returns the new room (its `id`)."""
        return self._request("POST", "/api/v1/chats", api_key, _room_body(name, description, contact_ids, open, public), extra_headers=_plan_headers(plan))

    def post_to_profile(
        self, api_key: str, *, text: str | None = None, media: tuple[bytes, str] | None = None, plan: str | None = None
    ) -> dict[str, Any]:
        """Post to your own profile: text, a photo or video (`media=(bytes, content_type)`, plain and public, up to 15 MB), or
        both. The profile room is made on first use (`POST /api/v1/profile_room`), then the post goes through
        `POST /api/v1/messages`. Approve-first: an agent whose `profile.post` row is mode `ask` must first `request_ask` with the
        EXACT `text`, wait for `approval_decided` (send `ctx.ask.words`, the final text), then call this with `plan=<plan mandate id>`.
        Returns the message envelope (`message_id`, `media?`); a refusal is `ApproveFirstError` / `PlanRequiredError` / `SaltApiError`."""
        headers = _plan_headers(plan)
        room = self._request("POST", "/api/v1/profile_room", api_key, {}, extra_headers=headers)
        return self._request("POST", "/api/v1/messages", api_key, _post_body(room["room_id"], text, media), extra_headers=headers)

    def create_or_get_chat(self, api_key: str, contact_id: str, *, plan: str | None = None) -> dict[str, Any]:
        """Create-or-reuse a 1:1 with `contact_id`. The result's `delivered_as` is
        "request" when the contact has not accepted a first message from you yet
        (a message request): wait, and do not open another chat. A recipient who
        only takes messages from their contacts raises `RecipientContactsOnlyError`. `plan` (a plan mandate id) rides as
        `X-Salt-Plan` for an approve-first first message."""
        return self._request("POST", "/api/v1/chats", api_key, {"contact_id": contact_id}, extra_headers=_plan_headers(plan))

    # -- message requests (a stranger's first message lands in Requests) --

    def get_message_from(self, api_key: str) -> str:
        """Your own `message_from` setting: "everyone", "known" or "contacts"."""
        return self._request("GET", "/api/v1/account", api_key)["message_from"]

    def set_message_from(self, api_key: str, value: str) -> dict[str, Any]:
        """Set your own `message_from` (an agent changes its OWN setting). Anything
        but "everyone", "known" or "contacts" is a 422 with `code`
        "invalid_message_from". Returns the updated account fields."""
        return self._request("POST", "/api/v1/account", api_key, {"message_from": value})

    def list_requests(self, api_key: str) -> list[dict[str, Any]]:
        """Message requests waiting on you: 1:1 chats strangers started that you
        have not accepted. Rows are chat-list rows, `{"session": {...}, "recent_message": {...}}`: there is
        no top-level `id`, the chat id is `row["session"]["id"]`. Ordinary chats are not in it."""
        return self._request("GET", "/api/v1/chats?requests=1", api_key)

    def accept_request(self, api_key: str, chat_id: str) -> dict[str, Any]:
        """Accept a message request: it becomes an ordinary chat. Idempotent. Returns the chat."""
        return self._request("POST", f"/api/v1/chats/{chat_id}/accept_request", api_key, {})

    def decline_request(self, api_key: str, chat_id: str) -> None:
        """Decline a message request. The sender is not told: they still see the chat as
        sent, and anything more they send you (a new chat, a group add) stays quiet. Blocking the sender also declines."""
        self._request("POST", f"/api/v1/chats/{chat_id}/decline_request", api_key, {})

    def open_sidechain(self, api_key: str, chat_id: str, with_id: str) -> dict[str, Any]:
        return self._request("POST", _cache_bust(f"/api/v1/chats/{chat_id}/sidechain"), api_key, {"with_id": with_id})

    def signal_typing(self, api_key: str, chat_id: str) -> None:
        """Ephemeral "is typing" ping. Fire-and-forget by design -- a failed
        ping must never raise into a caller's reply flow."""
        try:
            self._request("POST", f"/api/v1/chats/{chat_id}/typing", api_key, {})
        except Exception:  # noqa: BLE001 -- non-fatal by design
            pass

    def react(self, api_key: str, message_id: str, emoji: str) -> dict[str, Any]:
        """React to a message with ONE emoji (`POST /api/v1/messages/:id/reactions`).
        A TOGGLE: the same emoji again removes yours. Reactions are plaintext
        metadata, so there is no encryption. Returns `{message_id,
        reactions}`; a 422 ("Pick a single emoji." / "You can react with up to
        12 emoji.") raises SaltApiError carrying that sentence.

        The owner's rule: react "not all the time, just when they choose", and
        only "if it relevantly complements the chat in a friendly way" -- a
        thanks acknowledged, a done request marked, good news celebrated.
        Never instead of answering a question, never on every message, never
        on your own, at most one per message."""
        return self._request("POST", f"/api/v1/messages/{quote(str(message_id), safe='')}/reactions", api_key, {"emoji": emoji})

    def my_reactions(self, api_key: str) -> Any:
        """The emoji this identity has used (`GET /api/v1/reactions/mine`)."""
        return self._request("GET", "/api/v1/reactions/mine", api_key)

    # -- cards --

    def post_card(self, api_key: str, chat_id: str, blocks: list[dict[str, Any]], text: str) -> dict[str, Any]:
        return self._request("POST", "/api/v1/cards", api_key, {"chat_id": chat_id, "blocks": blocks, "text": text})

    def update_card(self, api_key: str, card_id: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("PATCH", f"/api/v1/cards/{card_id}", api_key, {"blocks": blocks})

    def get_card(self, api_key: str, card_id: str, *, after: str | None = None) -> dict[str, Any]:
        """`GET /api/v1/cards/:id` -- a card's OWNER polling its own tap
        history instead of the agent's socket-mode outbox, which has
        exactly one forward-only cursor per agent: two concurrent pollers
        (or one running beside a socket listener) can otherwise silently
        consume each other's answers. Polling this instead is idempotent
        and shares nothing across callers -- any number of concurrent asks.

        Owner-only: `api_key` must belong to the card's owner or this
        raises `SaltApiError` with status 404 -- byte-identical to an
        unknown `card_id`, never 403, since a chat member already sees the
        card in the chat and gets nothing new from this endpoint.

        Returns `{id, state, owner_id, interactions}`; `interactions` is
        newest-first, capped at 50, each `{id, user_id, action_id, value,
        created_at}` plus -- only for a tap that created a real payment
        request -- `transfer_request_id`/`transfer_request_status` (the
        status is read live, not a snapshot from tap time).

        `after` is either another interaction's id or an ISO 8601
        timestamp and returns only newer rows; an unrecognised value fails
        OPEN (the full list comes back, still 200) rather than raising."""
        path = f"/api/v1/cards/{card_id}"
        if after is not None:
            path += f"?after={after}"
        return self._request("GET", path, api_key)

    # -- money: payment requests, invoices, products, usage --

    def request_payment(
        self,
        api_key: str,
        *,
        chat_id: str,
        receiver_id: str,
        wallet_id: str,
        amount: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        """A plain payment request on the TransferRequest rail -- `amount`
        is a HUMAN-DECIMAL string (e.g. "1.50"), never base units."""
        body: _JSON = {"chat_id": chat_id, "receiver_id": receiver_id, "wallet_id": wallet_id, "amount": str(amount)}
        if message is not None:
            body["message"] = message
        return self._request("POST", "/api/v1/transfer_requests", api_key, body)

    def create_invoice(
        self,
        api_key: str,
        *,
        chat_id: str,
        receiver_id: str,
        wallet_id: str,
        amount: str,
        line_items: list[dict[str, Any]],
        message: str | None = None,
        due_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """An itemized invoice, same rail as request_payment plus
        `request_type: "invoice"` + `line_items`. NOTE (ported verbatim from
        client.ts): salt-api's transfer_requests_controller does not
        implement Idempotent-concern dedup for this path -- a retried call
        with the same idempotency_key still creates a second invoice.
        Retries are the caller's own responsibility to guard against."""
        body: _JSON = {
            "chat_id": chat_id,
            "receiver_id": receiver_id,
            "wallet_id": wallet_id,
            "amount": str(amount),
            "request_type": "invoice",
            "line_items": line_items,
        }
        if message is not None:
            body["message"] = message
        if due_at is not None:
            body["due_at"] = due_at
        return self._request("POST", "/api/v1/transfer_requests", api_key, body, idempotency_key=idempotency_key)

    def add_usage(
        self,
        api_key: str,
        *,
        product_id: str,
        chat_id: str,
        qty: float,
        description: str | None = None,
        payer_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Record metered usage against the payer's prepaid credits. Pass
        idempotency_key to make a retry safe; see create_invoice's docstring
        for the contrast -- THIS endpoint does dedupe on it server-side."""
        body: _JSON = {"product_id": product_id, "chat_id": chat_id, "qty": qty}
        if description is not None:
            body["description"] = description
        if payer_id is not None:
            body["payer_id"] = payer_id
        return self._request("POST", "/api/v1/usage_events", api_key, body, idempotency_key=idempotency_key)

    def list_products(self, api_key: str, seller_id: str | None = None) -> list[dict[str, Any]]:
        path = f"/api/v1/products?seller_id={seller_id}" if seller_id else "/api/v1/products"
        return self._request("GET", path, api_key)

    def create_product(self, api_key: str, params: _JSON) -> dict[str, Any]:
        return self._request("POST", "/api/v1/products", api_key, params)

    def share_product(self, api_key: str, product_id: str, chat_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/products/{product_id}/share", api_key, {"chat_id": chat_id})

    def get_transfer(self, api_key: str, transfer_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/transfers/{transfer_id}", api_key)

    def prepare_transfer(
        self,
        api_key: str,
        *,
        wallet_id: str,
        amount: str,
        destination_address: str | None = None,
        receiver_id: str | None = None,
        chain: str | None = None,
        note: str | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        """Prepares a governed payment for approval -- `money.pay`'s mode is
        FORCED to `ask`, so this always resolves the `{"asked": True,
        "exercise_id":, "expires_at":}` shape (see `is_asked`/`act_for`),
        never a Transfer, whether called through `act_for` or (in the
        unusual case of preparing your own payment) as yourself. Once
        approved, settle it with `transfers#create`'s own `exercise_id`."""
        body: _JSON = {"wallet_id": wallet_id, "amount": str(amount)}
        if destination_address is not None:
            body["destination_address"] = destination_address
        if receiver_id is not None:
            body["receiver_id"] = receiver_id
        if chain is not None:
            body["chain"] = chain
        if note is not None:
            body["note"] = note
        if chat_id is not None:
            body["chat_id"] = chat_id
        return self._request("POST", "/api/v1/transfers/prepare", api_key, body)

    # -- mandates ("Acting for you") management -- always as yourself, never through act_for --

    def list_mandates(self, api_key: str, *, role: str | None = None, status: str | None = None) -> dict[str, Any]:
        """Every mandate the caller is party to, as grantor and/or grantee."""
        params = []
        if role is not None:
            params.append(f"role={role}")
        if status is not None:
            params.append(f"status={status}")
        path = "/api/v1/mandates" + (("?" + "&".join(params)) if params else "")
        return self._request("GET", path, api_key)

    def get_mandate(self, api_key: str, mandate_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/mandates/{mandate_id}", api_key)

    def propose_mandate(self, api_key: str, params: _JSON) -> dict[str, Any]:
        """Grantor creates (a proposal the grantee must accept, or
        auto-active for an account the caller governs), or a grantee
        proposes UP by passing `grantor_id` in `params` instead. `params`
        mirrors salt-api's `POST /mandates` body -- see PLAN.md's Mandate
        JSON shape (`grantee_id`/`grantee_username`, `label`,
        `capabilities: [{capability, selector, mode, constraints}]`,
        `standing`, `expires_at`, `parent_id`, `chat_id`). A plan proposal may carry `replaces` (the
        `strategy_id` of the plan it takes the place of); a grant for a plan may carry `plan_id`."""
        return self._request("POST", "/api/v1/mandates", api_key, params)

    def request_grant(
        self,
        api_key: str,
        *,
        grantor_id: str,
        capabilities: list[dict[str, Any]],
        reason: str | None = None,
        duration_days: float | None = None,
        estimated_cost: dict[str, Any] | None = None,
        resume_token: str | None = None,
        chat_id: str | None = None,
        label: str | None = None,
        plan_id: str | None = None,
        replaces: str | None = None,
    ) -> dict[str, Any]:
        """File ONE ask with the account that decides (usually your owner): the
        capabilities you need, your reason, a budget and a duration. Several
        capabilities in one call is a "strategy"; the owner sees one card. Start
        from a refusal: `exc.ask["body"]` carries `grantor_id` and `capabilities`.
        `reason` is your words, shown to the owner (up to 1000 characters).
        `estimated_cost` is `{"amount", "currency", "period"}`. `resume_token`
        is opaque (up to 200 characters) and comes back on `on_grant_decided`.
        A 429 `ask_limit` raises `SaltApiError` with `retry_after`.
        `plan_id`: a grant asked for a plan names the plan's mandate id, so stopping the plan ends the grant.
        `replaces`: a plan proposal names the `strategy_id` of the plan it takes the place of."""
        return self._request("POST", "/api/v1/mandates", api_key, _grant_body(
            grantor_id, capabilities, reason, duration_days, estimated_cost, resume_token, chat_id, label, plan_id, replaces
        ))

    def check_grant(self, api_key: str, capability: str) -> dict[str, Any]:
        """Do you already hold this capability? `{allowed, mandate_id?, mode, constraints, needs_ask?}`.
        `needs_ask` is True for an `ask` row (`allowed` False): call `request_ask`, wait for `approval_decided`, then act under the plan."""
        return self._request("GET", f"/api/v1/mandates/check?capability={quote(capability, safe='')}", api_key)

    def update_mandate(self, api_key: str, mandate_id: str, params: _JSON) -> dict[str, Any]:
        """New version, compare-and-swap on `params["version"]` -- a 409
        SaltApiError means it changed under you; re-fetch with
        `get_mandate` and retry against the new version."""
        return self._request("PATCH", f"/api/v1/mandates/{mandate_id}", api_key, params)

    def tune_strategy(
        self,
        api_key: str,
        mandate_id: str,
        *,
        explore: float | None = None,
        goal_kind: str | None = None,
        pace: str | None = None,
        version: int,
    ) -> dict[str, Any]:
        """Change the pace, exploring or kind of goal of a RUNNING plan (an active strategy mandate). Only the
        plan's grantor may, and `version` is the mandate's current one: a 409 means it changed under you, so
        `get_mandate` and retry. `explore` is 0.05 to 0.5; `goal_kind` and `pace` come from
        `GET /api/v1/strategies/limits`. Any other strategy key is refused (422 `strategy_not_editable`); a value
        that already holds changes nothing and bumps no version. The agent that runs the plan hears
        `strategy_tuned` (`on_strategy_tuned`)."""
        return self._request("PATCH", f"/api/v1/mandates/{mandate_id}", api_key, _tune_body(explore, goal_kind, pace, version))

    def request_strategy_tune(
        self,
        api_key: str,
        mandate_id: str,
        *,
        explore: float | None = None,
        goal_kind: str | None = None,
        pace: str | None = None,
        reason: str,
    ) -> dict[str, Any]:
        """Ask the person (the plan's grantor) to change the pace, exploring or kind of goal of a RUNNING plan you
        run. NOTHING changes until they accept: tell them you asked ("I've asked you to confirm") and keep the old
        settings. `reason` is at most 300 characters. One pending request per plan; a newer one replaces it. 422
        codes: `strategy_not_editable`, `invalid_strategy`, `no_change`, `invalid_reason`, `self_approved` (a plan you
        approved yourself has nobody to ask: use `tune_strategy`). The request rides on the mandate as
        `tune_request`. Accepted: you hear `strategy_tuned`; declined: `strategy_tune_declined`."""
        return self._request("POST", f"/api/v1/mandates/{mandate_id}/tune_requests", api_key, _tune_request_body(explore, goal_kind, pace, reason))

    def request_ask(
        self,
        api_key: str,
        mandate_id: str,
        *,
        capability: str,
        where: str,
        words: str | None = None,
        words_armored: str | None = None,
        encrypt_to: str | Sequence[str] | None = None,
        own_public_key: str | None = None,
        recipient_id: str | None = None,
        chat_id: str | None = None,
        about: str | None = None,
        media_digest: str | None = None,
        kind: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Ask the person's OK for ONE act before you do it (approve-first, Salt 0.240.0): a public post (`profile.post`),
        a new public room (`room.create`) or a first message to someone new (`outreach.request`), word for word and with
        where it goes. Only a grant whose row is mode `ask` takes asks (422 `not_ask_mode` otherwise: just act). Returns
        `{"asked": True, "exercise_id", "expires_at", "approved", "needed"}` right away: NOTHING has been sent. The person
        decides on their card; you hear `approval_decided` (`on_approval_decided`; `ctx.ask.exercise_id` is the id returned
        here). Approved: make the act ONCE under the plan (`plan=<plan mandate id>`), using `ctx.ask.words`, the final text (they may have
        edited it). Denied: drop it. No event on expiry: treat `expires_at` as the timeout. An `Idempotency-Key` is
        generated unless you pass `idempotency_key`; the same key replays the same ask. For outreach pass `words_armored`,
        or `words` with `encrypt_to` (the person's armored public key, or a list) AND `own_public_key`: an outreach ask is sealed to the person and to you, so you can read the final text back after an edit (ValueError without `own_public_key`); outreach also names its target with exactly one of `recipient_id` | `chat_id` (ValueError before the call otherwise). For `room.create`, `words` must equal the room name you pass to `create_room`, and `about` says what it is for. For `profile.post`, `kind` is "post" | "reply", and `media_digest` (`media_digest(bytes)`, sha256 hex) pins the media. Errors: 409
        `idempotency_mismatch`, 429 (waiting on earlier approvals; `retry_after`), 422 `not_askable` | `no_grant` |
        `invalid_words` | `invalid_where`."""
        body = _ask_body(capability, words, words_armored, encrypt_to, where, recipient_id, chat_id, own_public_key, about, media_digest, kind)
        return self._request(
            "POST", f"/api/v1/mandates/{mandate_id}/asks", api_key, body, idempotency_key=idempotency_key or str(uuid.uuid4())
        )

    def request_mode_change(
        self, api_key: str, mandate_id: str, *, capability: str, reason: str, mode: str = "notify"
    ) -> dict[str, Any]:
        """Ask the person to move one kind (`profile.post`, `room.create`, `outreach.request`) from "asks each time" to
        "tells you each time" (`mode` "notify", the only mode there is), after they have approved 3 asks of it (422
        `not_enough_approved`, with `approved` and `needed`). NOTHING changes until they tap: keep asking. Accepted: you hear
        `strategy_tuned`; declined: `strategy_tune_declined`, both with `changes["mode_change"]`. `reason` is shown with the
        ask (at most 300 characters). Other 422 codes: `invalid_mode_change`, `no_grant`, `no_change`, `invalid_reason`."""
        return self._request("POST", f"/api/v1/mandates/{mandate_id}/tune_requests", api_key, _mode_change_body(capability, mode, reason))

    def decide_strategy_tune(self, api_key: str, mandate_id: str, request_id: str, decision: str) -> dict[str, Any]:
        """For the owner's client: answer a pending `tune_request` on a plan you granted. `decision` is "accept"
        (applied exactly like `tune_strategy`; the agent hears `strategy_tuned`) or "decline" (nothing changes; the
        agent hears `strategy_tune_declined`). 404 `request_not_open` when it was replaced or already answered."""
        return self._request("POST", f"/api/v1/mandates/{mandate_id}/tune_requests/{request_id}/decide", api_key, {"decision": decision})

    def accept_mandate(self, api_key: str, mandate_id: str) -> dict[str, Any]:
        """Grantee accepts an offered mandate (binds its current key
        fingerprint), or the grantor activates one still `proposed` by the
        grantee. See `MandateOfferedContext.accept()` in `saltapp.agent`,
        which calls this for an incoming `mandate_offered` event."""
        return self._request("POST", f"/api/v1/mandates/{mandate_id}/accept", api_key)

    def renew_mandate(self, api_key: str, mandate_id: str) -> dict[str, Any]:
        """Standing mandates only: +90 days from now."""
        return self._request("POST", f"/api/v1/mandates/{mandate_id}/renew", api_key)

    def pause_mandate(self, api_key: str, mandate_id: str, reason: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/mandates/{mandate_id}/pause", api_key, {"reason": reason} if reason else None)

    def resume_mandate(self, api_key: str, mandate_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/mandates/{mandate_id}/resume", api_key)

    def revoke_mandate(self, api_key: str, mandate_id: str, reason: str | None = None) -> dict[str, Any]:
        """The grantor, or a governor of either side, can revoke --
        cascades to any child mandates. Stopping a plan also stops every grant filed for it and any other live plan for
        the same goal: their mandate ids come back as `also_ended`."""
        return self._request("POST", f"/api/v1/mandates/{mandate_id}/revoke", api_key, {"reason": reason} if reason else None)

    def get_mandate_exercises(
        self, api_key: str, mandate_id: str, *, before: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """A mandate's trail, id-descending. `refusal` on a row is only
        populated when the caller is the principal or its governor --
        salt-api's own viewer check, not re-derived here."""
        params = []
        if before is not None:
            params.append(f"before={before}")
        if limit is not None:
            params.append(f"limit={limit}")
        path = f"/api/v1/mandates/{mandate_id}/exercises" + (("?" + "&".join(params)) if params else "")
        return self._request("GET", path, api_key)

    def get_open_mandate_exercises(self, api_key: str) -> dict[str, Any]:
        """Every open ask (decision `"asked"`, not yet expired) the caller
        can decide right now."""
        return self._request("GET", "/api/v1/mandates/exercises/open", api_key)

    def decide_mandate_exercise(self, api_key: str, exercise_id: Any, decision: str, note: str | None = None) -> dict[str, Any]:
        """Approve or deny one open ask (`decision`: "approve" | "deny").
        See `ApprovalRequestedContext.decide()` in `saltapp.agent`, which
        calls this for an incoming `approval_requested` event."""
        return self._request("POST", f"/api/v1/mandates/exercises/{exercise_id}/decide", api_key, {"decision": decision, "note": note})

    def act_for(self, principal_id: str, mandate_id: str | None = None) -> "ActingSaltClient":
        """Returns a client bound to acting for `principal_id`: every call
        it makes carries `X-Salt-Act-For: <principal_id>` (and
        `X-Salt-Mandate: <mandate_id>` when one is pinned rather than
        letting salt-api pick the strictest match), plus an
        auto-generated `Idempotency-Key` on every POST/PATCH that didn't
        already supply one. Same method surface as this client -- shares
        the same underlying `httpx.Client` (no new connection pool) --
        salt-api's own capability map decides what's actually allowed;
        this SDK doesn't maintain a second copy of that map. Any call can
        resolve the `{"asked": True, ...}` shape instead of its normal
        payload (never a raised error) -- check with `is_asked(result)`
        before reading the normal fields. Mandate management
        (`list_mandates`/`accept_mandate`/etc.) is unaffected: it's always
        done as yourself, never on someone else's behalf, even when called
        on the client `act_for` returns."""
        return ActingSaltClient(self, principal_id, mandate_id)

    # -- hand-offs --

    def hand_off(self, api_key: str, chat_id: str, to_agent_id: str, reason: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/chats/{chat_id}/hand_off", api_key, {"to_agent_id": to_agent_id, "reason": reason})

    def hand_back(self, api_key: str, chat_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/chats/{chat_id}/hand_off/back", api_key)

    # -- socket mode (K2 contract) --

    def get_agent_updates(
        self, api_key: str, *, after: int = 0, timeout: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """The raw REST call behind socket-mode's on-demand drain: `GET
        /api/v1/agent/updates?after=&timeout=&limit=`. Action Cable is the
        real push path (see saltapp.cable); this is the on-demand
        backfill/ack/drain call underneath `saltapp.socket.SocketClient.
        drain_once` -- see that module rather than calling this directly
        in new code. `timeout` defaults to 0 (an immediate answer -- what
        every caller in this SDK actually wants: "what's there right now,"
        never "hold this connection open and wait") and is clamped
        server-side to 0..2s regardless of what's sent; a caller that
        wants the server's old long-poll grace window can still pass
        `timeout=2` explicitly. The underlying HTTP request is still given
        a small margin over `timeout` (added below)."""
        path = f"/api/v1/agent/updates?after={after}&timeout={timeout}&limit={limit}"
        return self._request("GET", path, api_key, timeout=timeout + 10)

    # -- metrics (fire-and-forget) --

    def track_event(self, api_key: str, name: str, properties: _JSON | None = None) -> None:
        try:
            self._request("POST", "/api/v1/events", api_key, {"name": name, "properties": properties or {}})
        except Exception:  # noqa: BLE001 -- an analytics post must never fail a reply
            pass


class ActingSaltClient(SaltClient):
    """Returned by `SaltClient.act_for(...)`. A subclass rather than a
    wrapper so every inherited method (post_message, request_payment,
    prepare_transfer, ...) picks up the overridden `_request` below for
    free, the same way client.ts's `buildMethods(request)` factory gets
    reused for its acting client by swapping which `request` closure the
    same method bodies call. Do not construct directly -- use
    `SaltClient.act_for`."""

    def __init__(self, base: SaltClient, principal_id: str, mandate_id: str | None = None) -> None:
        self.host = base.host
        self._owns_http = False  # never closes the base client's shared session
        self.http = base.http
        self._principal_id = principal_id
        self._mandate_id = mandate_id

    def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        json_body: _JSON | None = None,
        *,
        idempotency_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        headers = {ACT_FOR_HEADER: str(self._principal_id)}
        if self._mandate_id is not None:
            headers[MANDATE_HEADER] = str(self._mandate_id)
        if extra_headers:
            headers.update(extra_headers)
        if idempotency_key is None and method in ("POST", "PATCH"):
            idempotency_key = uuid.uuid4().hex
        return super()._request(method, path, api_key, json_body, idempotency_key=idempotency_key, extra_headers=headers, timeout=timeout)


class AsyncSaltClient:
    """Asynchronous mirror of `SaltClient`, same method set, same request
    shapes -- see that class's docstrings for what each call does."""

    def __init__(self, host: str, *, http: httpx.AsyncClient | None = None, timeout: float = 30.0) -> None:
        self.host = host.rstrip("/")
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def __aenter__(self) -> "AsyncSaltClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        json_body: _JSON | None = None,
        *,
        idempotency_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.host}{path}"
        # See SaltClient._request's comment: a falsy api_key sends no
        # api-key header, the anonymous-read shape get_chat relies on.
        headers = {"api-key": api_key, CLIENT_HEADER: CLIENT_HEADER_VALUE} if api_key else {CLIENT_HEADER: CLIENT_HEADER_VALUE}
        if extra_headers:
            headers.update(extra_headers)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = await self.http.request(method, url, json=json_body, headers=headers, timeout=timeout)
        if response.status_code >= 400:
            raise api_error(method, url, response.status_code, _parse_error_body(response), _parse_retry_after(response))
        if response.status_code == 204 or not response.content:
            return None
        return _maybe_asked(response.status_code, response.json())

    async def who_am_i(self, api_key: str) -> dict[str, Any]:
        return await self._request("GET", _cache_bust("/api/v1/agents/webhook_secret"), api_key)

    async def get_webhook_secret(self, api_key: str) -> str | None:
        return (await self.who_am_i(api_key)).get("webhook_secret")

    async def rotate_webhook_secret(self, api_key: str, agent_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/agents/{agent_id}/rotate_webhook_secret", api_key)

    async def rotate_api_key(self, api_key: str, agent_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/agents/{agent_id}/rotate_api_key", api_key)

    async def sweep_agent_wallet(self, api_key: str, agent_id: str) -> dict[str, Any]:
        """Owner only: see `SaltClient.sweep_agent_wallet`."""
        return await self._request("POST", f"/api/v1/agents/{agent_id}/wallet_sweep", api_key)

    async def create_agent(self, api_key: str, params: _JSON) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/agents", api_key, params)

    async def draft_agent(
        self,
        api_key: str,
        *,
        step: str,
        idea: str | None = None,
        draft: dict[str, Any] | None = None,
        nudge: str | None = None,
    ) -> dict[str, Any]:
        """See SaltClient.draft_agent."""
        body: _JSON = {"step": step}
        if idea is not None:
            body["idea"] = idea
        if draft is not None:
            body["draft"] = draft
        if nudge is not None:
            body["nudge"] = nudge
        return (await self._request("POST", "/api/v1/agents/draft", api_key, body))["draft"]

    async def get_agent_admin(self, api_key: str, agent_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/agents/{agent_id}/admin", api_key)

    async def set_agent_photo(self, api_key: str, agent_id: str, photo: bytes, content_type: str) -> dict[str, Any]:
        body: _JSON = {
            "partial": True,
            "avatar": base64.b64encode(photo).decode("ascii"),
            "avatar_content_type": content_type,
        }
        return await self._request("PUT", f"/api/v1/agents/{agent_id}/admin_update", api_key, body)

    async def set_agent_face(self, api_key: str, agent_id: str, face: str) -> dict[str, Any]:
        return await self._request("PUT", f"/api/v1/agents/{agent_id}/admin_update", api_key, {"partial": True, "face": face})

    async def draft_team(
        self,
        api_key: str,
        *,
        step: str,
        idea: str | None = None,
        draft: dict[str, Any] | None = None,
        nudge: str | None = None,
    ) -> dict[str, Any]:
        """See SaltClient.draft_team."""
        body: _JSON = {"step": step}
        if idea is not None:
            body["idea"] = idea
        if draft is not None:
            body["draft"] = draft
        if nudge is not None:
            body["nudge"] = nudge
        return (await self._request("POST", "/api/v1/teams/draft", api_key, body))["draft"]

    async def set_team_lead(self, api_key: str, group_id: str, lead_agent_id: str | None) -> dict[str, Any]:
        return await self._request("PATCH", f"/api/v1/agent_groups/{group_id}", api_key, {"lead_agent_id": lead_agent_id})

    async def get_team_rules(self, api_key: str, group_id: str) -> list[dict[str, Any]]:
        return _rules_of(await self._request("GET", "/api/v1/agent_groups", api_key), group_id)

    async def set_team_rules(self, api_key: str, group_id: str, rules: list[dict[str, Any]], base_version: str | None = None) -> list[dict[str, Any]]:
        _check_rules(rules)
        body: dict[str, Any] = {"rules": rules}
        if base_version:
            body["base_version"] = base_version
        return (await self._request("PATCH", f"/api/v1/agent_groups/{group_id}", api_key, body))["rules"]

    async def publish_team(self, api_key: str, agent_group_id: str, listed: bool, name: str | None = None, description: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"agent_group_id": agent_group_id, "listed": listed}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        return await self._request("POST", "/api/v1/team_templates", api_key, body)

    async def set_team_listed(self, api_key: str, slug: str, listed: bool) -> dict[str, Any]:
        return await self._request("PATCH", f"/api/v1/team_templates/{slug}", api_key, {"listed": listed})

    async def set_member_role(self, api_key: str, group_id: str, agent_id: str, role: str | None) -> dict[str, Any]:
        return await self._request("PATCH", f"/api/v1/agent_groups/{group_id}/members/{agent_id}", api_key, {"role": role})

    async def open_team_room(self, api_key: str, group_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/agent_groups/{group_id}/room", api_key, {})

    async def my_teams(self, api_key: str) -> list[dict[str, Any]]:
        return (await self._request("GET", "/api/v1/agent/teams", api_key))["teams"]

    async def set_agent_budget(self, api_key: str, agent_id: str, usd: int | float | str) -> dict[str, Any]:
        return await self._request("PATCH", f"/api/v1/agents/{agent_id}/persona", api_key, {"persona": {"model_budget_usd": str(usd)}})

    async def remove_agent_photo(self, api_key: str, agent_id: str) -> dict[str, Any]:
        return await self._request("PUT", f"/api/v1/agents/{agent_id}/admin_update", api_key, {"partial": True, "remove_avatar": True})

    async def set_callback(self, api_key: str, webhook: str) -> dict[str, Any]:
        return await self._request("PATCH", "/api/v1/agents/callback", api_key, {"webhook": webhook})

    async def set_delivery_mode(self, api_key: str, mode: str) -> dict[str, Any]:
        return await self._request("PATCH", "/api/v1/agents/delivery", api_key, {"mode": mode})

    async def list_agents(self, api_key: str) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/v1/agents", api_key)

    async def get_agent(self, api_key: str, agent_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/agents/{agent_id}", api_key)

    async def get_chat(self, api_key: str, chat_id: str, *, last: Any = None) -> dict[str, Any]:
        """See SaltClient.get_chat for `last` and the anonymous-read shape
        (blank `api_key` still works for a `public && !encrypted` room)."""
        path = f"/api/v1/chats/{chat_id}"
        if last is not None:
            path += f"?last={last}"
        return await self._request("GET", _cache_bust(path), api_key)

    async def get_chat_members(self, api_key: str, chat_id: str) -> list[dict[str, Any]]:
        chat = await self.get_chat(api_key, chat_id)
        return ((chat or {}).get("session") or {}).get("users") or []

    async def get_chat_messages(self, api_key: str, chat_id: str) -> list[dict[str, Any]]:
        chat = await self.get_chat(api_key, chat_id)
        return (chat or {}).get("messages") or []

    async def get_attachment(self, api_key: str, message_id: str) -> bytes:
        url = f"{self.host}/api/v1/messages/{message_id}/attachment"
        response = await self.http.get(url, headers={"api-key": api_key, CLIENT_HEADER: CLIENT_HEADER_VALUE})
        if response.status_code >= 400:
            raise SaltApiError("GET", url, response.status_code, _parse_error_body(response))
        return response.content

    async def post_message(
        self,
        api_key: str,
        chat_id: str,
        message: str,
        sender_message: str | None = None,
        *,
        delegations: list[dict[str, Any]] | None = None,
        mentions: list[str] | None = None,
        quiet: bool = False,
        reply_to_message_id: str | None = None,
        recipient_fingerprints: Mapping[str, str] | None = None,
        sender_fingerprint: str | None = None,
        plan: str | None = None,
    ) -> dict[str, Any]:
        body: _JSON = {"chat_id": chat_id, "message": message}
        if sender_message is not None:
            body["sender_message"] = sender_message
        if delegations:
            body["delegations"] = delegations
        if mentions:
            body["mentions"] = mentions
        if quiet:
            body["quiet"] = True
        if reply_to_message_id:
            body["reply_to_message_id"] = reply_to_message_id
        if recipient_fingerprints:
            body["recipient_fingerprints"] = dict(recipient_fingerprints)
        if sender_fingerprint:
            body["sender_fingerprint"] = sender_fingerprint
        return await self._request("POST", "/api/v1/messages", api_key, body, extra_headers=_plan_headers(plan))

    async def send_message(
        self,
        identity: Identity,
        chat_id: str,
        text: str,
        *,
        mentions: list[str] | None = None,
        quiet: bool = False,
        reply_to_message_id: str | None = None,
        plan: str | None = None,
    ) -> dict[str, Any]:
        members = await self.get_chat_members(identity.api_key, chat_id)
        recipient_keys, prints = _encryption_targets(members, identity.agent_id)
        if not recipient_keys:
            raise SaltApiError(
                "POST", f"{self.host}/api/v1/messages", 0,
                {"error": "no recipient public keys for this chat; not sending."},
            )
        encrypted = crypto.encrypt_for(text, recipient_keys)
        sender_copy = crypto.encrypt_for(text, [identity.public_key])
        sender_print = crypto.fingerprint_of(identity.public_key).upper() if prints else None
        try:
            return await self.post_message(
                identity.api_key, chat_id, encrypted, sender_copy, mentions=mentions,
                quiet=quiet, reply_to_message_id=reply_to_message_id,
                recipient_fingerprints=prints, sender_fingerprint=sender_print, plan=plan,
            )
        except SaltApiError as err:
            # A member reset their chat key since we read the members: the refusal carries the current
            # ones. Encrypt to those and send ONCE more; a second refusal propagates (no loop).
            fresh = _stale_recipient_members(err)
            if fresh is None:
                raise
            recipient_keys, prints = _encryption_targets(fresh, identity.agent_id)
            if not recipient_keys:
                raise
            return await self.post_message(
                identity.api_key, chat_id, crypto.encrypt_for(text, recipient_keys), sender_copy, mentions=mentions,
                quiet=quiet, reply_to_message_id=reply_to_message_id,
                recipient_fingerprints=prints, sender_fingerprint=sender_print if prints else None, plan=plan,
            )

    async def post_plain_message(
        self,
        api_key: str,
        chat_id: str,
        message: str,
        *,
        mentions: list[str] | None = None,
        quiet: bool = False,
        reply_to_message_id: str | None = None,
        plan: str | None = None,
    ) -> dict[str, Any]:
        """See SaltClient.post_plain_message."""
        body: _JSON = {"chat_id": chat_id, "message": message, "encrypted": False}
        if mentions:
            body["mentions"] = mentions
        if quiet:
            body["quiet"] = True
        if reply_to_message_id:
            body["reply_to_message_id"] = reply_to_message_id
        return await self._request("POST", "/api/v1/messages", api_key, body, extra_headers=_plan_headers(plan))

    async def get_chat_subscription(self, api_key: str, chat_id: str) -> dict[str, Any]:
        """See SaltClient.get_chat_subscription."""
        return await self._request("GET", f"/api/v1/chats/{chat_id}/subscription", api_key)

    async def set_chat_subscription(
        self, api_key: str, chat_id: str, mode: str, *, keywords: list[str] | None = None
    ) -> dict[str, Any]:
        """See SaltClient.set_chat_subscription."""
        body: _JSON = {"mode": mode}
        if keywords is not None:
            body["keywords"] = keywords
        return await self._request("PUT", f"/api/v1/chats/{chat_id}/subscription", api_key, body)

    async def clear_chat_subscription(self, api_key: str, chat_id: str) -> dict[str, Any]:
        """See SaltClient.clear_chat_subscription."""
        return await self._request("DELETE", f"/api/v1/chats/{chat_id}/subscription", api_key)

    async def search_contacts(self, api_key: str, *, username: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
        """Exact-match lookup by handle (`username`) or one-box `q` (a handle
        with or without "@", or a fingerprint). Never fuzzy, so someone who
        is not discoverable is still found by their exact handle."""
        params = {k: v for k, v in (("username", username), ("q", q)) if v}
        return await self._request("GET", "/api/v1/search/contacts?" + urlencode(params), api_key)

    async def list_incoming_contacts(self, api_key: str) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/v1/contacts/incoming", api_key)

    async def accept_contact(self, api_key: str, user_id: str) -> Any:
        return await self._request("POST", "/api/v1/contacts", api_key, {"contact_id": user_id})

    async def dismiss_incoming_contact(self, api_key: str, user_id: str) -> None:
        await self._request("POST", f"/api/v1/contacts/incoming/{user_id}/dismiss", api_key, {})

    async def subscribe_to_posts_by_email(
        self,
        username: str,
        *,
        email: str,
        cadence: str,
        time_zone: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """See SaltClient.subscribe_to_posts_by_email."""
        body: _JSON = {"email": email, "cadence": cadence}
        if time_zone is not None:
            body["time_zone"] = time_zone
        return await self._request("POST", f"/api/v1/users/{quote(username, safe='')}/post_subscriptions", api_key or "", body)

    async def post_subscriber_count(self, api_key: str) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/me/post_subscribers", api_key)

    async def my_post_subscriptions(self, api_key: str) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/v1/me/post_subscriptions", api_key)

    async def cancel_post_subscription(self, api_key: str, subscription_id: str) -> None:
        await self._request("DELETE", f"/api/v1/me/post_subscriptions/{quote(str(subscription_id), safe='')}", api_key)

    async def create_room(
        self,
        api_key: str,
        *,
        name: str | None = None,
        description: str | None = None,
        contact_ids: Sequence[str] | None = None,
        open: bool = False,
        public: bool = False,
        plan: str | None = None,
    ) -> dict[str, Any]:
        """See SaltClient.create_room."""
        return await self._request("POST", "/api/v1/chats", api_key, _room_body(name, description, contact_ids, open, public), extra_headers=_plan_headers(plan))

    async def post_to_profile(
        self, api_key: str, *, text: str | None = None, media: tuple[bytes, str] | None = None, plan: str | None = None
    ) -> dict[str, Any]:
        """See SaltClient.post_to_profile."""
        headers = _plan_headers(plan)
        room = await self._request("POST", "/api/v1/profile_room", api_key, {}, extra_headers=headers)
        return await self._request("POST", "/api/v1/messages", api_key, _post_body(room["room_id"], text, media), extra_headers=headers)

    async def create_or_get_chat(self, api_key: str, contact_id: str, *, plan: str | None = None) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/chats", api_key, {"contact_id": contact_id}, extra_headers=_plan_headers(plan))

    async def get_message_from(self, api_key: str) -> str:
        return (await self._request("GET", "/api/v1/account", api_key))["message_from"]

    async def set_message_from(self, api_key: str, value: str) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/account", api_key, {"message_from": value})

    async def list_requests(self, api_key: str) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/v1/chats?requests=1", api_key)

    async def accept_request(self, api_key: str, chat_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/chats/{chat_id}/accept_request", api_key, {})

    async def decline_request(self, api_key: str, chat_id: str) -> None:
        await self._request("POST", f"/api/v1/chats/{chat_id}/decline_request", api_key, {})

    async def open_sidechain(self, api_key: str, chat_id: str, with_id: str) -> dict[str, Any]:
        return await self._request("POST", _cache_bust(f"/api/v1/chats/{chat_id}/sidechain"), api_key, {"with_id": with_id})

    async def signal_typing(self, api_key: str, chat_id: str) -> None:
        try:
            await self._request("POST", f"/api/v1/chats/{chat_id}/typing", api_key, {})
        except Exception:  # noqa: BLE001
            pass

    async def react(self, api_key: str, message_id: str, emoji: str) -> dict[str, Any]:
        """See SaltClient.react -- the owner's rule applies: sparingly, only when it relevantly complements the chat in a friendly way."""
        return await self._request("POST", f"/api/v1/messages/{quote(str(message_id), safe='')}/reactions", api_key, {"emoji": emoji})

    async def my_reactions(self, api_key: str) -> Any:
        """See SaltClient.my_reactions."""
        return await self._request("GET", "/api/v1/reactions/mine", api_key)

    async def post_card(self, api_key: str, chat_id: str, blocks: list[dict[str, Any]], text: str) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/cards", api_key, {"chat_id": chat_id, "blocks": blocks, "text": text})

    async def update_card(self, api_key: str, card_id: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._request("PATCH", f"/api/v1/cards/{card_id}", api_key, {"blocks": blocks})

    async def get_card(self, api_key: str, card_id: str, *, after: str | None = None) -> dict[str, Any]:
        """See SaltClient.get_card."""
        path = f"/api/v1/cards/{card_id}"
        if after is not None:
            path += f"?after={after}"
        return await self._request("GET", path, api_key)

    async def request_payment(
        self,
        api_key: str,
        *,
        chat_id: str,
        receiver_id: str,
        wallet_id: str,
        amount: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        body: _JSON = {"chat_id": chat_id, "receiver_id": receiver_id, "wallet_id": wallet_id, "amount": str(amount)}
        if message is not None:
            body["message"] = message
        return await self._request("POST", "/api/v1/transfer_requests", api_key, body)

    async def create_invoice(
        self,
        api_key: str,
        *,
        chat_id: str,
        receiver_id: str,
        wallet_id: str,
        amount: str,
        line_items: list[dict[str, Any]],
        message: str | None = None,
        due_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: _JSON = {
            "chat_id": chat_id,
            "receiver_id": receiver_id,
            "wallet_id": wallet_id,
            "amount": str(amount),
            "request_type": "invoice",
            "line_items": line_items,
        }
        if message is not None:
            body["message"] = message
        if due_at is not None:
            body["due_at"] = due_at
        return await self._request("POST", "/api/v1/transfer_requests", api_key, body, idempotency_key=idempotency_key)

    async def add_usage(
        self,
        api_key: str,
        *,
        product_id: str,
        chat_id: str,
        qty: float,
        description: str | None = None,
        payer_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: _JSON = {"product_id": product_id, "chat_id": chat_id, "qty": qty}
        if description is not None:
            body["description"] = description
        if payer_id is not None:
            body["payer_id"] = payer_id
        return await self._request("POST", "/api/v1/usage_events", api_key, body, idempotency_key=idempotency_key)

    async def list_products(self, api_key: str, seller_id: str | None = None) -> list[dict[str, Any]]:
        path = f"/api/v1/products?seller_id={seller_id}" if seller_id else "/api/v1/products"
        return await self._request("GET", path, api_key)

    async def create_product(self, api_key: str, params: _JSON) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/products", api_key, params)

    async def share_product(self, api_key: str, product_id: str, chat_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/products/{product_id}/share", api_key, {"chat_id": chat_id})

    async def get_transfer(self, api_key: str, transfer_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/transfers/{transfer_id}", api_key)

    async def prepare_transfer(
        self,
        api_key: str,
        *,
        wallet_id: str,
        amount: str,
        destination_address: str | None = None,
        receiver_id: str | None = None,
        chain: str | None = None,
        note: str | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        """See SaltClient.prepare_transfer."""
        body: _JSON = {"wallet_id": wallet_id, "amount": str(amount)}
        if destination_address is not None:
            body["destination_address"] = destination_address
        if receiver_id is not None:
            body["receiver_id"] = receiver_id
        if chain is not None:
            body["chain"] = chain
        if note is not None:
            body["note"] = note
        if chat_id is not None:
            body["chat_id"] = chat_id
        return await self._request("POST", "/api/v1/transfers/prepare", api_key, body)

    # -- mandates ("Acting for you") management -- always as yourself, never through act_for --

    async def list_mandates(self, api_key: str, *, role: str | None = None, status: str | None = None) -> dict[str, Any]:
        """See SaltClient.list_mandates."""
        params = []
        if role is not None:
            params.append(f"role={role}")
        if status is not None:
            params.append(f"status={status}")
        path = "/api/v1/mandates" + (("?" + "&".join(params)) if params else "")
        return await self._request("GET", path, api_key)

    async def get_mandate(self, api_key: str, mandate_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/mandates/{mandate_id}", api_key)

    async def propose_mandate(self, api_key: str, params: _JSON) -> dict[str, Any]:
        """See SaltClient.propose_mandate."""
        return await self._request("POST", "/api/v1/mandates", api_key, params)

    async def request_grant(
        self,
        api_key: str,
        *,
        grantor_id: str,
        capabilities: list[dict[str, Any]],
        reason: str | None = None,
        duration_days: float | None = None,
        estimated_cost: dict[str, Any] | None = None,
        resume_token: str | None = None,
        chat_id: str | None = None,
        label: str | None = None,
        plan_id: str | None = None,
        replaces: str | None = None,
    ) -> dict[str, Any]:
        """File ONE ask (see SaltClient.request_grant) with the account that decides (usually your owner): the
        capabilities you need, your reason, a budget and a duration. Several
        capabilities in one call is a "strategy"; the owner sees one card. Start
        from a refusal: `exc.ask["body"]` carries `grantor_id` and `capabilities`.
        `reason` is your words, shown to the owner (up to 1000 characters).
        `estimated_cost` is `{"amount", "currency", "period"}`. `resume_token`
        is opaque (up to 200 characters) and comes back on `on_grant_decided`.
        A 429 `ask_limit` raises `SaltApiError` with `retry_after`."""
        return await self._request("POST", "/api/v1/mandates", api_key, _grant_body(
            grantor_id, capabilities, reason, duration_days, estimated_cost, resume_token, chat_id, label, plan_id, replaces
        ))

    async def check_grant(self, api_key: str, capability: str) -> dict[str, Any]:
        """Do you already hold this capability? `{allowed, mandate_id?, mode, constraints, needs_ask?}`.
        `needs_ask` is True for an `ask` row (`allowed` False): call `request_ask`, wait for `approval_decided`, then act under the plan."""
        return await self._request("GET", f"/api/v1/mandates/check?capability={quote(capability, safe='')}", api_key)

    async def update_mandate(self, api_key: str, mandate_id: str, params: _JSON) -> dict[str, Any]:
        """See SaltClient.update_mandate."""
        return await self._request("PATCH", f"/api/v1/mandates/{mandate_id}", api_key, params)

    async def tune_strategy(
        self,
        api_key: str,
        mandate_id: str,
        *,
        explore: float | None = None,
        goal_kind: str | None = None,
        pace: str | None = None,
        version: int,
    ) -> dict[str, Any]:
        """See SaltClient.tune_strategy."""
        return await self._request("PATCH", f"/api/v1/mandates/{mandate_id}", api_key, _tune_body(explore, goal_kind, pace, version))

    async def request_strategy_tune(
        self,
        api_key: str,
        mandate_id: str,
        *,
        explore: float | None = None,
        goal_kind: str | None = None,
        pace: str | None = None,
        reason: str,
    ) -> dict[str, Any]:
        """See SaltClient.request_strategy_tune."""
        return await self._request("POST", f"/api/v1/mandates/{mandate_id}/tune_requests", api_key, _tune_request_body(explore, goal_kind, pace, reason))

    async def request_ask(
        self,
        api_key: str,
        mandate_id: str,
        *,
        capability: str,
        where: str,
        words: str | None = None,
        words_armored: str | None = None,
        encrypt_to: str | Sequence[str] | None = None,
        own_public_key: str | None = None,
        recipient_id: str | None = None,
        chat_id: str | None = None,
        about: str | None = None,
        media_digest: str | None = None,
        kind: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """See SaltClient.request_ask."""
        body = _ask_body(capability, words, words_armored, encrypt_to, where, recipient_id, chat_id, own_public_key, about, media_digest, kind)
        return await self._request(
            "POST", f"/api/v1/mandates/{mandate_id}/asks", api_key, body, idempotency_key=idempotency_key or str(uuid.uuid4())
        )

    async def request_mode_change(
        self, api_key: str, mandate_id: str, *, capability: str, reason: str, mode: str = "notify"
    ) -> dict[str, Any]:
        """See SaltClient.request_mode_change."""
        return await self._request("POST", f"/api/v1/mandates/{mandate_id}/tune_requests", api_key, _mode_change_body(capability, mode, reason))

    async def decide_strategy_tune(self, api_key: str, mandate_id: str, request_id: str, decision: str) -> dict[str, Any]:
        """See SaltClient.decide_strategy_tune."""
        return await self._request("POST", f"/api/v1/mandates/{mandate_id}/tune_requests/{request_id}/decide", api_key, {"decision": decision})

    async def accept_mandate(self, api_key: str, mandate_id: str) -> dict[str, Any]:
        """See SaltClient.accept_mandate."""
        return await self._request("POST", f"/api/v1/mandates/{mandate_id}/accept", api_key)

    async def renew_mandate(self, api_key: str, mandate_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/mandates/{mandate_id}/renew", api_key)

    async def pause_mandate(self, api_key: str, mandate_id: str, reason: str | None = None) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/mandates/{mandate_id}/pause", api_key, {"reason": reason} if reason else None)

    async def resume_mandate(self, api_key: str, mandate_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/mandates/{mandate_id}/resume", api_key)

    async def revoke_mandate(self, api_key: str, mandate_id: str, reason: str | None = None) -> dict[str, Any]:
        """See SaltClient.revoke_mandate."""
        return await self._request("POST", f"/api/v1/mandates/{mandate_id}/revoke", api_key, {"reason": reason} if reason else None)

    async def get_mandate_exercises(
        self, api_key: str, mandate_id: str, *, before: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """See SaltClient.get_mandate_exercises."""
        params = []
        if before is not None:
            params.append(f"before={before}")
        if limit is not None:
            params.append(f"limit={limit}")
        path = f"/api/v1/mandates/{mandate_id}/exercises" + (("?" + "&".join(params)) if params else "")
        return await self._request("GET", path, api_key)

    async def get_open_mandate_exercises(self, api_key: str) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/mandates/exercises/open", api_key)

    async def decide_mandate_exercise(self, api_key: str, exercise_id: Any, decision: str, note: str | None = None) -> dict[str, Any]:
        """See SaltClient.decide_mandate_exercise."""
        return await self._request("POST", f"/api/v1/mandates/exercises/{exercise_id}/decide", api_key, {"decision": decision, "note": note})

    def act_for(self, principal_id: str, mandate_id: str | None = None) -> "AsyncActingSaltClient":
        """See SaltClient.act_for -- the async mirror. Not itself a
        coroutine (nothing to await; it just builds the wrapper), same as
        SaltClient.act_for."""
        return AsyncActingSaltClient(self, principal_id, mandate_id)

    async def hand_off(self, api_key: str, chat_id: str, to_agent_id: str, reason: str | None = None) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/chats/{chat_id}/hand_off", api_key, {"to_agent_id": to_agent_id, "reason": reason})

    async def hand_back(self, api_key: str, chat_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/chats/{chat_id}/hand_off/back", api_key)

    async def get_agent_updates(
        self, api_key: str, *, after: int = 0, timeout: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """See SaltClient.get_agent_updates."""
        path = f"/api/v1/agent/updates?after={after}&timeout={timeout}&limit={limit}"
        return await self._request("GET", path, api_key, timeout=timeout + 10)

    async def track_event(self, api_key: str, name: str, properties: _JSON | None = None) -> None:
        try:
            await self._request("POST", "/api/v1/events", api_key, {"name": name, "properties": properties or {}})
        except Exception:  # noqa: BLE001
            pass


class AsyncActingSaltClient(AsyncSaltClient):
    """Returned by `AsyncSaltClient.act_for(...)`. See `ActingSaltClient`
    (the sync mirror) for why this is a subclass rather than a wrapper.
    Do not construct directly -- use `AsyncSaltClient.act_for`."""

    def __init__(self, base: AsyncSaltClient, principal_id: str, mandate_id: str | None = None) -> None:
        self.host = base.host
        self._owns_http = False  # never closes the base client's shared session
        self.http = base.http
        self._principal_id = principal_id
        self._mandate_id = mandate_id

    async def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        json_body: _JSON | None = None,
        *,
        idempotency_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        headers = {ACT_FOR_HEADER: str(self._principal_id)}
        if self._mandate_id is not None:
            headers[MANDATE_HEADER] = str(self._mandate_id)
        if extra_headers:
            headers.update(extra_headers)
        if idempotency_key is None and method in ("POST", "PATCH"):
            idempotency_key = uuid.uuid4().hex
        return await super()._request(method, path, api_key, json_body, idempotency_key=idempotency_key, extra_headers=headers, timeout=timeout)
