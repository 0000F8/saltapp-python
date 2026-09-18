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
    """

    def __init__(self, method: str, url: str, status: int, body: Any) -> None:
        reason = ""
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, str):
                reason = f": {err}"
            elif not reason and isinstance(body.get("errors"), list):
                reason = f": {', '.join(str(e) for e in body['errors'])}"
        super().__init__(f"Salt API {method} {url} -> {status}{reason}")
        self.method = method
        self.url = url
        self.status = status
        self.body = body


class SaltAppError(Exception):
    """Base class for every other saltapp-specific error (crypto, webhook
    verification, socket transport, ask timeouts) -- lets a caller catch
    "something in saltapp went wrong" without also catching SaltApiError's
    "the server said no"."""
