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


class SaltAppError(Exception):
    """Base class for every other saltapp-specific error (crypto, webhook
    verification, socket transport, ask timeouts) -- lets a caller catch
    "something in saltapp went wrong" without also catching SaltApiError's
    "the server said no"."""
