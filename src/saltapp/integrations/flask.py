# Thin Flask adapter for saltapp.agent.Agent. Flask is synchronous, so
# dispatch runs inline on the request thread (Flask itself is usually run
# multi-threaded/multi-worker, so one slow agent turn doesn't stop other
# requests being served -- but it IS a blocking call on the thread handling
# this particular webhook, unlike the ASGI/FastAPI paths). If your handlers
# make slow model calls, prefer `Agent.asgi_app()` or the FastAPI adapter.
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from saltapp.agent import Agent

try:
    from flask import Blueprint, jsonify, request
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "saltapp.integrations.flask requires the 'flask' extra: pip install 'saltapp[flask]'"
    ) from exc

from saltapp.webhook import WebhookVerificationError, handle


def create_blueprint(agent: "Agent", *, name: str = "saltapp_agent") -> Blueprint:
    """A Blueprint with `POST /` (the webhook) and `GET /health`. Register
    it on your own Flask app:

        app = Flask(__name__)
        app.register_blueprint(saltapp.integrations.flask.create_blueprint(agent))
    """
    bp = Blueprint(name, __name__)

    @bp.route("/", methods=["POST"])
    def webhook():
        raw = request.get_data()
        try:
            event = handle(
                dict(request.headers),
                raw,
                secret=agent.webhook_secret,
                verify=agent.verify_signatures,
                tolerance_seconds=agent.signature_tolerance_seconds,
            )
        except WebhookVerificationError as exc:
            return jsonify({"error": str(exc)}), 401
        agent.dispatch_sync(event)
        return jsonify({"status": "accepted"})

    @bp.route("/health")
    def health():
        return jsonify({"status": "ok", **agent.health_extra()})

    return bp


def create_app(agent: "Agent"):
    """A standalone Flask app, if you don't have one already."""
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(create_blueprint(agent))
    return app
