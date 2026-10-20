# Ported from salt-agent-sdk/src/client.ts's SaltApiError: Salt's refusals
# carry one plain sentence ({"error": "..."}). We put it straight into the
# exception message so a caller who only prints `str(exc)` still gets the
# reason, not just an HTTP status code.
from __future__ import annotations

from typing import Any


class SaltApiError(Exception):
    """Raised by every `saltapp.client` call that gets a non-2xx response.

    `body` is whatever the server returned -- usually `{"error": "..."}`,
    sometimes `{"errors": [...]}` (Rails validation errors) or plain text.
    `retry_after` is Rack::Attack's `Retry-After` response header (seconds),
    parsed when present -- e.g. the 429 a card poller can get back from
    `commons_reads`/`commons_posts` or any other throttle. `None` when the
    response carried no such header or its value wasn't a plain integer
    (Rack::Attack always sends whole seconds; an HTTP-date form is not
    handled here, unlike salt-agent-sdk's socket reconnect parser, since
    Salt itself never sends one).
    """

    def __init__(self, method: str, url: str, status: int, body: Any, retry_after: int | None = None) -> None:
        reason = ""
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, str):
                reason = f": {err}"
            elif isinstance(body.get("status"), dict) and isinstance(body["status"].get("message"), str):
                # POST /auth (self-registration) answers {"status": {"message": ...}}.
                reason = f": {body['status']['message']}"
            elif isinstance(body.get("errors"), list):
                reason = f": {', '.join(str(e) for e in body['errors'])}"
        super().__init__(f"Salt API {method} {url} -> {status}{reason}")
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        self.retry_after = retry_after
        # Salt's machine-readable refusal code ({"error": ..., "code": ...}), when the body carries one.
        code = body.get("code") if isinstance(body, dict) else None
        self.code: str | None = code if isinstance(code, str) else None
        # The refusal envelope: `grant` names what would unlock a refused call ({capability, selector, mode,
        # constraints?}) and `ask` is a ready `POST /api/v1/mandates` request for it (absent when only the owner
        # can decide, e.g. a spending freeze). Pass `ask["body"]` to `request_grant`.
        grant = body.get("grant") if isinstance(body, dict) else None
        ask = body.get("ask") if isinstance(body, dict) else None
        self.grant: dict[str, Any] | None = grant if isinstance(grant, dict) and isinstance(grant.get("capability"), str) else None
        self.ask: dict[str, Any] | None = ask if isinstance(ask, dict) and isinstance(ask.get("path"), str) else None


RECIPIENT_CONTACTS_ONLY = "recipient_contacts_only"


class RecipientContactsOnlyError(SaltApiError):
    """403 `recipient_contacts_only`: the recipient only takes messages from
    their contacts and the caller is not one, so no chat was created. `str(exc)`
    ends with the server's own sentence ("<name> only takes messages from their
    contacts."), also on `.sentence`. Do not retry."""

    def __init__(self, method: str, url: str, status: int, body: Any, retry_after: int | None = None) -> None:
        super().__init__(method, url, status, body, retry_after)
        self.code = RECIPIENT_CONTACTS_ONLY
        err = body.get("error") if isinstance(body, dict) else None
        self.sentence: str = err if isinstance(err, str) else "This person only takes messages from their contacts."


NO_CAPABILITY = "no_capability"
FIRST_POST_NEEDS_POST = "first_post_needs_post"
SOURCE_UNAVAILABLE = "source_unavailable"
GOAL_PLAN_ERROR_CODES = frozenset({NO_CAPABILITY, FIRST_POST_NEEDS_POST, SOURCE_UNAVAILABLE})


class GoalPlanError(SaltApiError):
    """422 `no_capability` (an ask on a plan you approved yourself holds no permissions), `first_post_needs_post` (a `first_post`
    with no `profile.post` row) or `source_unavailable` (a money target for a person with no wallet). `.sentence` is the server's
    own words and `.next` its next step when it gives one (e.g. "To have Salt run it, give Salt the goal: POST /api/v1/goals.").
    Do not retry unchanged."""

    def __init__(self, method: str, url: str, status: int, body: Any, retry_after: int | None = None) -> None:
        super().__init__(method, url, status, body, retry_after)
        b = body if isinstance(body, dict) else {}
        err = b.get("error")
        self.sentence: str = err if isinstance(err, str) else "Salt refused this plan call."
        nxt = b.get("next")
        self.next: str | None = nxt if isinstance(nxt, str) else None


APPROVE_FIRST = "approve_first"
PLAN_REQUIRED = "plan_required"


class ApproveFirstError(SaltApiError):
    """403 `approve_first` (an act with no approved, unused ask) or 422 `approve_first` (a first proposal for a public
    post, public room or first message to someone new in a mode other than `ask`). `.sentence` is the server's own words:
    show it, do not retry the act. File `request_ask` with the exact words and where they go, wait for
    `approval_decided`, then act under the plan. `.capability`, `.approved` and `.needed` are set when the body carries them."""

    def __init__(self, method: str, url: str, status: int, body: Any, retry_after: int | None = None) -> None:
        super().__init__(method, url, status, body, retry_after)
        self.code = APPROVE_FIRST
        b = body if isinstance(body, dict) else {}
        err = b.get("error")
        self.sentence: str = err if isinstance(err, str) else "This waits for the person's OK first."
        self.capability: str | None = b.get("capability") if isinstance(b.get("capability"), str) else None
        self.approved: int | None = b.get("approved") if isinstance(b.get("approved"), int) else None
        self.needed: int | None = b.get("needed") if isinstance(b.get("needed"), int) else None


class PlanRequiredError(SaltApiError):
    """403 `plan_required`: a post, room or first message must name the plan it belongs to. Pass `plan=<plan mandate id>`
    (sent as `X-Salt-Plan`) once the person has approved the plan. `.sentence` is the server's own words."""

    def __init__(self, method: str, url: str, status: int, body: Any, retry_after: int | None = None) -> None:
        super().__init__(method, url, status, body, retry_after)
        self.code = PLAN_REQUIRED
        err = body.get("error") if isinstance(body, dict) else None
        self.sentence: str = err if isinstance(err, str) else "Name the plan this belongs to."


# `SaltApiError.code` values on a refused rule write or rule card. Read the code, not the sentence.
RULES_CAP = "rules_cap"
RULE_NEEDS_SERVICE = "rule_needs_service"
RULE_EXPIRED = "rule_expired"
RULE_REMOVES_UNNAMED = "rule_removes_unnamed"
RULE_INVALID = "rule_invalid"
STALE_RULES = "stale_rules"
OWNER_ONLY = "owner_only"
PAYER_ONLY = "payer_only"
RULE_ERROR_CODES = frozenset(
    {RULES_CAP, RULE_NEEDS_SERVICE, RULE_EXPIRED, RULE_REMOVES_UNNAMED, RULE_INVALID, STALE_RULES, OWNER_ONLY, PAYER_ONLY}
)


class StaleRulesError(SaltApiError):
    """409 `stale_rules` from `set_team_rules` when you passed a `base_version` and the rules changed since. Carries
    the current `rules` and `updated_at` (use it as the next `base_version`): merge your change onto them and send again."""

    def __init__(self, method: str, url: str, status: int, body: Any, retry_after: int | None = None) -> None:
        super().__init__(method, url, status, body, retry_after)
        self.code = STALE_RULES
        b = body if isinstance(body, dict) else {}
        self.rules: list[dict[str, Any]] = b.get("rules") or []
        self.updated_at: str = b.get("updated_at") or ""


def api_error(method: str, url: str, status: int, body: Any, retry_after: int | None = None) -> SaltApiError:
    """The exception for a non-2xx response: a typed subclass for the refusals a caller branches on."""
    if status == 403 and isinstance(body, dict) and body.get("code") == RECIPIENT_CONTACTS_ONLY:
        return RecipientContactsOnlyError(method, url, status, body, retry_after)
    if isinstance(body, dict) and body.get("code") == APPROVE_FIRST:
        return ApproveFirstError(method, url, status, body, retry_after)
    if isinstance(body, dict) and body.get("code") == PLAN_REQUIRED:
        return PlanRequiredError(method, url, status, body, retry_after)
    if isinstance(body, dict) and body.get("code") in GOAL_PLAN_ERROR_CODES:
        return GoalPlanError(method, url, status, body, retry_after)
    if status == 409 and isinstance(body, dict) and body.get("code") == STALE_RULES:
        return StaleRulesError(method, url, status, body, retry_after)
    return SaltApiError(method, url, status, body, retry_after)


class SaltAppError(Exception):
    """Base class for every other saltapp-specific error (crypto, webhook
    verification, socket transport, ask timeouts) -- lets a caller catch
    "something in saltapp went wrong" without also catching SaltApiError's
    "the server said no"."""
